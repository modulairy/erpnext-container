"""Disposable-site integration checks executed inside the actual built image."""
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
from urllib.parse import quote

SITE = 'test.local'
LOCK = json.loads(Path('image-lock.json').read_text())


def command(*args):
    if subprocess.run(args).returncode:
        # Avoid including ephemeral passwords from CLI arguments in exceptions.
        raise RuntimeError(f'{args[0]} failed')


def setup():
    config = {'db_host': 'db', 'db_port': 3306, 'redis_cache': 'redis://redis:6379/0',
              'redis_queue': 'redis://redis:6379/1', 'redis_socketio': 'redis://redis:6379/1',
              'socketio_port': 9000, 'webserver_host': 'backend', 'webserver_port': 8000, 'default_site': SITE,
              'serve_default_site': True}
    Path('sites/common_site_config.json').write_text(json.dumps(config))
    command('bench', 'new-site', SITE, '--db-root-password', os.environ['DB_PASSWORD'],
            '--admin-password', os.environ['ADMIN_PASSWORD'], '--mariadb-user-host-login-scope', '%')
    # Initialize encryption before any long-lived service caches site configuration.
    from cryptography.fernet import Fernet
    site_config = Path('sites')/SITE/'site_config.json'
    values = json.loads(site_config.read_text())
    values.setdefault('encryption_key', Fernet.generate_key().decode())
    site_config.write_text(json.dumps(values))
    for app in LOCK['apps']:
        if app['name'] != 'frappe':
            command('bench', '--site', SITE, 'install-app', app['name'])
    command('bench', '--site', SITE, 'migrate')
    command('bench', '--site', SITE, 'enable-scheduler')
    other = Path('sites/cache-other.local')
    (other/'public/files').mkdir(parents=True, exist_ok=True)
    (other/'site_config.json').write_text('{"redis_cache":"redis://redis:6379/3"}')
    print('PASS: site creation, all applications installed, migration, scheduler enabled', flush=True)


