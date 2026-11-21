"""Disposable-site integration checks executed inside the actual built image."""
import json
import os
from pathlib import Path
import subprocess
import socket
import shutil
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
              'serve_default_site': True, 'allow_symlink_sites': True, 'scheduler_tick_interval': 1}
    Path('sites/common_site_config.json').write_text(json.dumps(config))
    command('bench', 'new-site', SITE, '--db-root-password', os.environ['DB_PASSWORD'],
            '--admin-password', os.environ['ADMIN_PASSWORD'], '--mariadb-user-host-login-scope', '%')
    # Initialize encryption before any long-lived service caches site configuration.
    from cryptography.fernet import Fernet
    site_config = Path('sites')/SITE/'site_config.json'
    values = json.loads(site_config.read_text())
    values.setdefault('encryption_key', Fernet.generate_key().decode())
    values['helpdesk_search_backend'] = 'redis'
    values['allow_tests'] = True
    site_config.write_text(json.dumps(values))
    for app in LOCK['apps']:
        if app['name'] != 'frappe':
            command('bench', '--site', SITE, 'install-app', app['name'])
    command('bench', '--site', SITE, 'migrate')
    command('bench', '--site', SITE, 'enable-scheduler')
    second = 'search-other.local'
    command('bench', 'new-site', second, '--db-root-password', os.environ['DB_PASSWORD'],
            '--admin-password', os.environ['ADMIN_PASSWORD'], '--mariadb-user-host-login-scope', '%')
    second_config = Path('sites')/second/'site_config.json'
    second_values = json.loads(second_config.read_text())
    second_values.update(helpdesk_search_backend='redis', encryption_key=Fernet.generate_key().decode())
    second_config.write_text(json.dumps(second_values))
    for app in LOCK['apps']:
        if app['name'] != 'frappe':
            command('bench', '--site', second, 'install-app', app['name'])
    command('bench', '--site', second, 'migrate')
    target = Path('sites/.linked-sites')
    target.mkdir()
    (Path('sites')/SITE).rename(target/SITE)
    (Path('sites')/SITE).symlink_to(Path('.linked-sites')/SITE, target_is_directory=True)
    other = Path('sites/cache-other.local')
    (other/'public/files').mkdir(parents=True, exist_ok=True)
    (other/'site_config.json').write_text('{"redis_cache":"redis://redis:6379/3"}')
    print('PASS: site creation, all applications installed, migration, scheduler enabled', flush=True)


