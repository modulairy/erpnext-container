import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline
import build


class FrappeProfileTests(unittest.TestCase):
    def fixture(self):
        return {'erpnext_tag': 'v16.54.0', 'apps': [
            {'name': 'frappe', 'ref': 'v16.55.2', 'sha': 'a'*40},
            {'name': 'erpnext', 'ref': 'v16.54.0', 'sha': 'b'*40}],
            'community_patches': {'frappe': {'sha256': 'a'*64}, 'helpdesk': {'sha256': 'b'*64}}}

    def test_frappe_profile_contains_no_other_app(self):
        original = self.fixture()
        selected = pipeline.select_profile(original, 'frappe')
        self.assertEqual([a['name'] for a in selected['apps']], ['frappe'])
        self.assertEqual(selected['frappe_tag'], 'v16.55.2')
        self.assertNotIn('erpnext_tag', selected)
        self.assertEqual(set(selected['community_patches']), {'frappe'})
        self.assertEqual(len(original['apps']), 2)

    def test_unknown_or_ambiguous_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            pipeline.select_profile(self.fixture(), 'unknown')
        lock = self.fixture()
        lock['apps'] = []
        with self.assertRaises(ValueError):
            pipeline.select_profile(lock, 'frappe')

    def test_frappe_builder_rejects_extra_apps_before_execution(self):
        lock = self.fixture()
        lock['profile'] = 'frappe'
        with patch.object(build.Path, 'read_text', return_value=json.dumps(lock)), patch.object(build, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Unexpected application set'):
                build.install()
            run.assert_not_called()