def verify():
    for package in ('mariadb-client', 'mariadb-client-core', 'mariadb-common', 'libmariadb3'):
        installed = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', package], text=True)
        assert installed == LOCK['mariadb_client']['version'], (package, installed)
    print('PASS: locked MariaDB client package versions', flush=True)
    for package in ('curl', 'libcurl3t64-gnutls', 'libcurl4-gnutls', 'libcurl4t64'):
        installed = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', package], text=True)
        assert installed == LOCK['curl_backport'], (package, installed)
    import requests
    import frappe
    from websockets.sync.client import connect
    session = requests.Session()
    session.headers['Host'] = SITE
    base = 'http://frontend:8080'
    deadline = time.monotonic() + 180
    while True:
        try:
            response = session.get(base+'/api/method/ping', timeout=10)
            if response.ok and response.json().get('message') == 'pong':
                break
        except (requests.RequestException, ValueError):
            pass
        if time.monotonic() > deadline:
            raise RuntimeError('HTTP readiness timeout')
        time.sleep(3)
    command('curl', '--fail', '--silent', '--show-error', '--max-time', '30',
            '-H', 'Host: '+SITE, base+'/api/method/ping')
    print('PASS: locked curl packages and HTTP request', flush=True)
    response = session.post(base+'/api/method/login', data={'usr': 'Administrator',
                            'pwd': os.environ['ADMIN_PASSWORD']}, timeout=30)
    response.raise_for_status()
    response = session.get(base+'/api/method/frappe.auth.get_logged_user', timeout=30)
    response.raise_for_status()
    assert response.json()['message'] == 'Administrator'
    print('PASS: HTTP readiness and login', flush=True)
    response = session.get(base+'/app', timeout=30)
    response.raise_for_status()
    assert 'text/html' in response.headers['Content-Type']
    assets = json.loads(Path('sites/assets/assets.json').read_text())
    asset = next(value for value in assets.values() if value.endswith('.js'))
    response = session.get(base+('/' if not asset.startswith('/') else '')+asset, timeout=30)
    response.raise_for_status()
    assert response.content
    assert response.headers.get('X-HTTP-Redis-Cache') == 'BYPASS'
    with tempfile.TemporaryDirectory() as directory:
        pdf = Path(directory)/'test.pdf'
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(pdf)
        assert len(PdfReader(pdf).pages) == 1
        from PIL import Image
        for format in ('PNG', 'JPEG', 'WEBP'):
            path = Path(directory)/('image.'+format.lower())
            Image.new('RGB', (32, 32), 'white').resize((16, 16)).save(path, format=format)
            with Image.open(path) as picture:
                picture.load()
                assert picture.size == (16, 16)
        from filelock import FileLock, Timeout
        lock_path = Path(directory)/'test.lock'
        with FileLock(lock_path):
            try:
                FileLock(lock_path).acquire(timeout=0)
            except Timeout:
                pass
            else:
                raise AssertionError('Concurrent file lock was not excluded')
        from git import Repo
        with Repo.init(Path(directory)/'git-fixture') as repository:
            assert repository.bare is False
    import importlib.util
    assert importlib.util.find_spec('pip') is None, 'Build-only pip remains in runtime'
    assert not Path('/usr/local/bin/npm').exists()
    assert not Path('/usr/local/bin/yarn').exists()
    assert not list(Path('apps').glob('*/node_modules/esbuild*/bin/esbuild'))
    print('PASS: security override image codecs, PDF reader, file locking, GitPython and compiler removal', flush=True)
    print('PASS: assets and PDF', flush=True)
    os.chdir('sites')
    frappe.init(site=SITE, sites_path=os.getcwd())
    frappe.connect()
    frappe.set_user('Administrator')
    expected = {a['name'] for a in LOCK['apps']}
    assert set(frappe.get_installed_apps()) == expected
    sid = session.cookies.get('sid')
    # API keys avoid coupling CRUD checks to web-session CSRF implementation details.
    from frappe.core.doctype.user.user import generate_keys
    credentials = generate_keys('Administrator')
    secret, key = credentials['api_secret'], credentials['api_key']
    frappe.db.commit()
    session.cookies.clear()
    session.headers['Authorization'] = f'token {key}:{secret}'
    response = session.post(base+'/api/resource/UOM', json={
        'uom_name': 'Container Smoke Unit'}, timeout=30)
    if not response.ok:
        detail = response.text.replace(secret, '[REDACTED]').replace(key, '[REDACTED]')
        raise RuntimeError(f'API create failed: {response.status_code}: {detail}')
    response.raise_for_status()
    name = response.json()['data']['name']
    response = session.get(base+'/api/resource/UOM/'+quote(name, safe=''), timeout=30)
    response.raise_for_status()
    assert response.json()['data']['uom_name'] == 'Container Smoke Unit'
    response = session.delete(base+'/api/resource/UOM/'+quote(name, safe=''), timeout=30)
    response.raise_for_status()
    # All three queue roles must execute an actual job, not merely start a process.
    print('PASS: authenticated API CRUD and application inventory', flush=True)
    from pdf_test import verify_pdf
    verify_pdf(session, base)
    from cache_test import verify_cache
    verify_cache()
    jobs = [frappe.enqueue('frappe.utils.now', queue=q, job_name=f'container-smoke-{q}')
            for q in ('short', 'default', 'long')]
    deadline = time.monotonic() + 120
    while any(job.get_status(refresh=True) not in ('finished', 'failed') for job in jobs):
        if time.monotonic() > deadline:
            raise RuntimeError('Queue execution timeout')
        time.sleep(2)
    assert all(job.get_status(refresh=True) == 'finished' for job in jobs)
    assert not frappe.conf.get('pause_scheduler')
    # Use Frappe's bundled websocket client; no test-only package or internet access.
    with socket.create_connection(('frontend', 8080), timeout=30) as tcp:
        with connect(f'ws://{SITE}:8080/socket.io/?EIO=4&transport=websocket', sock=tcp,
                     origin=f'http://{SITE}', additional_headers={'Cookie': f'sid={sid}'},
                     open_timeout=30) as client:
            assert client.recv(timeout=30).startswith('0')  # Engine.IO handshake
            client.send(f'40/{SITE},')  # Authenticate the site's Socket.IO namespace.
            frame = client.recv(timeout=30)
            while frame == '2':
                client.send('3')
                frame = client.recv(timeout=30)
            assert frame.startswith(f'40/{SITE},'), frame
    frappe.destroy()
    print('PASS: HTTP, login, authenticated API CRUD, assets, PDF, app inventory, all queues, websocket', flush=True)


if __name__ == '__main__':
    {'setup': setup, 'verify': verify}[sys.argv[1]]()
