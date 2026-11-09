import base64
import copy
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
    return {'schema': 2, 'bootstrap': {},
            'verification_inputs': {k: 'locked' for k in ('trivy_image','grype_image','mariadb_image','redis_image')}, 'images': {kind: base_manifest.BASE_IMAGE+'@sha256:'+'a'*64
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
            inputs, trivy, grype = resolve.runtime_inputs(rows, Path(directory), False)
            self.assertEqual(inputs, {**data['inputs'], 'bootstrap': data['bootstrap']})
            self.assertEqual(trivy, data['security']['trivy_image'])
            self.assertEqual(grype, data['security']['grype_image'])
            self.assertEqual(json.loads((Path(directory)/'base-images.json').read_text()), data)

    def test_incompatible_runtime_requires_manual_update(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(base_manifest, 'load', return_value=approved()):
            with self.assertRaisesRegex(ValueError, 'update the base manually'):
                resolve.runtime_inputs([{'python': '>=3.15', 'node': []}], Path(directory), False)

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

    def test_manual_verify_only_copies_ready_inputs_without_discovery_or_build(self):
        data = approved()
        data['security'] = {'status': 'pending'}
        data['verification_inputs'] = {'trivy_image': 'locked', 'grype_image': 'locked',
                                       'mariadb_image': 'locked', 'redis_image': 'locked'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'resources').mkdir()
            (root/'resources/base-images.json').write_text(json.dumps(data))
            with patch.object(pipeline, 'ROOT', root), patch.object(pipeline, 'OUT', root/'out'), \
                 patch.object(pipeline, 'run', side_effect=AssertionError('No Docker/build calls')), \
                 patch.object(pipeline, 'base_key', side_effect=AssertionError('No source key check')):
                pipeline.prepare_base()
                self.assertEqual(json.loads((root/'out/base-images.json').read_text()), data)
                self.assertEqual(json.loads((root/'out/lock.json').read_text())['python'], '3.14.10')

    def test_integrity_only_allows_configured_missing_documentation(self):
        policy = 'path-exclude /usr/share/doc/*\npath-exclude /usr/share/man/*\npath-include /usr/share/doc/*/copyright\n'
        checked = pipeline.assess_integrity('missing     /usr/share/doc/lib/AUTHORS\n', policy)
        self.assertEqual(checked['intentional_omissions'], ['/usr/share/doc/lib/AUTHORS'])
        self.assertEqual(checked['blocking'], [])
        for output, rules in (
            ('missing     /usr/share/doc/lib/AUTHORS\n', ''),
            ('missing     /usr/lib/libexpat.so\n', 'path-exclude /usr/lib/*'),
            ('??5??????   /usr/share/doc/lib/AUTHORS\n', policy),
            ('missing     /usr/share/doc/lib/copyright\n', policy),
        ):
            self.assertTrue(pipeline.assess_integrity(output, rules)['blocking'])
