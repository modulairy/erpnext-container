"""The same build, test and scan commands are used locally and in Actions."""
import json
import hashlib
import fnmatch
import re
import shutil
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



from base_manifest import BASE_IMAGE, BASE_FIELDS


def base_key(lock):
    # App releases, Bench and resolved_at must never invalidate OS compilation.
    inputs = {'platform': 'linux/amd64', 'lock': {k: lock[k] for k in BASE_FIELDS},
              'files': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                        for name in ('Containerfile.base', 'scripts/os_backports.py', 'scripts/chromium.py', 'scripts/python_expat.py', 'scripts/bootstrap.py', 'resources/bootstrap.json')}}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def registry_digest(tag):
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', tag,
                             '--format', '{{json .Manifest.Digest}}'],
                            capture_output=True, text=True)
    if result.returncode:
        if any(message in result.stderr.lower() for message in
               ('not found', 'manifest unknown', 'name unknown')):
            return None
        raise RuntimeError('Cannot inspect base image: '+result.stderr.strip())
    digest = json.loads(result.stdout)
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Invalid base image digest')
    return digest



def config():
    lock = load()
    report = OUT/'base-config.json'
    report.unlink(missing_ok=True)
    run(['docker', 'run', '--rm', '-v', f'{OUT}:/scan', '-v', f'{ROOT}:/source:ro',
         lock['trivy_image'], 'fs', '--scanners', 'misconfig,secret',
         '--severity', 'HIGH,CRITICAL', '--exit-code', '1',
         '--skip-dirs', '/source/.git,/source/.build', '--format', 'json',
         '--output', '/scan/base-config.json', '/source'])
    cache = 'chrome-config-'+secrets.token_hex(4)
    run(['docker', 'volume', 'create', cache])
    try:
        result = scan_chromium(lock, cache)
        if result['blocking']:
            raise ValueError('Chrome release has blocking product vulnerabilities')
    finally:
        run(['docker', 'volume', 'rm', cache])


def base():
    lock = load()
    key = base_key(lock)
    context = OUT/'base'
    context.mkdir(parents=True, exist_ok=True)
    (context/'lock.json').write_text(json.dumps({**{k: lock[k] for k in BASE_FIELDS}, 'resolved_at': lock['resolved_at']}, indent=2)+'\n')
    shutil.copyfile(OUT/'mariadb-keyring.asc', context/'mariadb-keyring.asc')
    images, rebuilt = {}, []
    force = '--rebuild' in sys.argv[2:]
    # Build builder first so the runtime target reuses the expensive shared layers.
    for kind in ('builder', 'runtime'):
        tag = f'{BASE_IMAGE}:{kind}-{key}'
        digest = None if force else registry_digest(tag)
        if digest is None:
            args = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--push',
                    '--build-context', f'resolved={context}', '--file', 'Containerfile.base',
                    '--target', kind+'-base', '--tag', tag,
                    '--label', 'io.modulairy.erpnext.base-key='+key,
                    '--metadata-file', str(context/(kind+'-metadata.json'))]
            if force and kind == 'builder':
                args += ['--no-cache']
            for name, value in (('PYTHON_IMAGE', lock['python_image']),
                                ('NODE_IMAGE', lock['node_image']), ('YARN_VERSION', lock['yarn'])):
                args += ['--build-arg', name+'='+value]
            run(args+['.'])
            digest = registry_digest(tag)
            if digest is None:
                raise RuntimeError('Published base manifest is missing')
            rebuilt.append(kind)
        images[kind] = BASE_IMAGE+'@'+digest
        smoke_base(images[kind], kind)

    data = {'schema': 2, 'key': key, 'images': images, 'rebuilt': rebuilt,
            'inputs': {name: lock[name] for name in BASE_FIELDS},
            'verification_inputs': {name: lock[name] for name in ('trivy_image', 'grype_image', 'mariadb_image', 'redis_image')},
            'bootstrap': json.loads((ROOT/'resources/bootstrap.json').read_text())}
    (OUT/'base-images.json').write_text(json.dumps(data, indent=2)+'\n')
    summary = f"Base images: {key}; rebuilt: {', '.join(rebuilt) or 'none (GHCR reuse)'}.\n"
    summary += '\n'.join(f'{kind}: {ref}' for kind, ref in images.items())+'\n'
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write(summary)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            for kind, ref in images.items():
                handle.write(f'{kind}={ref}\n')


