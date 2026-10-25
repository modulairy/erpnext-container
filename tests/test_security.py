import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from security import overlay, prune_build_tools, verify_overrides


class SecurityTests(unittest.TestCase):
    def test_app_compatibility_range_does_not_weaken_other_app_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            bench = Path(directory)
            app = bench/'apps/helpdesk'
            app.mkdir(parents=True)
            (app/'pyproject.toml').write_text('[project]\ndependencies = []\n')
            (app/'package.json').write_text('{"devDependencies":{"vite":"^5.4.21"}}')
            overrides = {'python': {}, 'javascript': {'vite': '8.3.5'},
                         'javascript_by_app': {'helpdesk': {'vite': '7.3.7'}},
                         'javascript_migrations': {'vite': [5]}}
            overlay(app, overrides)
            self.assertEqual(json.loads((app/'package.json').read_text())['devDependencies']['vite'], '7.3.7')
            installed = app/'node_modules/vite'
            installed.mkdir(parents=True)
            (installed/'package.json').write_text('{"name":"vite","version":"7.3.7"}')
            verify_overrides(bench, overrides)
            other = bench/'apps/crm/node_modules/vite'
            other.mkdir(parents=True)
            (other/'package.json').write_text('{"name":"vite","version":"7.3.7"}')
            with self.assertRaisesRegex(ValueError, 'not installed'):
                verify_overrides(bench, overrides)

    def test_overlay_changes_installed_requirements_and_build_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[project]\ndependencies = ["Pillow~=12.3.0"]\n')
            (root/'package.json').write_text('{"resolutions":{"sortablejs":"^1.15.7"}}')
            (root/'yarn.lock').write_text('dompurify@^3.0.0:\n  version "3.0.0"\n')
            changes = overlay(root, {'python': {'Pillow': '13.0.0'},
                                     'javascript': {'dompurify': '3.4.17'}})
            self.assertIn('Pillow==13.0.0', (root/'pyproject.toml').read_text())
            self.assertEqual(json.loads((root/'package.json').read_text())['resolutions'],
                             {'sortablejs': '^1.15.7', 'dompurify': '3.4.17'})
            self.assertEqual(len(changes), 3)
            self.assertFalse((root/'yarn.lock').exists())

    def test_policy_does_not_silently_downgrade_new_upstream_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[project]\ndependencies = ["Pillow>=14"]\n')
            with self.assertRaises(ValueError):
                overlay(root, {'python': {'Pillow': '13.0.0'}, 'javascript': {}})

    def test_pruning_preserves_frontend_and_realtime_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root/'apps/frappe/node_modules'
            for name in ('esbuild-linux-64/bin', '@esbuild/linux-x64/bin', 'dompurify', 'socket.io'):
                (modules/name).mkdir(parents=True)
                (modules/name/'fixture').write_text('test')
            removed = prune_build_tools(root)
            self.assertFalse((modules/'esbuild-linux-64').exists())
            self.assertFalse((modules/'@esbuild').exists())
            self.assertTrue((modules/'dompurify/fixture').exists())
            self.assertTrue((modules/'socket.io/fixture').exists())
            self.assertEqual(len(removed), 2)

    def test_javascript_major_change_requires_policy_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[project]\ndependencies = []\n')
            (root/'package.json').write_text('{"dependencies":{"dompurify":"^4.0.0"}}')
            with self.assertRaises(ValueError):
                overlay(root, {'python': {}, 'javascript': {'dompurify': '3.4.17'}})

    def test_javascript_major_only_requirement_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[project]\ndependencies = []\n')
            (root/'package.json').write_text('{"devDependencies":{"postcss":"8"}}')
            overlay(root, {'python': {}, 'javascript': {'postcss': '8.5.31'}})
            self.assertEqual(json.loads((root/'package.json').read_text())['resolutions']['postcss'], '8.5.31')
            self.assertEqual(json.loads((root/'package.json').read_text())['devDependencies']['postcss'], '8.5.31')

    def test_explicit_major_migration_and_installed_version_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[project]\ndependencies = []\n')
            (root/'package.json').write_text('{"devDependencies":{"vite":"^5.4.21"}}')
            overrides = {'python': {}, 'javascript': {'vite': '8.3.5'},
                         'javascript_migrations': {'vite': [5]}}
            overlay(root, overrides)
            self.assertEqual(json.loads((root/'package.json').read_text())['devDependencies']['vite'], '8.3.5')
            installed = root/'node_modules/vite'
            installed.mkdir(parents=True)
            (installed/'package.json').write_text('{"name":"vite","version":"5.4.21"}')
            with self.assertRaisesRegex(ValueError, 'not installed'):
                verify_overrides(root, overrides)
            (installed/'package.json').write_text('{"name":"vite","version":"8.3.5"}')
            verify_overrides(root, overrides)

    def test_unrelated_security_packages_are_not_added(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[project]\ndependencies = []\n')
            (root/'package.json').write_text('{"dependencies":{"other":"1.0.0"}}')
            changes = overlay(root, {'python': {}, 'javascript': {'postcss': '8.5.31'}})
            self.assertEqual(changes, [])
            self.assertNotIn('resolutions', json.loads((root/'package.json').read_text()))
