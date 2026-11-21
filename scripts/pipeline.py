"""The same build, test and scan commands are used locally and in Actions."""
import json
import hashlib
import re
import os
from pathlib import Path
import secrets
from collections import Counter
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get('BUILD_DIR', ROOT/'.build')).resolve()
PROFILE = os.environ.get('BUILD_PROFILE', 'erpnext')
if PROFILE not in ('erpnext', 'frappe'):
    raise ValueError('Unknown build profile')
IMAGE = PROFILE+'-community:test'


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def load():
    return json.loads((OUT/'lock.json').read_text())



from base_manifest import LOCAL_IMAGES, OFFICIAL_IMAGES, PUBLISHED_IMAGE, policy_hash, validate


def image_output(image, executable, *args):
    return subprocess.check_output(
        ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', executable, image, *args],
        text=True,
    ).strip()


def versions(image):
    return {
        'python': image_output(image, 'python', '--version').removeprefix('Python '),
        'node': image_output(image, 'node', '--version').removeprefix('v'),
        'bench': image_output(image, 'bench', '--version'),
        'chromium_version': re.search(r'\b\d+\.\d+\.\d+\.\d+\b',
                                      image_output(image, 'chromium-headless-shell', '--version'))[0],
    }


def verify_chrome_package_delta(before, after):
    expected = dict(before)
    for package in ('chromium-headless-shell', 'chromium-common'):
        expected.pop(package)
    expected.setdefault('libatk-bridge2.0-0t64', '2.56.2-1+deb13u2')
    if after != expected:
        raise ValueError('Unexpected Debian package change in Chrome base')


def prepare_base():
    from resolve import image_digest, request, scanner_version
    parents = {}
    for kind, repository in OFFICIAL_IMAGES.items():
        tag = repository + ':version-16'
        details = json.loads(request('https://hub.docker.com/v2/repositories/' + repository + '/tags/version-16'))
        [digest] = [row['digest'] for row in details['images']
                    if row['os'] == 'linux' and row['architecture'] == 'amd64']
        parents[kind] = tag+'@'+digest
    chrome = json.loads((ROOT/'resources/chromium.json').read_text())
    for kind, parent in parents.items():
        image = LOCAL_IMAGES[kind]
        run(['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load',
             '--file', 'Containerfile.chrome', '--build-arg', 'FRAPPE_IMAGE='+parent,
             '--tag', image, '.'])
        verify_chrome_package_delta(package_versions(parent), package_versions(image))
    verification = {'trivy_image': image_digest('aquasec/trivy:'+scanner_version().lstrip('v')),
                    'grype_image': image_digest('anchore/grype:v0.121.3'),
                    'mariadb_image': image_digest('mariadb:11.8'),
                    'redis_image': image_digest('redis:8-alpine')}
    metadata = {'schema': 4, 'images': dict(LOCAL_IMAGES), 'parents': parents,
                'inputs': versions(LOCAL_IMAGES['runtime']),
                'verification_inputs': verification}
    validate(metadata, require_verified=False)
    if (versions(LOCAL_IMAGES['builder']) != metadata['inputs']
            or metadata['inputs']['chromium_version'] != chrome['version']):
        raise ValueError('Official Frappe builder and runtime versions differ')
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/'base-images.json').write_text(json.dumps(metadata, indent=2)+'\n')
    (OUT/'lock.json').write_text(json.dumps({**metadata['inputs'], **verification}, indent=2)+'\n')


def locked_base_images(lock):
    metadata = validate(json.loads((OUT/'base-images.json').read_text()))
    if any(lock.get(name) != value for name, value in metadata['inputs'].items()):
        raise ValueError('Application lock must use the selected base runtime inputs')
    return metadata['images']


