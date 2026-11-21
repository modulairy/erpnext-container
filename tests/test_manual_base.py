import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import base_manifest
import resolve
import pipeline


def approved():
    return {'schema': 4,
            'verification_inputs': {k: 'locked' for k in ('trivy_image','grype_image','mariadb_image','redis_image')},
            'images': {kind: base_manifest.PUBLISHED_IMAGE+'@sha256:'+'a'*64 for kind in ('runtime', 'builder')},
            'parents': {kind: base_manifest.OFFICIAL_IMAGES[kind]+':version-16@sha256:'+'a'*64
                                    for kind in ('runtime', 'builder')},
            'inputs': {**{name: name+'-locked' for name in base_manifest.BASE_FIELDS},
                       'python': '3.14.10', 'node': '24.22.2'},
            'security': {'status': 'passed', 'severity': ['HIGH', 'CRITICAL'],
                         'policy_sha256': base_manifest.policy_hash(),
                         'reports': {'runtime': 'b'*64, 'builder': 'c'*64},
                         'trivy_image': 'trivy@sha256:'+'d'*64, 'grype_image': 'grype@sha256:'+'e'*64}}


class ManualBaseTests(unittest.TestCase):
    def test_app_resolution_uses_ready_base_without_registry_or_rebuild(self):
        data = approved()
        rows = [{'python': '>=3.14,<3.15', 'node': ['>=24 <25']}]
        with tempfile.TemporaryDirectory() as directory, patch.object(base_manifest, 'load', return_value=data), \
             patch.object(resolve, 'request', side_effect=AssertionError('No base discovery permitted')), \
             patch.object(resolve, 'image_digest', side_effect=AssertionError('No base registry lookup permitted')):
            inputs, verification = resolve.runtime_inputs(rows, Path(directory))
            self.assertEqual(inputs, data['inputs'])
            self.assertEqual(verification, data['verification_inputs'])
            self.assertEqual(json.loads((Path(directory)/'base-images.json').read_text()), data)

    def test_incompatible_runtime_requires_manual_update(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(base_manifest, 'load', return_value=approved()):
            with self.assertRaisesRegex(ValueError, 'do not match the approved base'):
                resolve.runtime_inputs([{'python': '>=3.15', 'node': []}], Path(directory))

    def test_missing_manifest_never_creates_base(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'manually'):
                base_manifest.load(Path(directory)/'missing.json')

    def test_unverified_or_different_policy_is_rejected(self):
        for change in ('status', 'policy_sha256', 'severity'):
            data = approved()
            data['security'][change] = 'changed'
            with self.assertRaises(ValueError):
                base_manifest.validate(data)

    def test_promotion_refuses_concurrent_main_update_before_writing(self):
        writes = []
        def api(method, path, payload=None):
            if method != 'GET':
                writes.append((method, path))
            return {'object': {'sha': 'b'*40}}
        with self.assertRaisesRegex(ValueError, 'Main changed'):
            base_manifest.promote(approved(), 'modulairy/erpnext-container', 'a'*40, api)
        self.assertEqual(writes, [])

    def test_promotion_only_updates_manifest_and_never_force_pushes(self):
        data, writes = approved(), []
        def api(method, path, payload=None):
            if method != 'GET':
                writes.append((method, path, payload))
            if '/contents/' in path:
                return {'content': base64.b64encode(json.dumps(data).encode()).decode()}
            if path.endswith('/git/ref/heads/main'):
                return {'object': {'sha': 'a'*40}}
            if '/git/commits/' in path:
                return {'tree': {'sha': 'b'*40}}
            return {'sha': 'c'*40}
        base_manifest.promote(data, 'modulairy/erpnext-container', 'a'*40, api)
        self.assertEqual([x['path'] for x in writes[0][2]['tree']], ['resources/base-images.json'])
        self.assertEqual(writes[1][2]['parents'], ['a'*40])
        self.assertEqual(writes[2][2], {'sha': 'c'*40, 'force': False})

    def test_base_only_accepts_the_official_repo_for_each_stage(self):
        data = approved()
        data['parents']['runtime'] = data['parents']['builder']
        with self.assertRaisesRegex(ValueError, 'official Frappe'):
            base_manifest.validate(data)

    def test_chrome_base_rejects_unrelated_package_changes(self):
        before = {'chromium-headless-shell': '156', 'chromium-common': '156', 'git': '1'}
        after = {'libatk-bridge2.0-0t64': '2.56.2-1+deb13u2', 'git': '1'}
        pipeline.verify_chrome_package_delta(before, after)
        with self.assertRaisesRegex(ValueError, 'Unexpected Debian package'):
            pipeline.verify_chrome_package_delta(before, {**after, 'git': '2'})

    def test_local_base_cannot_be_published_without_passing_scans(self):
        data = approved()
        data['images'] = dict(base_manifest.LOCAL_IMAGES)
        data.pop('security')
        env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': 'modulairy/erpnext-container'}
        with tempfile.TemporaryDirectory() as directory, patch.object(pipeline, 'OUT', Path(directory)), \
             patch.dict(pipeline.os.environ, env), patch.object(pipeline, 'run') as docker:
            (Path(directory)/'base-images.json').write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'pass both scans'):
                pipeline.publish_base()
            docker.assert_not_called()