def verify_app_pages(session, base):
    import frappe
    import requests
    from builder.utils import Block
    from wiki.frappe_wiki.doctype.wiki_change_request.wiki_change_request import (
        create_change_request, create_cr_page, merge_change_request, update_cr_page,
    )
    suffix = frappe.generate_hash(length=8)
    route = 'container-builder-'+suffix
    def blocks(text):
        return Block(element='div', originalElement='body',
                     children=[Block(element='h1', innerHTML=text)]).as_json(wrap_in_array=True)
    page = frappe.get_doc({'doctype': 'Builder Page', 'page_title': 'Container Builder',
                          'route': route, 'draft_blocks': blocks('Builder initial '+suffix)}).insert()
    page.publish()
    frappe.db.commit()
    response = requests.get(base+'/'+route, headers={'Host': SITE}, timeout=30)
    response.raise_for_status()
    assert 'Builder initial '+suffix in response.text
    page.draft_blocks = blocks('Builder updated '+suffix)
    page.save()
    page.publish()
    frappe.db.commit()
    response = requests.get(base+'/'+route, headers={'Host': SITE}, timeout=30)
    response.raise_for_status()
    assert 'Builder updated '+suffix in response.text
    response = session.get(base+'/builder', timeout=30)
    response.raise_for_status()
    assert 'text/html' in response.headers['Content-Type']
    assert '/assets/builder/' in response.text
    page.unpublish()
    frappe.db.commit()
    response = requests.get(base+'/'+route, headers={'Host': SITE}, timeout=30)
    assert response.status_code == 404 and 'Builder updated '+suffix not in response.text
    space = frappe.get_doc({'doctype': 'Wiki Space', 'space_name': 'Container Wiki '+suffix,
                            'route': 'container-wiki-'+suffix, 'is_published': 1,
                            'roles': [{'role': 'Guest', 'permission_level': 'Read'}]}).insert()
    creation = create_change_request(space.name, 'Container acceptance creation')
    root_key = frappe.db.get_value('Wiki Document', space.root_group, 'doc_key')
    page_key = create_cr_page(creation.name, root_key, 'Container wiki page',
                             content='Wiki initial '+suffix)
    frappe.db.set_value('Wiki Change Request', creation.name, 'status', 'Approved')
    merge_change_request(creation.name)
    document = frappe.get_doc('Wiki Document', {'doc_key': page_key})
    change = create_change_request(space.name, 'Container acceptance update')
    update_cr_page(change.name, document.doc_key, {'content': 'Wiki updated '+suffix})
    frappe.db.set_value('Wiki Change Request', change.name, 'status', 'Approved')
    merge_change_request(change.name)
    document.reload()
    assert document.content == 'Wiki updated '+suffix
    frappe.db.commit()
    response = requests.get(base+'/'+document.route, headers={'Host': SITE}, timeout=30)
    response.raise_for_status()
    assert 'Wiki updated '+suffix in response.text
    space.reload()
    space.set('roles', [{'role': 'System Manager', 'permission_level': 'Read'}])
    space.save()
    frappe.db.commit()
    response = requests.get(base+'/'+document.route, headers={'Host': SITE}, timeout=30, allow_redirects=False)
    assert response.status_code in (302, 403, 404) and 'Wiki updated '+suffix not in response.text
    print('PASS: Builder create/edit/publish/unpublish and editor route; Wiki change-request merge, public read and private-space isolation', flush=True)


def verify():
    if LOCK.get('profile') != 'frappe':
        command('node', '/tests/zod_test.cjs')
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
    print('PASS: HTTP request using the runtime curl', flush=True)
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
    assert not shutil.which('npm')
    assert not shutil.which('yarn')
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
    doctype = 'ToDo' if LOCK.get('profile') == 'frappe' else 'UOM'
    fields = {'description': 'Container Smoke Task', 'status': 'Open'} if doctype == 'ToDo' else {'uom_name': 'Container Smoke Unit'}
    response = session.post(base+'/api/resource/'+doctype, json=fields, timeout=30)
    if not response.ok:
        detail = response.text.replace(secret, '[REDACTED]').replace(key, '[REDACTED]')
        raise RuntimeError(f'API create failed: {response.status_code}: {detail}')
    name = response.json()['data']['name']
    endpoint = base+'/api/resource/'+doctype+'/'+quote(name, safe='')
    response = session.get(endpoint, timeout=30)
    response.raise_for_status()
    for field, value in fields.items():
        assert response.json()['data'][field] == value
    response = session.delete(endpoint, timeout=30)
    response.raise_for_status()
    # All three queue roles must execute an actual job, not merely start a process.
    print('PASS: authenticated API CRUD and application inventory', flush=True)
    if LOCK.get('profile') != 'frappe':
        verify_app_pages(session, base)
    from pdf_test import verify_pdf, verify_frappe_pdf
    (verify_frappe_pdf if LOCK.get('profile') == 'frappe' else verify_pdf)(session, base)
    frappe.db.commit()
    assert frappe.conf.allow_tests, 'Application tests must be explicitly enabled on the disposable site'
    if LOCK.get('profile') == 'frappe':
        from community_test import symlink_discovery
        symlink_discovery()
    else:
        command('bench', '--site', SITE, 'run-tests', '--app', 'helpdesk',
                '--module', 'helpdesk.tests.test_ticket_search_backend')
        from community_test import verify_community
        verify_community(session, base)
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
