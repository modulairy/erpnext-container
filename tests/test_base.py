import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline


class BaseImageTests(unittest.TestCase):
    def test_validation_build_never_pushes_and_local_ids_cannot_be_promoted(self):
        from base_manifest import validate
        from test_manual_base import approved
        with tempfile.TemporaryDirectory() as directory, patch.object(pipeline, 'OUT', Path(directory)), \
             patch.dict('os.environ', {'BASE_PUBLISH': 'false', 'GITHUB_SHA': 'b'*40}), \
             patch.object(pipeline, 'run') as run, patch.object(pipeline, 'smoke_base'), \
             patch.object(pipeline.subprocess, 'check_output', return_value='sha256:'+'a'*64):
            lock = {name: name+'-locked' for name in pipeline.BASE_FIELDS}
            lock.update(resolved_at='2026-11-09T00:00:00+00:00', bootstrap={},
                        trivy_image='trivy', grype_image='grype', mariadb_image='db', redis_image='redis')
            (Path(directory)/'lock.json').write_text(json.dumps(lock))
            (Path(directory)/'mariadb-keyring.asc').write_text('test key')
            pipeline.base()
            for call in run.call_args_list:
                self.assertIn('--load', call.args[0])
                self.assertNotIn('--push', call.args[0])
            data = approved()
            data['images'] = json.loads((Path(directory)/'base-images.json').read_text())['images']
            validate(data, require_verified=False, local=True)
            with self.assertRaises(ValueError):
                validate(data)

    def setUp(self):
        self.lock = {name: name+'-locked' for name in pipeline.BASE_FIELDS}
        self.lock.update(bootstrap={}, apps=[{'sha': 'app-a'}], bench='5.0', resolved_at='first')
        self.digest = 'sha256:'+'a'*64

    def test_consumer_uses_approved_digest_without_source_hash_check(self):
        from base_manifest import policy_hash
        with tempfile.TemporaryDirectory() as directory, patch.object(pipeline, 'OUT', Path(directory)):
            metadata = {'schema': 2, 'key': 'previously-approved-source-key', 'bootstrap': {},
                        'inputs': {name: self.lock[name] for name in pipeline.BASE_FIELDS},
                        'images': {kind: pipeline.BASE_IMAGE+'@'+self.digest for kind in ('runtime', 'builder')},
                        'security': {'status': 'passed', 'severity': ['HIGH', 'CRITICAL'],
                                     'policy_sha256': policy_hash(),
                                     'reports': {'runtime': 'a'*64, 'builder': 'b'*64}}}
            path = Path(directory)/'base-images.json'
            path.write_text(json.dumps(metadata))
            self.assertEqual(pipeline.locked_base_images(self.lock), metadata['images'])
            metadata['images']['runtime'] = pipeline.BASE_IMAGE+':latest'
            path.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                pipeline.locked_base_images(self.lock)
            metadata['images']['runtime'] = pipeline.BASE_IMAGE+'@'+self.digest
            metadata['security']['status'] = 'failed'
            path.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                pipeline.locked_base_images(self.lock)

