import sys
import json
import gzip
import lzma
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import resolve


def row(name, version, deps=None, required=None):
    return {'name': name, 'version': version, 'dependencies': deps or {}, 'required_apps': required or []}


class ResolverTests(unittest.TestCase):
    def test_curl_backport_requires_common_version_and_sorts_numerically(self):
        packages = ('curl', 'libcurl3t64-gnutls', 'libcurl4-gnutls', 'libcurl4t64')
        index = ''.join(f'Package: {name}\nVersion: 8.{minor}.0-2~bpo13+1\n\n'
                        for name in packages for minor in (9, 21))
        index += 'Package: curl\nVersion: 8.22.0-1~bpo13+1\n\n'
        with patch.object(resolve, 'request', return_value=lzma.compress(index.encode())):
            self.assertEqual(resolve.curl_backport(), '8.21.0-2~bpo13+1')

    def test_mariadb_selects_newest_version_available_for_every_client_package(self):
        packages = ('mariadb-client', 'mariadb-client-core', 'mariadb-common', 'libmariadb3', 'libmariadb-dev')
        index = ''.join(f'Package: {name}\nVersion: 1:11.8.{patch}+maria~deb13\n\n'
                        for name in packages for patch in (8, 9))
        index += 'Package: mariadb-client\nVersion: 1:11.8.10+maria~deb13\n\n'
        key = b'test signing key'
        with tempfile.TemporaryDirectory() as directory, patch.object(
                resolve, 'request', side_effect=[key, gzip.compress(index.encode())]):
            selected = resolve.mariadb_client(Path(directory))
            self.assertEqual(selected['version'], '1:11.8.9+maria~deb13')
            self.assertEqual(selected['key_sha256'], hashlib.sha256(key).hexdigest())
            self.assertEqual((Path(directory)/'mariadb-keyring.asc').read_bytes(), key)

    def test_scanner_registry_ignores_nonstable_tags_and_paginates(self):
        prefix = 'https://hub.docker.com/v2/repositories/aquasec/trivy/tags'
        rows = [{'results': [{'name': '0.75.0'}, {'name': 'canary'}], 'next': prefix+'?page=2'},
                {'results': [{'name': '0.76.1'}, {'name': '0.77.0-rc1'}], 'next': None}]
        with patch.object(resolve, 'request', side_effect=[json.dumps(r).encode() for r in rows]):
            self.assertEqual(resolve.scanner_version(), '0.76.1')

    def test_release_order_ignores_latest_endpoint_and_nonversions(self):
        rows = [{'tag_name': name} for name in ('v15.124.1', 'v16.52.1', 'v13-baseline', 'v17.0.0-beta')]
        rows += [{'tag_name': 'v18.0.0', 'prerelease': True}, {'tag_name': 'v19.0.0', 'draft': True}]
        self.assertEqual([r['tag_name'] for r in resolve.stable_releases(rows)], ['v16.52.1', 'v15.124.1'])

    def test_pagination_reads_beyond_first_page(self):
        with patch.object(resolve, 'api', side_effect=[[{}]*100, [{'last': True}]]) as call:
            self.assertEqual(len(list(resolve.pages('releases'))), 101)
            self.assertEqual(call.call_count, 2)

    def test_framework_constraint_blocks_incompatible_snapshot(self):
        self.assertFalse(resolve.compatible([row('frappe', '16.53.0'),
                         row('payments', '0.0.1', {'frappe': '>=17.0.0-dev'})]))

    def test_bidirectional_constraints(self):
        self.assertFalse(resolve.compatible([row('erpnext', '16.52.1', {'frappe': '>=16.50,<17'}),
                                             row('frappe', '16.49.0')]))

    def test_backtracks_to_compatible_release(self):
        def candidate(app, ref, kind):
            return row(app, ref, {'frappe': '<17'} if app == 'erpnext' else {})
        options = [('frappe', [('17.0.0', 'release'), ('16.53.0', 'release')]),
                   ('erpnext', [('16.52.1', 'release')])]
        with patch.object(resolve, 'candidate', side_effect=candidate):
            self.assertEqual(resolve.solve(options)[0]['version'], '16.53.0')

    def test_no_solution_is_not_partial_success(self):
        with patch.object(resolve, 'candidate', side_effect=[row('frappe', '17.0.0'),
                              row('erpnext', '16.0.0', {'frappe': '<17'})]):
            self.assertIsNone(resolve.solve([('frappe', [('17.0.0', 'release')]),
                                           ('erpnext', [('16.0.0', 'release')])]))

    def test_install_order_respects_required_apps(self):
        rows = [row('webshop', '1.0.0', required=['erpnext', 'payments']),
                row('payments', '0.0.1', {'frappe': '>=16'}),
                row('erpnext', '16.0.0', {'frappe': '>=16'}), row('frappe', '16.0.0')]
        ordered = [r['name'] for r in resolve.install_order(rows)]
        self.assertEqual(ordered[0], 'frappe')
        self.assertEqual(ordered[-1], 'webshop')

    def test_cycles_rejected(self):
        with self.assertRaises(ValueError):
            resolve.install_order([row('a', '1.0', required=['b']), row('b', '1.0', required=['a'])])

    def test_metadata_never_executed(self):
        with self.assertRaises(ValueError):
            resolve.literal('required_apps = dangerous()', 'required_apps')

    def test_snapshot_policy_is_explicit(self):
        self.assertEqual(resolve.SNAPSHOTS,
                         {'telephony': 'develop', 'payments': 'version-16', 'webshop': 'version-16'})
        self.assertNotIn('writer', resolve.APPS)
        self.assertNotIn('drive', resolve.APPS)

    def test_unknown_required_app_blocks_before_build(self):
        resolve.candidate.cache_clear()
        with patch.object(resolve, 'api', return_value={'sha': 'a'*40}), \
             patch.object(resolve, 'source', side_effect=[
                 '[project]\nrequires-python=">=3.14"', 'required_apps = ["private_app"]']):
            with self.assertRaisesRegex(ValueError, 'undeclared app dependency'):
                resolve.candidate('crm', 'v1.0.0', 'release')

    def test_release_source_version_mismatch_blocks(self):
        resolve.candidate.cache_clear()
        with patch.object(resolve, 'api', return_value={'sha': 'a'*40}), \
             patch.object(resolve, 'source', side_effect=[
                 '[project]\nrequires-python=">=3.14"', '', '__version__ = "1.0.1"']):
            with self.assertRaisesRegex(ValueError, 'release/source mismatch'):
                resolve.candidate('crm', 'v1.0.0', 'release')


if __name__ == '__main__':
    unittest.main()
