import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import source_fixes
from pipeline import assess_report


class VerifiedSourceFixTests(unittest.TestCase):
    def setUp(self):
        self.version = '2:1.8.4-1'
        self.cve = 'CVE-2026-109089'
        self.source = {'version': self.version, 'files': {'libxi.dsc': 'a'*64}}
        self.lock = {'os_backports': {'libxi': copy.deepcopy(self.source)},
                     'os_backport_provenance': {'libxi': {'source': copy.deepcopy(self.source),
                     'tests_disabled': False, 'binaries': {'libxi6': {'version': self.version, 'sha256': 'b'*64}}}}}
        self.installed = {'versions': {'libxi6': self.version}, 'verified_packages': ['libxi6']}
        self.policy = {'schema': 1, 'fixes': [{'source': 'libxi', 'cve': self.cve,
            'fixed_version': self.version, 'url': 'https://security-tracker.debian.org/tracker/'+self.cve}]}
        self.tracker = {'libxi': {self.cve: {'releases': {'sid': {'status': 'resolved', 'fixed_version': self.version}}}}}
        self.report = {'SchemaVersion': 2, 'Results': [{'Type': 'debian', 'Class': 'os-pkgs',
            'Vulnerabilities': [{'VulnerabilityID': self.cve, 'PkgName': 'libxi6', 'SrcName': 'libxi',
                                'InstalledVersion': self.version, 'Severity': 'HIGH'}]}]}

    def verify(self):
        with patch.object(source_fixes, 'version_at_least', return_value=True):
            return source_fixes.verify(self.report, self.lock, self.installed, self.policy, self.tracker)

    def test_exact_source_and_installed_integrity_are_required(self):
        self.assertEqual(len(self.verify()), 1)
        original = copy.deepcopy(self.lock)
        for mutation in ('source', 'tests', 'version', 'integrity'):
            self.lock = copy.deepcopy(original)
            self.installed = {'versions': {'libxi6': self.version}, 'verified_packages': ['libxi6']}
            if mutation == 'source':
                self.lock['os_backport_provenance']['libxi']['source']['files']['libxi.dsc'] = 'c'*64
            elif mutation == 'tests':
                self.lock['os_backport_provenance']['libxi']['tests_disabled'] = True
            elif mutation == 'version':
                self.installed['versions']['libxi6'] = '2:1.8.2-1'
            else:
                self.installed['verified_packages'] = []
            self.assertEqual(self.verify(), [], mutation)

    def test_new_cve_and_secrets_remain_blocking(self):
        proven = [p['finding'] for p in self.verify()]
        self.report['Results'][0]['Vulnerabilities'].append({
            'VulnerabilityID': 'CVE-2026-999999', 'PkgName': 'libxi6', 'InstalledVersion': self.version, 'Severity': 'HIGH'})
        self.report['Results'][0]['Secrets'] = [{'Severity': 'HIGH'}]
        result = assess_report(self.report, verified_fixed=proven)
        self.assertEqual(len(result['verified_fixed']), 1)
        self.assertEqual(len(result['blocking']), 2)
        self.assertEqual(result['accepted'], [])

    def test_changed_official_record_fails_closed(self):
        self.tracker['libxi'][self.cve]['releases']['sid']['status'] = 'open'
        with self.assertRaises(ValueError):
            self.verify()

    def test_older_source_does_not_pass(self):
        with patch.object(source_fixes, 'version_at_least', return_value=False):
            self.assertEqual(source_fixes.verify(self.report, self.lock, self.installed, self.policy, self.tracker), [])

    def test_expat_requires_both_installed_xml_modules(self):
        self.lock = {k: {('expat' if n == 'libxi' else n): v for n,v in d.items()}
                     for k,d in self.lock.items()}
        self.policy['fixes'][0]['source'] = 'expat'
        self.tracker['expat'] = self.tracker.pop('libxi')
        self.report['Results'][0]['Vulnerabilities'][0]['SrcName'] = 'expat'
        self.assertEqual(self.verify(), [])
        sources = {'pyexpat.c': {'sha256': 'c'*64}}
        self.lock['pyexpat_sources'] = sources
        self.lock['python_expat_provenance'] = {
            'tests_passed': True, 'sources': sources, 'expat_version': 'expat_2.10.0',
            'binaries': {n: {'sha256': 'd'*64} for n in ('pyexpat', '_elementtree')}}
        self.installed['python_xml'] = {'version': 'expat_2.10.0',
            'binaries': {n: 'd'*64 for n in ('pyexpat', '_elementtree')}}
        self.assertEqual(len(self.verify()), 1)
        self.installed['python_xml']['binaries']['_elementtree'] = 'e'*64
        self.assertEqual(self.verify(), [])
