from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import release

class FrappeReleaseTests(unittest.TestCase):
    def test_frappe_release_has_separate_tag_and_preserves_latest(self):
        revision, digest = 'a'*40, 'sha256:'+'b'*64
        calls = []
        saved = {}
        def api(method, path, payload=None):
            calls.append((method,path,payload))
            if '/git/ref/tags/' in path:
                return {'object': {'sha': revision, 'type': 'commit'}}
            if method == 'POST' and path.endswith('/releases'):
                saved.update(payload, id=1, html_url='https://example.test/release')
                return saved
            if '/releases/tags/' in path:
                return saved or None
            raise AssertionError((method,path))
        with patch.object(release, 'api', side_effect=api):
            result = release.publish({'frappe_tag':'v16.55.2','bench':'5.31.0','apps':[]},
                digest,'modulairy/erpnext-container',revision,1,1,'frappe')
        self.assertEqual(result,'https://example.test/release')
        self.assertEqual(saved['tag_name'],'frappe-v16.55.2')
        self.assertEqual(saved['make_latest'],'false')
        self.assertIn('ghcr.io/modulairy/frappe:v16.55.2',saved['body'])
