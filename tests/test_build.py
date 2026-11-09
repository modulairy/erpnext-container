import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from build import prune_build_tools


class BuildTests(unittest.TestCase):
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

