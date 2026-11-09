import json
from pathlib import Path
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

