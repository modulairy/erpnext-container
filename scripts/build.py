"""Build only the immutable, explicitly allowed app set from the resolver."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from security import overlay, prune_build_tools, verify_overrides
from pdf_overlay import apply_pdf
from community_overlay import apply_community


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def install():
    lock = json.loads(Path('/opt/build/lock.json').read_text())
    bench = Path('/home/frappe/frappe-bench')
    sources = Path('/home/frappe/sources')
    allowed = {'frappe', 'erpnext', 'payments', 'telephony', 'helpdesk', 'builder',
               'webshop', 'crm', 'wiki', 'print_designer'}
    if {a['name'] for a in lock['apps']} != allowed:
        raise ValueError('Unexpected application set')
    sources.mkdir()
    lock['source_overlays'] = {}
    lock['os_backport_provenance'] = json.loads(Path('/opt/os-backports/provenance.json').read_text())
    for app in lock['apps']:
        name, sha = app['name'], app['sha']
        if app['url'] != f'https://github.com/frappe/{name}.git' or not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('Untrusted source')
        dest = sources/name
        run('git', 'init', str(dest))
        run('git', '-C', str(dest), 'remote', 'add', 'origin', app['url'])
        run('git', '-C', str(dest), 'fetch', '--depth=1', 'origin', sha)
        run('git', '-C', str(dest), 'checkout', '-b', 'locked', 'FETCH_HEAD')
        actual = subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != sha:
            raise ValueError('Source commit mismatch')
        lock['source_overlays'][name] = apply_community(dest, app, lock['community_patches'])
        lock['source_overlays'][name] += overlay(dest, lock['security_overrides'])
        lock['source_overlays'][name] += apply_pdf(dest, Path('/opt/build/pdf'))
        run('git', '-C', str(dest), 'add', '.')
        run('git', '-C', str(dest), '-c', 'user.name=Container security overlay',
            '-c', 'user.email=container@example.invalid', 'commit', '--allow-empty',
            '-m', 'Apply declared container security dependency overrides')
    run('bench', 'init', '--frappe-path', f'file://{sources}/frappe', '--frappe-branch', 'locked',
        '--no-procfile', '--no-backups', '--skip-redis-config-generation', '--skip-assets', str(bench))
    for app in lock['apps']:
        if app['name'] != 'frappe':
            run('bench', 'get-app', '--branch', 'locked', '--skip-assets',
                f"file://{sources}/{app['name']}", cwd=bench)
    run(str(bench/'env/bin/python'), '-m', 'pip', 'install', '--upgrade',
        *[f'{name}=={version}' for name, version in lock['security_overrides']['python'].items()])
    run(str(bench/'env/bin/python'), '-m', 'pip', 'check')
    verify_overrides(bench, lock['security_overrides'])
    (bench/'image-lock.json').write_text(json.dumps(lock, indent=2)+'\n')


def assets():
    bench = Path('/home/frappe/frappe-bench')
    sources = Path('/home/frappe/sources')
    lock = json.loads((bench/'image-lock.json').read_text())
    allowed = {app['name'] for app in lock['apps']}
    run('bench', 'build', '--production', cwd=bench)
    verify_overrides(bench, lock['security_overrides'])
    actual = set((bench/'sites/apps.txt').read_text().split())
    if actual != allowed:
        raise ValueError(f'Unexpected installed sources: {actual}')
    (bench/'sites/common_site_config.json').write_text('{}\n')
    # Keep baked assets outside the sites volume; each runtime uses its own image assets.
    shutil.copytree(bench/'sites/assets', bench/'assets', symlinks=True)
    shutil.rmtree(bench/'sites/assets')
    (bench/'sites/assets').symlink_to(bench/'assets', target_is_directory=True)
    lock['removed_build_tools'] = prune_build_tools(bench)
    (bench/'image-lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    for path in bench.rglob('.git'):
        if path.is_dir():
            shutil.rmtree(path)
    shutil.rmtree(sources)


if __name__ == '__main__':
    {'install': install, 'assets': assets}[sys.argv[1]]()
