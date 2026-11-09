"""Verify reviewed fixed-source findings without accepting an affected package."""
import hashlib
import json
import re
import subprocess
from urllib.request import urlopen

TRACKER = 'https://security-tracker.debian.org/tracker/data/json'


def tracker_data():
    with urlopen(TRACKER, timeout=120) as response:
        raw = response.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def version_at_least(actual, fixed):
    result = subprocess.run(['dpkg', '--compare-versions', actual, 'ge', fixed], capture_output=True)
    if result.returncode not in (0, 1):
        raise ValueError('Debian version comparison failed')
    return result.returncode == 0


def verify(report, lock, installed, policy, tracker):
    if policy.get('schema') != 1 or not isinstance(policy.get('fixes'), list):
        raise ValueError('Invalid reviewed source-fix policy')
    provenance = lock.get('os_backport_provenance', {})
    rules = {}
    for rule in policy['fixes']:
        cve, name, fixed = rule['cve'], rule['source'], rule['fixed_version']
        if not re.fullmatch(r'CVE-\d{4}-\d+', cve) or rule['url'] != 'https://security-tracker.debian.org/tracker/'+cve:
            raise ValueError('Invalid source-fix authority')
        if cve in rules:
            raise ValueError('Duplicate reviewed source fix')
        current = tracker[name][cve]['releases']['sid']
        if current.get('status') != 'resolved' or current.get('fixed_version') != fixed:
            raise ValueError('Official fixed-source record changed: '+cve)
        rules[cve] = rule
    result = []
    for row in report.get('Results', []):
        if row.get('Type') != 'debian' or row.get('Class') != 'os-pkgs':
            continue
        for finding in row.get('Vulnerabilities', []):
            rule = rules.get(finding['VulnerabilityID'])
            if not rule:
                continue
            name, package = rule['source'], finding['PkgName']
            proof = provenance.get(name)
            if not proof:
                continue
            binaries = {**proof.get('binaries', {}), **proof.get('build_only_binaries', {})}
            if package not in binaries:
                continue
            source = proof['source']
            binary = binaries[package]
            expected = lock['os_backports'].get(name)
            if (proof.get('tests_disabled') is not False or source != expected
                    or installed.get('versions', {}).get(package) != binary['version']
                    or finding['InstalledVersion'] != binary['version']
                    or package not in installed.get('verified_packages', [])
                    or finding.get('SrcName', name) != name):
                continue
            if not source.get('files') or any(not re.fullmatch('[0-9a-f]{64}', h) for h in source['files'].values()):
                raise ValueError('Missing locked source hashes')
            if not re.fullmatch('[0-9a-f]{64}', binary['sha256']):
                raise ValueError('Missing built package checksum')
            if not version_at_least(source['version'], rule['fixed_version']):
                continue
            if upstream := source.get('upstream'):
                actual = source['version'].split(':')[-1].rsplit('-', 1)[0]
                if upstream['version'] != actual:
                    raise ValueError('Replacement upstream differs from the fixed source release')
            if name == 'expat':
                xml = installed.get('python_xml', {})
                xml_proof = lock.get('python_expat_provenance', {})
                if (xml_proof.get('tests_passed') is not True
                        or xml_proof.get('sources') != lock.get('pyexpat_sources')
                        or xml.get('version') != xml_proof.get('expat_version')
                        or set(xml_proof.get('binaries', {})) != {'pyexpat', '_elementtree'}
                        or any(xml.get('binaries', {}).get(module) != data['sha256']
                               for module, data in xml_proof['binaries'].items())):
                    continue
            key = dict(cve=finding['VulnerabilityID'], package=package, version=finding['InstalledVersion'],
                       severity=finding['Severity'], type=row['Type'], classification=row['Class'])
            result.append({'finding': key, 'authority': rule['url'], 'source': name,
                           'source_version': source['version'], 'fixed_version': rule['fixed_version'],
                           'source_sha256': source['files'], 'binary_sha256': binary['sha256'],
                           'installed_package_integrity_verified': True})
    return result
