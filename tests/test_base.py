import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline


class BaseImageTests(unittest.TestCase):
    def test_manual_build_records_pushed_digests_and_source_date(self):
        lock = {name: name+'-locked' for name in pipeline.BASE_FIELDS}
        lock.update(resolved_at='2026-11-09T00:00:00+00:00', bootstrap={},
                    trivy_image='trivy', grype_image='grype', mariadb_image='db', redis_image='redis')
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out/'lock.json').write_text(json.dumps(lock))
            (out/'mariadb-keyring.asc').write_text('test key')
            def run(args):
                if args[:3] == ['docker', 'buildx', 'build']:
                    Path(args[args.index('--metadata-file')+1]).write_text(
                        json.dumps({'containerimage.digest': 'sha256:'+'a'*64}))
            with patch.object(pipeline, 'OUT', out), patch.object(pipeline, 'run', side_effect=run), \
                 patch.object(pipeline, 'smoke_base'), patch.dict('os.environ', {'GITHUB_SHA': 'b'*40}):
                pipeline.base()
            self.assertEqual(json.loads((out/'base/lock.json').read_text())['resolved_at'], lock['resolved_at'])
            self.assertEqual(set(json.loads((out/'base-images.json').read_text())['images']), {'runtime', 'builder'})

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