def select_profile(lock, profile):
    if profile not in ('erpnext', 'frappe'):
        raise ValueError('Unknown build profile')
    result = json.loads(json.dumps(lock))
    result['profile'] = profile
    if profile == 'frappe':
        apps = [app for app in result['apps'] if app['name'] == 'frappe']
        if len(apps) != 1 or not re.fullmatch(r'v\d+\.\d+\.\d+', apps[0]['ref']):
            raise ValueError('Missing stable Frappe source')
        result['apps'] = apps
        result['frappe_tag'] = apps[0]['ref']
        result.pop('erpnext_tag', None)
        result['community_patches'] = {name: patch for name, patch in result['community_patches'].items()
                                       if name == 'frappe'}
    return result

def build():
    lock = select_profile(load(), PROFILE)
    bases = locked_base_images(lock)
    lock['base_images'] = bases
    (OUT/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    args = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load',
            '--build-context', f'resolved={OUT}', '--file',
            'Containerfile.frappe' if PROFILE == 'frappe' else 'Containerfile', '--tag', IMAGE]
    values = {'RUNTIME_BASE_IMAGE': bases['runtime'], 'BUILDER_BASE_IMAGE': bases['builder'],
              ('FRAPPE_TAG' if PROFILE == 'frappe' else 'ERPNEXT_TAG'):
                  lock['frappe_tag' if PROFILE == 'frappe' else 'erpnext_tag'],
              'SOURCE_REVISION': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}
    for key, value in values.items():
        args += ['--build-arg', f'{key}={value}']
    run(args+['.'])
    inventory = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'cat', IMAGE,
                                        '/home/frappe/frappe-bench/image-lock.json'])
    (OUT/'image-lock.json').write_bytes(inventory)
    capture_installed(IMAGE)
    image_id = subprocess.check_output(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], text=True).strip()
    (OUT/'image-id.txt').write_text(image_id)
    run(['docker', 'save', '--output', str(OUT/'image.tar'), IMAGE])


def package_versions(image):
    query = image_output(image, 'dpkg-query', '-W', '-f=${Package}\t${Version}\n')
    return dict(line.split('\t', 1) for line in query.splitlines())


def capture_installed(image):
    (OUT/'installed-packages.json').write_text(json.dumps({'versions': package_versions(image)}, indent=2)+'\n')


