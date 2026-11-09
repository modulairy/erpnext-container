import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline


class BaseImageTests(unittest.TestCase):
    def setUp(self):
        self.lock = {name: name+'-locked' for name in pipeline.BASE_FIELDS}
        self.lock.update(bootstrap={}, apps=[{'sha': 'app-a'}], bench='5.0', resolved_at='first')
        self.digest = 'sha256:'+'a'*64

    def test_app_and_bench_updates_reuse_base(self):
        changed = copy.deepcopy(self.lock)
        changed.update(apps=[{'sha': 'app-b'}], bench='6.0', resolved_at='second')
        self.assertEqual(pipeline.base_key(self.lock), pipeline.base_key(changed))

    def test_every_base_input_invalidates_cache(self):
        for name in pipeline.BASE_FIELDS:
            changed = copy.deepcopy(self.lock)
            changed[name] = 'new-input'
            self.assertNotEqual(pipeline.base_key(self.lock), pipeline.base_key(changed), name)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'scripts').mkdir()
            (root/'Containerfile.base').write_text('base')
            (root/'scripts/os_backports.py').write_text('backports')
            (root/'scripts/chromium.py').write_text('chromium')
            (root/'scripts/python_expat.py').write_text('python XML modules')
            (root/'scripts/bootstrap.py').write_text('bootstrap')
            (root/'resources').mkdir()
            (root/'resources/bootstrap.json').write_text('{}')
            with patch.object(pipeline, 'ROOT', root):
                first = pipeline.base_key(self.lock)
                (root/'scripts/os_backports.py').write_text('updated backports')
                self.assertNotEqual(first, pipeline.base_key(self.lock))

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
            with patch.object(pipeline, 'base_key', side_effect=AssertionError('No source check permitted')):
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

    def test_registry_auth_errors_do_not_trigger_rebuild(self):
        for message in ('unauthorized: authentication required', 'connection timed out'):
            result = subprocess.CompletedProcess([], 1, '', message)
            with patch.object(pipeline.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError):
                    pipeline.registry_digest('base:tag')

    def test_registry_missing_and_pinned_manifest(self):
        result = subprocess.CompletedProcess([], 1, '', 'manifest unknown: not found')
        with patch.object(pipeline.subprocess, 'run', return_value=result):
            self.assertIsNone(pipeline.registry_digest('base:tag'))
        result = subprocess.CompletedProcess([], 0, json.dumps(self.digest), '')
        with patch.object(pipeline.subprocess, 'run', return_value=result):
            self.assertEqual(pipeline.registry_digest('base:tag'), self.digest)
