import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline
from pip_vendor import verify_finding
from test_manual_base import approved


class MaintenanceTests(unittest.TestCase):
    def test_bootstrap_refresh_can_only_build_the_small_maintenance_file(self):
        source = pipeline.ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'resources').mkdir()
            (root/'scripts').mkdir()
            for name in ('Containerfile.base-maintenance', 'scripts/bootstrap.py', 'resources/bootstrap.json'):
                (root/name).write_text((source/name).read_text())
            (root/'resources/base-images.json').write_text(json.dumps(approved()))
            with patch.object(pipeline, 'ROOT', root), patch.object(pipeline, 'OUT', root/'out'), \
                 patch.object(pipeline, 'registry_digest', side_effect=[None, 'sha256:'+'d'*64, None, 'sha256:'+'e'*64]), \
                 patch.object(pipeline, 'run') as run, \
                 patch.object(pipeline, 'base', side_effect=AssertionError('No OS build')), \
                 patch.object(pipeline, 'base_key', side_effect=AssertionError('No OS source checks')):
                pipeline.refresh_base()
                self.assertEqual(run.call_count, 2)
                for call in run.call_args_list:
                    args = call.args[0]
                    self.assertEqual(args[args.index('--file')+1], 'Containerfile.base-maintenance')
                    self.assertNotIn('--build-context', args)

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