def locked_base_images(lock):
    from base_manifest import validate
    metadata = validate(json.loads((OUT/'base-images.json').read_text()))
    if lock.get('bootstrap') != metadata['bootstrap'] or any(lock.get(name) != value for name, value in metadata['inputs'].items()):
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
              'BENCH_VERSION': lock['bench'],
              ('FRAPPE_TAG' if PROFILE == 'frappe' else 'ERPNEXT_TAG'):
                  lock['frappe_tag' if PROFILE == 'frappe' else 'erpnext_tag'],
              'SOURCE_REVISION': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}
    for key, value in values.items():
        args += ['--build-arg', f'{key}={value}']
    run(args+['.'])
    inventory = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'cat', IMAGE,
                                        '/home/frappe/frappe-bench/image-lock.json'])
    (OUT/'image-lock.json').write_bytes(inventory)
    inventory = json.loads(inventory)
    capture_installed(IMAGE, inventory, lock)
    image_id = subprocess.check_output(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], text=True).strip()
    (OUT/'image-id.txt').write_text(image_id)
    run(['docker', 'save', '--output', str(OUT/'image.tar'), IMAGE])


def assess_integrity(output, policy):
    rules = []
    for line in policy.splitlines():
        match = re.fullmatch(r'path-(exclude|include)(?:\s+|=)(\S+)', line.strip())
        if match:
            rules.append(match.groups())
    omitted, blocking = [], []
    for line in output.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'missing\s+(/\S+)', line)
        path = match[1] if match else ''
        excluded = False
        for operation, pattern in rules:
            if fnmatch.fnmatchcase(path, pattern):
                excluded = operation == 'exclude'
        documentation = path.startswith(('/usr/share/doc/', '/usr/share/man/', '/usr/share/lintian/overrides/'))
        if match and documentation and excluded:
            omitted.append(path)
        else:
            blocking.append(line)
    return {'intentional_omissions': omitted, 'blocking': blocking,
            'policy_sha256': hashlib.sha256(policy.encode()).hexdigest(), 'raw_output': output}


