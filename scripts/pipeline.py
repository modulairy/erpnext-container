"""The same build, test and scan commands are used locally and in Actions."""
import json
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


def build():
    lock = load()
    args = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load',
            '--build-context', f'resolved={OUT}', '--file', 'Containerfile', '--tag', IMAGE]
    values = {'PYTHON_IMAGE': lock['python_image'], 'NODE_IMAGE': lock['node_image'],
              'BENCH_VERSION': lock['bench'], 'YARN_VERSION': lock['yarn'],
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
                          'worker-short', 'worker-default', 'worker-long',
                          'cache-origin', 'cache-frontend', 'cache-fail'],
                         ['run', '--rm', 'verify']):
                run(compose+args, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            for service in ('backend', 'frontend', 'websocket', 'scheduler',
                            'worker-short', 'worker-default', 'worker-long'):
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
    lock = load()
    trivy = ['docker', 'run', '--rm', '-v', f'{OUT}:/scan', '-v', f'{ROOT}:/source:ro',
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
    {'build': build, 'integration': integration, 'scan': scan}[sys.argv[1]]()
