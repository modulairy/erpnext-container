import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'resources'))
from http_cache_config import configure, redis_connection


class CacheConfigTests(unittest.TestCase):
    def test_disabled_needs_no_site_or_credentials(self):
        self.assertEqual(configure(Path('/missing'), {}), {'active': False, 'sites': {}})

    def test_connection_decodes_acl_and_requires_verified_tls_scheme(self):
        row = redis_connection('rediss://cache-user:encoded%40password@redis.example:6380/3')
        self.assertEqual((row['username'], row['password'], row['database'], row['tls']),
                         ('cache-user', 'encoded@password', 3, True))
        for url in ('http://redis/0', 'redis:///0', 'redis://redis/bad', 'rediss://redis/0?ssl_cert_reqs=none'):
            with self.assertRaises(ValueError):
                redis_connection(url)

    def test_precedence_site_isolation_and_image_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'image-lock.json').write_text('{}')
            (root/'sites/one.local').mkdir(parents=True)
            (root/'sites/two.local').mkdir()
            (root/'sites/common_site_config.json').write_text('{"redis_cache":"redis://global:6379/0"}')
            (root/'sites/one.local/site_config.json').write_text('{"redis_cache":"redis://site:6379/1"}')
            (root/'sites/two.local/site_config.json').write_text('{}')
            env = {'HTTP_REDIS_CACHE_ACTIVE': 'true'}
            config = configure(root, env)
            self.assertEqual(config['sites']['one.local']['host'], 'site')
            self.assertEqual(config['sites']['two.local']['host'], 'global')
            override = configure(root, dict(env, HTTP_REDIS_CACHE_URL='redis://override:6379/2'))
            self.assertTrue(all(row['host'] == 'override' for row in override['sites'].values()))
            self.assertNotEqual(config['prefix'], configure(root, dict(env, HTTP_REDIS_CACHE_GENERATION='2'))['prefix'])
            (root/'image-lock.json').write_text('{"new":"image"}')
            self.assertNotEqual(config['prefix'], configure(root, env)['prefix'])
            with self.assertRaises(ValueError):
                configure(root, dict(env, HTTP_REDIS_CACHE_TIMEOUT_MS='0'))
            with self.assertRaises(ValueError):
                configure(root, dict(env, HTTP_REDIS_CACHE_PREFIX='bad\nconfig'))


if __name__ == '__main__':
    unittest.main()
