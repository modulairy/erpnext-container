"""Build a private Nginx Redis configuration from ENV and known Frappe sites."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def integer(env, name, default, minimum, maximum):
    value = int(env.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(name+' is outside its supported range')
    return value


def redis_connection(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('redis', 'rediss') or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError('HTTP cache requires a redis:// or rediss:// URL without query/fragment')
    database = parsed.path.lstrip('/') or '0'
    if not database.isdecimal():
        raise ValueError('Invalid HTTP cache Redis database')
    return {'host': parsed.hostname, 'port': parsed.port or 6379, 'database': int(database),
            'username': unquote(parsed.username or ''), 'password': unquote(parsed.password or ''),
            'tls': parsed.scheme == 'rediss'}


def configure(bench, env):
    active = env.get('HTTP_REDIS_CACHE_ACTIVE', 'false').lower()
    if active not in ('true', 'false', '1', '0'):
        raise ValueError('HTTP_REDIS_CACHE_ACTIVE must be true or false')
    result = {'active': active in ('true', '1'), 'sites': {}}
    if not result['active']:
        return result
    result.update(
        static_ttl=integer(env, 'HTTP_REDIS_CACHE_STATIC_TTL', 86400, 1, 2592000),
        public_ttl=integer(env, 'HTTP_REDIS_CACHE_PUBLIC_TTL', 60, 1, 3600),
        max_size=integer(env, 'HTTP_REDIS_CACHE_MAX_BYTES', 16777216, 1024, 67108864),
        timeout=integer(env, 'HTTP_REDIS_CACHE_TIMEOUT_MS', 200, 10, 5000),
        pool_size=integer(env, 'HTTP_REDIS_CACHE_POOL_SIZE', 10, 1, 100))
    prefix = env.get('HTTP_REDIS_CACHE_PREFIX', 'frappe-http')
    generation = env.get('HTTP_REDIS_CACHE_GENERATION', '1')
    if not all(re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value) for value in (prefix, generation)):
        raise ValueError('Invalid HTTP cache prefix/generation')
    result['prefix'] = prefix+':'+hashlib.sha256((bench/'image-lock.json').read_bytes()).hexdigest()[:24]+':'+generation
    sites = bench/'sites'
    common_path = sites/'common_site_config.json'
    common = json.loads(common_path.read_text()) if common_path.exists() else {}
    allow_symlinks = str(env.get('FRAPPE_ALLOW_SYMLINK_SITES', common.get('allow_symlink_sites', False))).lower() in ('true', '1')
    for directory in sites.iterdir():
        if not directory.is_dir() or (directory.is_symlink() and not allow_symlinks) or not re.fullmatch(r'[A-Za-z0-9_.-]+', directory.name):
            continue
        path = directory/'site_config.json'
        if not path.is_file():
            continue
        site = json.loads(path.read_text())
        url = env.get('HTTP_REDIS_CACHE_URL') or site.get('redis_cache') or common.get('redis_cache')
        if url:
            result['sites'][directory.name] = redis_connection(url)
    if not result['sites']:
        raise ValueError('HTTP cache enabled but no configured site has a Redis cache connection')
    return result


def main():
    config = configure(Path('/home/frappe/frappe-bench'), os.environ)
    folder = Path('/etc/nginx')
    path = folder/'http-cache.json'
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(config, handle)
    path.chmod(0o600)
    resolvers = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                 if line.startswith('nameserver ')]
    addresses = []
    for value in resolvers:
        address = ipaddress.ip_address(value)
        addresses.append('['+str(address)+']' if address.version == 6 else str(address))
    if not addresses:
        raise ValueError('No DNS resolver is configured')
    (folder/'conf.d/00-http-cache.conf').write_text('''lua_package_path "/usr/local/share/lua/5.1/?.lua;;";
lua_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
lua_ssl_verify_depth 5;
lua_socket_log_errors off;
init_by_lua_block { require('http_cache').init('/etc/nginx/http-cache.json') }
resolver '''+' '.join(addresses)+''' valid=30s;
resolver_timeout 1s;
map $sent_http_cache_control $http_cache_private { default 0; ~*(private|no-store|no-cache) 1; }
map $sent_http_cache_control $http_cache_public { default 0; "~*(^|,)\\s*public\\s*(,|$)" 1; }
map $sent_http_cache_control $http_cache_age { default 0; ~*(s-maxage|max-age)=[1-9][0-9]* 1; }
map $sent_http_set_cookie $http_cache_cookie { default 1; "" 0; }
map $sent_http_content_encoding $http_cache_encoded { default 1; "" 0; }
map $sent_http_vary $http_cache_vary {
    default 1;
    "" 0;
    ~*^(accept-encoding|accept-language)(,\\s*(accept-encoding|accept-language))*$ 0;
}
map "$http_cache_skip:$http_cache_kind:$http_cache_private:$http_cache_cookie:$http_cache_vary:$http_cache_encoded:$http_cache_public:$http_cache_age" $http_cache_store_skip {
    default 1;
    ~^0:static:0:0:0:0: 0;
    "0:public:0:0:0:0:1:1" 0;
}
''')
    for kind in ('static', 'public'):
        ttl = config.get(kind+'_ttl', 60)
        (folder/('http-cache-'+kind+'.conf')).write_text('''set $http_cache_kind '''+kind+''';
set $http_cache_site "";
set $http_cache_key "";
set $http_cache_query "";
set_by_lua_block $http_cache_skip { return require('http_cache').prepare() }
header_filter_by_lua_block { require('http_cache').headers() }
srcache_fetch_skip $http_cache_skip;
srcache_store_skip $http_cache_store_skip;
srcache_fetch GET /__http_cache_fetch $http_cache_query;
srcache_store PUT /__http_cache_store "$http_cache_query&ttl=$srcache_expire";
srcache_store_statuses 200;
srcache_store_max_size '''+str(config.get('max_size', 16777216))+''';
srcache_default_expire '''+str(ttl)+'''s;
srcache_max_expire '''+str(ttl)+'''s;
srcache_request_cache_control '''+('off' if kind == 'static' else 'on')+''';
''')


if __name__ == '__main__':
    main()
