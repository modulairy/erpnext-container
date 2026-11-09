import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline
from pip_vendor import verify_finding


class SecurityProofTests(unittest.TestCase):
    def test_vendor_proof_rejects_old_standalone_setuptools_or_mismatched_content(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root/'resources/bootstrap.json').read_text())
        policy = json.loads((root/'security-remediations.json').read_text())
        proof = {'config': config, 'wheel_sha256': config['pip']['sha256'],
                 'pip_version': config['pip']['version'], 'affected_module_present': False,
                 'standalone_setuptools': []}
        self.assertEqual(verify_finding(proof, {'bootstrap': config}, policy)['finding']['cve'], 'CVE-2025-47273')
        for key, value in (('wheel_sha256','f'*64), ('affected_module_present',True),
                           ('standalone_setuptools',['70.3.0']), ('pip_version','26.2.2')):
            changed = copy.deepcopy(proof)
            changed[key] = value
            with self.assertRaises(ValueError):
                verify_finding(changed, {'bootstrap':config}, policy)

    def test_builder_advisories_are_exact_and_never_exempt_secrets_or_new_cves(self):
        root = Path(__file__).resolve().parents[1]
        records = json.loads((root/'security-builder-advisories.json').read_text())['records']
        self.assertEqual(len(records), 68)
        row = records[0]
        finding = {'VulnerabilityID':row['cve'],'PkgName':row['package'],
                   'InstalledVersion':row['version'],'Severity':row['severity']}
        report = {'SchemaVersion':2,'Results':[{'Class':row['classification'],'Type':row['type'],
                   'Vulnerabilities':[finding], 'Secrets':[{'Severity':'HIGH'}]}]}
        result = pipeline.assess_report(report, advisories=records)
        self.assertEqual(len(result['advisory']),1)
        self.assertEqual(len(result['blocking']),1)
        finding['VulnerabilityID'] = 'CVE-2026-999999'
        self.assertEqual(len(pipeline.assess_report(report, advisories=records)['blocking']),2)
        self.assertEqual(len(pipeline.assess_report(report)['advisory']),0)
