"""The same build, test and scan commands are used locally and in Actions."""
import json
import hashlib
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
IMAGE = 'erpnext-community:test'


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def load():
    return json.loads((OUT/'lock.json').read_text())



BASE_IMAGE = 'ghcr.io/modulairy/erpnext-base'
BASE_FIELDS = ('python', 'node', 'python_image', 'node_image', 'yarn',
               'mariadb_client', 'curl_backport', 'os_backports')


def base_key(lock):
    # App releases, Bench and resolved_at must never invalidate OS compilation.
    inputs = {'platform': 'linux/amd64', 'lock': {k: lock[k] for k in BASE_FIELDS},
              'files': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                        for name in ('Containerfile.base', 'scripts/os_backports.py')}}
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
        # These images stop before Bench, and compilers belong only to the builder.
        check = ("import importlib.util,shutil; from pathlib import Path; "
                 "assert importlib.util.find_spec('bench') is None; "
                 "assert Path('/opt/os-backports/provenance.json').is_file(); "
                 "assert shutil.which('node'); assert shutil.which('chromium-headless-shell'); ")
        check += ("assert all(shutil.which(x) for x in ('gcc','npm','yarn'))"
                  if kind == 'builder' else
                  "assert not any(shutil.which(x) for x in ('gcc','npm','yarn'))")
        run(['docker', 'run', '--rm', '--entrypoint', 'python', images[kind], '-c', check])
    data = {'schema': 1, 'key': key, 'images': images, 'rebuilt': rebuilt}
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
    metadata = json.loads((OUT/'base-images.json').read_text())
    if metadata.get('schema') != 1 or metadata.get('key') != base_key(lock):
        raise ValueError('Base image inputs changed; run the base image workflow')
    images = metadata['images']
    for kind in ('runtime', 'builder'):
        if not re.fullmatch(re.escape(BASE_IMAGE)+r'@sha256:[0-9a-f]{64}', images[kind]):
            raise ValueError('Base image must use the approved GHCR package and a digest')
    return images


def build():
    lock = load()
    bases = locked_base_images(lock)
    lock['base_images'] = bases
    (OUT/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    args = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load',
            '--build-context', f'resolved={OUT}', '--file', 'Containerfile', '--tag', IMAGE]
    values = {'RUNTIME_BASE_IMAGE': bases['runtime'], 'BUILDER_BASE_IMAGE': bases['builder'],
              'BENCH_VERSION': lock['bench'],
              'ERPNEXT_TAG': lock['erpnext_tag'],
              'SOURCE_REVISION': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}
    for key, value in values.items():
        args += ['--build-arg', f'{key}={value}']
    run(args+['.'])
    inventory = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'cat', IMAGE,
                                        '/home/frappe/frappe-bench/image-lock.json'])
    (OUT/'image-lock.json').write_bytes(inventory)
    image_id = subprocess.check_output(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], text=True).strip()
    (OUT/'image-id.txt').write_text(image_id)
    run(['docker', 'save', '--output', str(OUT/'image.tar'), IMAGE])


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


def assess_report(data, exceptions=(), advisory=False):
    """Keep raw evidence; exempt only exact approved vulnerability records."""
    if not isinstance(data.get('Results'), list) or data.get('SchemaVersion') != 2:
        raise ValueError('Missing or unsupported scanner results')
    accepted, blocking, reported = [], [], []
    for row in data['Results']:
        for finding in row.get('Vulnerabilities', []):
            key = dict(cve=finding['VulnerabilityID'], package=finding['PkgName'],
                       version=finding['InstalledVersion'], severity=finding['Severity'],
                       type=row['Type'], classification=row['Class'])
            if advisory:
                reported.append(key)
            elif key in exceptions:
                accepted.append(key)
            else:
                blocking.append(key)
        # Service vulnerability acceptance never exempts leaked secrets/config errors.
        for kind in ('Secrets', 'Misconfigurations'):
            blocking.extend(f for f in row.get(kind, [])
                            if f.get('Severity') in ('HIGH', 'CRITICAL')
                            and f.get('Status', 'FAIL') != 'PASS')
    return dict(accepted=accepted, blocking=blocking, advisory=reported)


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
            assessment = assess_report(json.loads(report.read_text(encoding='utf-8')),
                policy['application'] if filename == 'vulnerabilities.json' else (),
                advisory=filename == 'service-mariadb-security.json')
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
    {'base': base, 'build': build, 'integration': integration, 'scan': scan}[sys.argv[1]]()