def integration():
    lock = load()
    (OUT/'pdf').mkdir(exist_ok=True)
    # Disposable artifact directory is written by the container's uid 1000.
    (OUT/'pdf').chmod(0o777)
    env = dict(os.environ, TEST_IMAGE=IMAGE, MARIADB_IMAGE=lock['mariadb_image'],
               REDIS_IMAGE=lock['redis_image'], DB_PASSWORD=secrets.token_hex(24),
               ADMIN_PASSWORD=secrets.token_hex(24), PDF_OUTPUT_DIR=str(OUT/'pdf'))
    compose = ['docker', 'compose', '-p', 'erpnext-test-'+secrets.token_hex(4),
               '-f', str(ROOT/'tests/compose.yaml')]
    report = OUT/'integration.log'
    try:
        with report.open('w', encoding='utf-8') as log:
            for args in (['up', '-d', '--wait', '--wait-timeout', '300', 'db', 'redis'],
                         ['run', '--rm', 'setup'],
                         ['up', '-d', 'backend', 'frontend', 'websocket', 'scheduler',
                          'worker-short', 'worker-default', 'worker-long', 'worker-long-peer'],
                         ['run', '--rm', 'verify']):
                run(compose+args, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            for service in ('backend', 'frontend', 'websocket', 'scheduler',
                            'worker-short', 'worker-default', 'worker-long', 'worker-long-peer'):
                ids = subprocess.check_output(compose+['ps', '-q', service], env=env, text=True).strip()
                if not ids:
                    raise RuntimeError(f'{service} exited')
                state = json.loads(subprocess.check_output(['docker', 'inspect', ids], text=True))[0]
                if not state['State']['Running'] or state['RestartCount']:
                    raise RuntimeError(f'{service} is unhealthy')
        print('Integration checks passed')
    finally:
        logs = subprocess.run(compose+['logs', '--no-color'], env=env, capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        for secret in (env['DB_PASSWORD'], env['ADMIN_PASSWORD']):
            text = text.replace(secret, '[REDACTED]')
        (OUT/'services.log').write_text(text, encoding='utf-8')
        if report.exists():
            text = report.read_text(encoding='utf-8')
            for secret in (env['DB_PASSWORD'], env['ADMIN_PASSWORD']):
                text = text.replace(secret, '[REDACTED]')
            report.write_text(text, encoding='utf-8')
        run(compose+['down', '-v', '--remove-orphans'], env=env)


def assess_report(data, exceptions=(), advisory=False, verified_fixed=(), verified_not_affected=(), advisories=()):
    """Keep raw evidence; exempt only exact approved vulnerability records."""
    if not isinstance(data.get('Results'), list) or data.get('SchemaVersion') != 2:
        raise ValueError('Missing or unsupported scanner results')
    accepted, blocking, reported, patched, unaffected = [], [], [], [], []
    for row in data['Results']:
        for finding in row.get('Vulnerabilities', []):
            key = dict(cve=finding['VulnerabilityID'], package=finding['PkgName'],
                       version=finding['InstalledVersion'], severity=finding['Severity'],
                       type=row['Type'], classification=row['Class'])
            if advisory:
                reported.append(key)
            elif key in verified_not_affected:
                unaffected.append(key)
            elif key in advisories:
                reported.append(key)
            elif key in verified_fixed:
                patched.append(key)
            elif key in exceptions:
                accepted.append(key)
            else:
                blocking.append(key)
        # Service vulnerability acceptance never exempts leaked secrets/config errors.
        for kind in ('Secrets', 'Misconfigurations'):
            blocking.extend(f for f in row.get(kind, [])
                            if f.get('Severity') in ('HIGH', 'CRITICAL')
                            and f.get('Status', 'FAIL') != 'PASS')
    return dict(accepted=accepted, blocking=blocking, advisory=reported, verified_fixed=patched, verified_not_affected=unaffected)



def scan_chromium(lock, cache):
    version = lock['chromium_version']
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        raise ValueError('Invalid installed Chromium version')
    grype = ['docker', 'run', '--rm', '-v', f'{OUT}:/scan', '-v', f'{cache}:/cache',
             '-e', 'GRYPE_DB_CACHE_DIR=/cache/grype', '-e', 'GRYPE_MATCH_STOCK_USING_CPES=true',
             '-e', 'GRYPE_DB_REQUIRE_UPDATE_CHECK=true', '-e', 'GRYPE_CHECK_FOR_APP_UPDATE=false',
             lock['grype_image']]
    reports = {}
    for name in ('chromium-control', 'chromium'):
        filename = name+'-security.json'
        result = subprocess.run(grype+['cpe:2.3:a:google:chrome:'+('156.0.8078.79' if name == 'chromium-control' else version)+':*:*:*:*:*:*:*', '--output', 'json',
                                       '--file', '/scan/'+filename])
        if result.returncode:
            raise ValueError('Chrome product scanner failed')
        report = json.loads((OUT/filename).read_text())
        if not isinstance(report.get('matches'), list):
            raise ValueError('Chrome product scanner returned invalid results')
        reports[name] = [m for m in report['matches']
                         if m['vulnerability']['severity'].upper() in ('HIGH', 'CRITICAL')]
    if not reports['chromium-control']:
        raise ValueError('Chrome CPE scan coverage control failed')
    return {'accepted': [], 'blocking': [], 'advisory': reports['chromium'], 'verified_fixed': []}

def scan_base():
    global OUT
    root = OUT
    lock = load()
    metadata = validate(json.loads((OUT/'base-images.json').read_text()), require_verified=False)
    cache = 'base-scan-'+secrets.token_hex(4)
    run(['docker', 'volume', 'create', cache])
    reports, failed, image_ids = {}, [], {}
    try:
        for kind in ('runtime', 'builder'):
            image = metadata['images'][kind]
            image_ids[kind] = image_id(image)
            if versions(image) != metadata['inputs']:
                raise ValueError('Base image versions differ from the resolved inputs')
            OUT = root/(kind+'-reports')
            OUT.mkdir(exist_ok=True)
            (OUT/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
            capture_installed(image)
            run(['docker', 'save', '--output', str(OUT/'image.tar'), image_ids[kind]])
            try:
                scan_with_cache(cache)
            except RuntimeError:
                failed.append(kind)
            reports[kind] = hashlib.sha256((OUT/'gate-security.json').read_bytes()).hexdigest()
            (OUT/'image.tar').unlink()
    finally:
        OUT = root
        run(['docker', 'volume', 'rm', cache])
    if failed:
        raise RuntimeError('Base security gate failed: '+', '.join(failed))
    metadata['security'] = {'status': 'passed', 'severity': ['HIGH', 'CRITICAL'],
                            'policy_sha256': policy_hash(), 'reports': reports,
                            'image_ids': image_ids,
                            'trivy_image': lock['trivy_image'], 'grype_image': lock['grype_image']}
    metadata['source_revision'] = os.environ['GITHUB_SHA']
    metadata['evidence'] = ('https://github.com/'+os.environ['GITHUB_REPOSITORY']+
                            '/actions/runs/'+os.environ['GITHUB_RUN_ID'])
    (OUT/'base-images.json').write_text(json.dumps(metadata, indent=2)+'\n')


def image_id(image):
    return subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                   text=True).strip()


def publish_base():
    if os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_REPOSITORY') != 'modulairy/erpnext-container':
        raise ValueError('Base publication requires main in the owning repository')
    path = OUT/'base-images.json'
    metadata = validate(json.loads(path.read_text()), require_verified=False)
    security = metadata.get('security', {})
    if (metadata['images'] != LOCAL_IMAGES or security.get('status') != 'passed'
            or security.get('policy_sha256') != policy_hash()
            or set(security.get('reports', {})) != set(LOCAL_IMAGES)
            or metadata.get('source_revision') != os.environ['GITHUB_SHA']):
        raise ValueError('Base publication requires this run to pass both scans')
    images = {}
    for kind, image in LOCAL_IMAGES.items():
        tested = security['image_ids'][kind]
        if image_id(image) != tested or not re.fullmatch(r'sha256:[a-f0-9]{64}', tested):
            raise ValueError('Local base image differs from the scanned image')
        tag = PUBLISHED_IMAGE+':'+kind+'-'+tested.removeprefix('sha256:')
        run(['docker', 'tag', tested, tag])
        output = subprocess.check_output(['docker', 'push', tag], text=True)
        digest, = re.findall(r'digest: (sha256:[a-f0-9]{64})\s+size:', output)
        images[kind] = PUBLISHED_IMAGE+'@'+digest
    metadata['images'] = images
    validate(metadata)
    path.write_text(json.dumps(metadata, indent=2)+'\n')


def scan():
    cache = 'erpnext-scan-'+secrets.token_hex(4)
    run(['docker', 'volume', 'create', cache])
    try:
        scan_with_cache(cache)
    finally:
        run(['docker', 'volume', 'rm', cache])


def scan_with_cache(cache):
    lock = load()
    trivy = ['docker', 'run', '--rm', '-v', f'{OUT}:/scan', '-v', f'{ROOT}:/source:ro',
             '-v', f'{cache}:/root/.cache/trivy',
             lock['trivy_image'], '--timeout', '15m']
    run(trivy+['image', '--input', '/scan/image.tar', '--format', 'cyclonedx',
               '--output', '/scan/sbom.cdx.json'])
    from chromium import component
    sbom_path = OUT/'sbom.cdx.json'
    sbom = json.loads(sbom_path.read_text(encoding='utf-8'))
    chrome = json.loads((ROOT/'resources/chromium.json').read_text())
    if chrome['version'] != lock['chromium_version']:
        raise ValueError('Chrome SBOM version differs from the scanned base')
    chrome_component = component(chrome)
    components = sbom.setdefault('components', [])
    if not any(item.get('purl') == chrome_component['purl'] for item in components):
        components.append(chrome_component)
    updated_sbom = OUT/'sbom.updated.cdx.json'
    updated_sbom.write_text(json.dumps(sbom, indent=2)+'\n', encoding='utf-8')
    updated_sbom.replace(sbom_path)
    checks = [
        ['image', '--input', '/scan/image.tar', '--scanners', 'vuln,secret', '--severity', 'HIGH,CRITICAL',
         '--exit-code', '0', '--format', 'json', '--output', '/scan/vulnerabilities.json'],
        ['fs', '--scanners', 'misconfig,secret', '--severity', 'HIGH,CRITICAL', '--exit-code', '0',
         '--skip-dirs', '/source/.git,/source/.build', '--format', 'json',
         '--output', '/scan/config-security.json', '/source']]
    for service in ('mariadb', 'redis'):
        checks.append(['image', '--scanners', 'vuln,secret', '--severity', 'HIGH,CRITICAL',
                       '--exit-code', '0', '--format', 'json',
                       '--output', f'/scan/service-{service}-security.json', lock[service+'_image']])
    policy = json.loads((ROOT/'security-exceptions.json').read_text())
    failed = False
    assessments = {}
    try:
        assessments['chromium-security.json'] = scan_chromium(lock, cache)
        failed |= bool(assessments['chromium-security.json']['blocking'])
    except (ValueError, KeyError, OSError, TypeError) as error:
        failed = True
        assessments['chromium-security.json'] = {'scanner_error': str(error)}
    for args in checks:
        filename = Path(args[args.index('--output')+1]).name
        report = OUT/filename
        report.unlink(missing_ok=True)
        result = subprocess.run(trivy+args)
        if result.returncode != 0:
            failed = True
            assessments[filename] = {'scanner_error': result.returncode}
            continue
        try:
            data = json.loads(report.read_text(encoding='utf-8'))
            assessment = assess_report(
                data, policy['application'] if filename == 'vulnerabilities.json' else (),
                advisory=True,
            )
            assessments[filename] = assessment
            failed |= bool(assessment['blocking'])
        except (ValueError, KeyError, OSError, TypeError) as error:
            failed = True
            assessments[filename] = {'report_error': str(error)}
    (OUT/'gate-security.json').write_text(json.dumps(assessments, indent=2)+'\n')
    for filename, assessment in assessments.items():
        summary = (f"{filename}: {len(assessment.get('accepted', []))} accepted, "
                   f"{len(assessment.get('advisory', []))} advisory, "
                   f"{len(assessment.get('blocking', []))} blocking; "
                   f"{len(assessment.get('verified_fixed', []))} verified fixed; "
                   f"errors: {assessment.get('scanner_error', assessment.get('report_error', 'none'))}")
        print(summary)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
                handle.write(summary+'\n\n')
    for label, filename in [('Application image', 'vulnerabilities.json'),
                            ('MariaDB service', 'service-mariadb-security.json'),
                            ('Redis service', 'service-redis-security.json')]:
        report = OUT/filename
        if not report.exists():
            continue
        data = json.loads(report.read_text(encoding='utf-8'))
        findings = [v for row in data.get('Results', []) for v in row.get('Vulnerabilities', [])]
        counts = Counter(v['Severity'] for v in findings)
        summary = f"{label} security findings: {dict(counts)}; {len({v['VulnerabilityID'] for v in findings})} unique vulnerability IDs.\n"
        print(summary)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
                handle.write(summary+'See the reports artifact for package details.\n')
    if failed:
        raise RuntimeError('Security gate failed; review security reports')


if __name__ == '__main__':
    os.chdir(ROOT)
    {'build': build, 'integration': integration, 'scan': scan,
     'scan-base': scan_base, 'prepare-base': prepare_base, 'publish-base': publish_base}[sys.argv[1]]()
