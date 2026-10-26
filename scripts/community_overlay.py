"""Apply reviewed compatibility patches only to their audited release sources."""
import hashlib
import json
from pathlib import Path
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def policy(folder=None):
    folder = Path(folder) if folder else Path(__file__).resolve().parent/'patches'
    if not folder.exists():
        folder = Path(__file__).resolve().parents[1]/'patches'
    manifest = json.loads((folder/'manifest.json').read_text())
    for entry in manifest.values():
        if digest(folder/entry['patch']) != entry['sha256']:
            raise ValueError('Compatibility patch checksum mismatch')
    return folder, manifest


def selected_patches(apps, folder=None):
    _, manifest = policy(folder)
    versions = {app['name']: app['version'] for app in apps}
    for name, entry in manifest.items():
        if versions.get(name) != entry['version']:
            raise ValueError(f'Review compatibility patch for {name} {versions.get(name)}')
    return manifest


def apply_community(root, app, locked):
    folder, manifest = policy()
    entry = manifest.get(app['name'])
    if not entry:
        return []
    if entry != locked.get(app['name']) or app['version'] != entry['version']:
        raise ValueError('Compatibility patch does not match resolved lock')
    for name, expected in entry['expected'].items():
        path = root/name
        if path.is_symlink() or digest(path) != expected:
            raise ValueError('Review changed upstream compatibility source: '+name)
    subprocess.run(['git', '-C', str(root), 'apply', '--check', str(folder/entry['patch'])], check=True)
    subprocess.run(['git', '-C', str(root), 'apply', str(folder/entry['patch'])], check=True)
    for name, expected in entry['output'].items():
        if digest(root/name) != expected:
            raise ValueError('Unexpected compatibility patch output: '+name)
    return [{'action': 'reviewed Frappe 16 community backport', **entry}]
