"""Exercise the real Nginx/Redis cache, including hard refresh and privacy boundaries."""
from pathlib import Path
import time
import requests


def verify_cache(base='http://cache-frontend:8080', failed='http://cache-fail:8080'):
    def get(path, host='test.local', headers=None, server=base):
        return requests.get(server+path, headers={'Host': host, **(headers or {})}, timeout=10)
    deadline = time.monotonic()+60
    while True:
        try:
            response = get('/ready')
            if response.ok:
                break
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError('Cache frontend readiness timeout')
        time.sleep(1)
    path = '/public-cache-test?variant=one'
    first = get(path)
    second = get(path)
    assert first.status_code == second.status_code == 200
    assert second.headers.get('X-HTTP-Redis-Cache') == 'HIT', dict(second.headers)
    assert first.content == second.content
    public_body = first.content
    assert get(path, host='cache-other.local').content != first.content
    from redis import Redis
    assert any(Redis.from_url('redis://redis:6379/3').scan_iter(match='frappe-http:*:cache-other.local:*'))
    assert get('/public-cache-test?variant=two').content != first.content
    assert get(path, headers={'Accept-Language': 'tr'}).content != first.content
    for headers in ({'Authorization': 'Bearer test-only'}, {'Cookie': 'sid=test-only'}):
        response = get(path, headers=headers)
        assert response.headers.get('X-HTTP-Redis-Cache') == 'BYPASS', dict(response.headers)
        assert response.content != first.content
    for path in ('/api/cache-test', '/login', '/private-response', '/set-cookie', '/vary-user'):
        left, right = get(path), get(path)
        assert left.content != right.content, path
        assert right.headers.get('X-HTTP-Redis-Cache') != 'HIT', path
    assert get('/__http_cache_fetch').status_code == 404
    asset = Path('/home/frappe/frappe-bench/sites/test.local/public/files/cache-proof.txt')
    asset.write_text('public asset served from Redis')
    try:
        first = get('/files/cache-proof.txt')
        assert first.content == b'public asset served from Redis'
        asset.unlink()
        second = get('/files/cache-proof.txt', headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
        assert second.headers.get('X-HTTP-Redis-Cache') == 'HIT', dict(second.headers)
        assert second.content == first.content
    finally:
        asset.unlink(missing_ok=True)
    time.sleep(3)
    expired = get('/public-cache-test?variant=one')
    assert expired.content != public_body
    assert expired.headers.get('X-HTTP-Redis-Cache') == 'MISS', dict(expired.headers)
    left, right = get('/failure', server=failed), get('/failure', server=failed)
    assert left.ok and right.ok and left.content != right.content
    assert right.headers.get('X-HTTP-Redis-Cache') == 'MISS'
    assert get('/public-cache-test', host='unknown.local').headers.get('X-HTTP-Redis-Cache') == 'BYPASS'
    print('PASS: Redis HTTP HIT, hard refresh, expiry, site/query/language isolation, auth/private bypass and Redis failure fallback', flush=True)


if __name__ == '__main__':
    verify_cache()
