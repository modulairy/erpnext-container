import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import resolve


def row(name, version, deps=None, required=None):
    return {'name': name, 'version': version, 'dependencies': deps or {}, 'required_apps': required or []}


class ResolverTests(unittest.TestCase):
    def test_no_solution_is_not_partial_success(self):
        with patch.object(resolve, 'candidate', side_effect=[row('frappe', '17.0.0'),
                              row('erpnext', '16.0.0', {'frappe': '<17'})]):
            self.assertIsNone(resolve.solve([('frappe', [('17.0.0', 'release')]),
                                           ('erpnext', [('16.0.0', 'release')])]))

    def test_metadata_never_executed(self):
        with self.assertRaises(ValueError):
            resolve.literal('required_apps = dangerous()', 'required_apps')

    def test_release_source_version_mismatch_blocks(self):
        resolve.candidate.cache_clear()
        with patch.object(resolve, 'api', return_value={'sha': 'a'*40}), \
             patch.object(resolve, 'source', side_effect=[
                 '[project]\nrequires-python=">=3.14"', '', '__version__ = "1.0.1"']):
            with self.assertRaisesRegex(ValueError, 'release/source mismatch'):
                resolve.candidate('crm', 'v1.0.0', 'release')


if __name__ == '__main__':
    unittest.main()