def capture_installed(image, inventory, lock):
    query = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'dpkg-query', image,
                                     '-W', '-f=${Package}\\t${Version}\\n'], text=True)
    versions = dict(line.split('\t', 1) for line in query.splitlines())
    packages = sorted({name for proof in inventory['os_backport_provenance'].values()
                       for name in proof['binaries']})
    packages += sorted({name for proof in inventory['os_backport_provenance'].values()
                        for name in proof.get('build_only_binaries', {}) if name in versions})
    integrity = subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'dpkg', image,
                                '--verify', *packages], capture_output=True, text=True)
    slim_policy = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'cat', image,
                                           '/etc/dpkg/dpkg.cfg.d/docker'], text=True)
    checked = assess_integrity(integrity.stdout, slim_policy)
    checked.update(exit_code=integrity.returncode, stderr=integrity.stderr, policy=slim_policy)
    (OUT/'package-integrity.json').write_text(json.dumps(checked, indent=2)+'\n')
    if integrity.returncode or integrity.stderr.strip() or checked['blocking']:
        raise ValueError('Installed backport package integrity check failed: '+json.dumps(checked['blocking']))
    xml_check = ("import hashlib,json,pyexpat; from pathlib import Path; "
                 "import _elementtree; "
                 "print(json.dumps({'version':pyexpat.EXPAT_VERSION,'binaries':{"
                 "name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() "
                 "for name,module in [('pyexpat',pyexpat),('_elementtree',_elementtree)]}}))")
    xml = json.loads(subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'python',
                                             image, '-c', xml_check], text=True))
    proof = inventory['python_expat_provenance']
    if (proof['sources'] != lock['pyexpat_sources'] or proof['tests_passed'] is not True
            or xml['version'] != proof['expat_version']
            or any(xml['binaries'][name] != item['sha256'] for name, item in proof['binaries'].items())):
        raise ValueError('Installed Python XML modules differ from verified build provenance')
    (OUT/'installed-packages.json').write_text(json.dumps(
        {'versions': versions, 'verified_packages': packages, 'python_xml': xml}, indent=2)+'\n')


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
                          'worker-short', 'worker-default', 'worker-long', 'worker-long-peer',
                          'cache-origin', 'cache-frontend', 'cache-fail'],
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
    from chromium import component
    chrome = component(lock['chromium'])
    document = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1, 'components': [chrome]}
    (OUT/'chromium.cdx.json').write_text(json.dumps(document, indent=2)+'\n')
    control = json.loads(json.dumps(document))
    item = control['components'][0]
    item['version'] = '156.0.8078.79'
    item['purl'] = 'pkg:generic/google/chrome-headless-shell@'+item['version']
    item['cpe'] = 'cpe:2.3:a:google:chrome:'+item['version']+':*:*:*:*:linux:*:*'
    item.pop('hashes', None)
    item.pop('externalReferences', None)
    (OUT/'chromium-control.cdx.json').write_text(json.dumps(control, indent=2)+'\n')
    grype = ['docker', 'run', '--rm', '-v', f'{OUT}:/scan', '-v', f'{cache}:/cache',
             '-e', 'GRYPE_DB_CACHE_DIR=/cache/grype', '-e', 'GRYPE_MATCH_STOCK_USING_CPES=true',
             '-e', 'GRYPE_DB_REQUIRE_UPDATE_CHECK=true', '-e', 'GRYPE_CHECK_FOR_APP_UPDATE=false',
             lock['grype_image']]
    reports = {}
    for name in ('chromium-control', 'chromium'):
        filename = name+'-security.json'
        result = subprocess.run(grype+['cpe:2.3:a:google:chrome:'+('156.0.8078.79' if name == 'chromium-control' else lock['chromium']['version'])+':*:*:*:*:*:*:*', '--output', 'json',
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
    return {'accepted': [], 'blocking': reports['chromium'], 'advisory': [], 'verified_fixed': []}

def smoke_base(image, kind):
    # These images stop before Bench, and compilers belong only to the builder.
    check = ("import importlib.util,shutil; from pathlib import Path; "
             "assert importlib.util.find_spec('bench') is None; "
             "assert Path('/opt/os-backports/provenance.json').is_file(); "
             "assert shutil.which('node'); assert shutil.which('chromium-headless-shell'); ")
    check += ("assert all(shutil.which(x) for x in ('gcc','npm','yarn'))"
              if kind == 'builder' else
              "assert not any(shutil.which(x) for x in ('gcc','npm','yarn'))")
    run(['docker', 'run', '--rm', '--entrypoint', 'python', image, '-c', check])


def refresh_base():
    from base_manifest import MANIFEST, validate
    metadata = validate(json.loads((ROOT/MANIFEST).read_text()), require_verified=False)
    previous = dict(metadata['images'])
    config = json.loads((ROOT/'resources/bootstrap.json').read_text())
    files = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
             for name in ('Containerfile.base-maintenance', 'scripts/bootstrap.py', 'resources/bootstrap.json')}
    key = hashlib.sha256(json.dumps({'parents': previous, 'files': files}, sort_keys=True).encode()).hexdigest()
    for kind in ('runtime', 'builder'):
        tag = BASE_IMAGE+':'+kind+'-maintenance-'+key
        digest = registry_digest(tag)
        if digest is None:
            run(['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--push',
                 '--file', 'Containerfile.base-maintenance', '--tag', tag,
                 '--build-arg', 'BASE_IMAGE='+previous[kind], '--build-arg', 'BOOTSTRAP_KIND='+kind, '.'])
            digest = registry_digest(tag)
            if digest is None:
                raise RuntimeError('Maintenance base publication is missing')
        metadata['images'][kind] = BASE_IMAGE+'@'+digest
    metadata.update(key=key, parents=previous, bootstrap=config, security={'status': 'pending'})
    OUT.mkdir(exist_ok=True)
    (OUT/'base-images.json').write_text(json.dumps(metadata, indent=2)+'\n')
    (OUT/'lock.json').write_text(json.dumps({**metadata['inputs'], **metadata['verification_inputs'],
        'bootstrap': config, 'base_parents': previous}, indent=2)+'\n')


def prepare_base():
    from base_manifest import MANIFEST, validate
    metadata = validate(json.loads((ROOT/MANIFEST).read_text()), require_verified=False)
    lock = {**metadata['inputs'], **metadata['verification_inputs'],
            'bootstrap': metadata['bootstrap'], 'base_parents': metadata.get('parents', {})}
    OUT.mkdir(exist_ok=True)
    (OUT/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    # Exact selected digests only. No resolver, source comparison or builder call.
    (OUT/'base-images.json').write_text(json.dumps(metadata, indent=2)+'\n')


def scan_base():
    global OUT
    from base_manifest import validate, policy_hash
    root = OUT
    lock = load()
    metadata = validate(json.loads((OUT/'base-images.json').read_text()), require_verified=False)
    cache = 'base-scan-'+secrets.token_hex(4)
    run(['docker', 'volume', 'create', cache])
    reports, failed = {}, []
    try:
        for kind in ('runtime', 'builder'):
            image = metadata['images'][kind]
            OUT = root/(kind+'-reports')
            OUT.mkdir(exist_ok=True)
            (OUT/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
            inventory = dict(metadata['inputs'])
            for key, path in (('os_backport_provenance', '/opt/os-backports/provenance.json'),
                              ('python_expat_provenance', '/opt/os-backports/pyexpat/provenance.json')):
                inventory[key] = json.loads(subprocess.check_output(
                    ['docker', 'run', '--rm', '--entrypoint', 'cat', image, path]))
            (OUT/'image-lock.json').write_text(json.dumps(inventory, indent=2)+'\n')
            smoke_base(image, kind)
            capture_installed(image, inventory, lock)
            run(['docker', 'run', '--rm', '--entrypoint', 'python',
                 '-v', f'{ROOT}/tests/os_test.py:/tmp/os_test.py:ro', image, '/tmp/os_test.py'])
            run(['docker', 'save', '--output', str(OUT/'image.tar'), image])
            try:
                scan_with_cache(cache, stage=kind)
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
                            'trivy_image': lock['trivy_image'], 'grype_image': lock['grype_image']}
    metadata['source_revision'] = os.environ['GITHUB_SHA']
    metadata['evidence'] = ('https://github.com/'+os.environ['GITHUB_REPOSITORY']+
                            '/actions/runs/'+os.environ['GITHUB_RUN_ID'])
    (OUT/'base-images.json').write_text(json.dumps(metadata, indent=2)+'\n')


def scan():
    cache = 'erpnext-scan-'+secrets.token_hex(4)
    run(['docker', 'volume', 'create', cache])
    try:
        scan_with_cache(cache)
    finally:
        run(['docker', 'volume', 'rm', cache])


def scan_with_cache(cache, stage='runtime'):
    lock = load()
    trivy = ['docker', 'run', '--rm', '-v', f'{OUT}:/scan', '-v', f'{ROOT}:/source:ro',
             '-v', f'{cache}:/root/.cache/trivy',
             lock['trivy_image']]
    run(trivy+['image', '--input', '/scan/image.tar', '--format', 'cyclonedx',
               '--output', '/scan/sbom.cdx.json'])
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
    source_proofs = []
    vendor_proofs = []
    builder_policy = json.loads((ROOT/'security-builder-advisories.json').read_text())
    advisories = (builder_policy['records'] if stage == 'builder'
        and lock.get('base_parents', {}).get('builder') == builder_policy['image'] else ())
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
            if filename == 'vulnerabilities.json':
                from source_fixes import verify, tracker_data, TRACKER
                tracker, tracker_sha = tracker_data()
                source_proofs = verify(data, json.loads((OUT/'image-lock.json').read_text()),
                    json.loads((OUT/'installed-packages.json').read_text()),
                    json.loads((ROOT/'security-remediations.json').read_text()), tracker)
                from pip_vendor import verify_finding
                vendor_image = (json.loads((OUT.parent/'base-images.json').read_text())['images'][stage]
                                if stage in ('builder', 'runtime') and OUT.name.endswith('-reports') else IMAGE)
                raw_proof = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'python',
                    '-v', f'{ROOT}/scripts/pip_vendor.py:/tmp/pip_vendor.py:ro', vendor_image, '/tmp/pip_vendor.py'])
                vendor_proofs = [verify_finding(json.loads(raw_proof), lock,
                    json.loads((ROOT/'security-remediations.json').read_text()))]
                (OUT/'vendor-assessment.json').write_text(json.dumps(vendor_proofs, indent=2)+'\n')
                (OUT/'source-fixes.json').write_text(json.dumps(
                    {'authority': TRACKER, 'tracker_sha256': tracker_sha, 'fixes': source_proofs}, indent=2)+'\n')
            assessment = assess_report(data,
                policy['application'] if filename == 'vulnerabilities.json' else (),
                advisory=filename == 'service-mariadb-security.json',
                verified_fixed=[proof['finding'] for proof in source_proofs] if filename == 'vulnerabilities.json' else (),
                verified_not_affected=[proof['finding'] for proof in vendor_proofs] if filename == 'vulnerabilities.json' else (),
                advisories=advisories if filename == 'vulnerabilities.json' else ())
            assessments[filename] = assessment
            failed |= bool(assessment['blocking'])
        except (ValueError, KeyError, OSError, TypeError) as error:
            failed = True
            assessments[filename] = {'report_error': str(error)}
    from chromium import component
    sbom = json.loads((OUT/'sbom.cdx.json').read_text())
    chrome_component = component(lock['chromium'])
    if not any(c.get('purl') == chrome_component['purl'] for c in sbom.setdefault('components', [])):
        sbom['components'].append(chrome_component)
    sbom.setdefault('metadata', {}).setdefault('properties', []).append(
        {'name': 'container:verified-source-fixes', 'value': json.dumps(source_proofs, sort_keys=True)})
    sbom['metadata']['properties'].append({'name': 'container:verified-not-affected',
        'value': json.dumps(vendor_proofs, sort_keys=True)})
    verified_sbom = OUT/'sbom.verified.cdx.json'
    verified_sbom.write_text(json.dumps(sbom, indent=2)+'\n')
    verified_sbom.replace(OUT/'sbom.cdx.json')
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
    {'config': config, 'base': base, 'build': build, 'integration': integration, 'scan': scan, 'scan-base': scan_base, 'prepare-base': prepare_base, 'refresh-base': refresh_base}[sys.argv[1]]()
