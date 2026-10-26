import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from pipeline import assess_report

class GateTests(unittest.TestCase):
    def setUp(self):
        self.key = dict(cve='CVE-2026-1',package='library',version='1',severity='HIGH',type='debian',classification='os-pkgs')
        self.data = {'SchemaVersion':2,'Results':[{'Type':'debian','Class':'os-pkgs','Vulnerabilities':[{'VulnerabilityID':'CVE-2026-1','PkgName':'library','InstalledVersion':'1','Severity':'HIGH'}]}]}
    def test_exact_exception_and_new_findings(self):
        self.assertEqual(len(assess_report(self.data,[self.key])['accepted']),1)
        for field in ('VulnerabilityID','PkgName','InstalledVersion','Severity'):
            changed=copy.deepcopy(self.data)
            changed['Results'][0]['Vulnerabilities'][0][field]='changed'
            self.assertEqual(len(assess_report(changed,[self.key])['blocking']),1)
    def test_service_advisory_does_not_exempt_secrets(self):
        self.data['Results'][0]['Secrets']=[{'Severity':'CRITICAL'}]
        result=assess_report(self.data,advisory=True)
        self.assertEqual(len(result['advisory']),1)
        self.assertEqual(len(result['blocking']),1)
    def test_invalid_report_fails(self):
        for report in ({},{'SchemaVersion':1,'Results':[]},{'SchemaVersion':2}):
            with self.assertRaises(ValueError): assess_report(report)
    def test_redis_and_config_remain_blocking(self):
        self.assertEqual(len(assess_report(self.data)['blocking']),1)
        self.data['Results'][0]['Misconfigurations']=[{'Severity':'HIGH','Status':'FAIL'}]
        self.assertEqual(len(assess_report(self.data)['blocking']),2)
    def test_exceptions_are_unique_os_records(self):
        data=json.loads((Path(__file__).resolve().parents[1]/'security-exceptions.json').read_text())
        keys=data['application']
        self.assertEqual(len(keys),len({json.dumps(k,sort_keys=True) for k in keys}))
        self.assertTrue(all(k['classification']=='os-pkgs' and k['type']=='debian' for k in keys))
