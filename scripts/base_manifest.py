"""Approved immutable base manifest and guarded publication on main."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
BASE_IMAGE = 'ghcr.io/modulairy/erpnext-base'
BASE_FIELDS = ('python', 'node', 'python_image', 'node_image', 'yarn',
               'mariadb_client', 'curl_backport', 'os_backports', 'chromium', 'pyexpat_sources')
MANIFEST = 'resources/base-images.json'


def policy_hash(root=ROOT):
    files = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
             for name in ('security-exceptions.json', 'security-remediations.json')}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def validate(data, require_verified=True):
    if data.get('schema') != 2 or set(data.get('images', {})) != {'runtime', 'builder'}:
        raise ValueError('Missing approved base manifest; run the base Action manually')
    for image in data['images'].values():
        if not re.fullmatch(re.escape(BASE_IMAGE)+r'@sha256:[0-9a-f]{64}', image):
            raise ValueError('Approved base references must be immutable GHCR digests')
    if set(data.get('inputs', {})) != set(BASE_FIELDS):
        raise ValueError('Missing locked base runtime inputs')
    if require_verified:
        security = data.get('security', {})
        if (security.get('status') != 'passed' or security.get('severity') != ['HIGH', 'CRITICAL']
                or security.get('policy_sha256') != policy_hash()
                or set(security.get('reports', {})) != {'runtime', 'builder'}):
            raise ValueError('Base must pass the shared security policy in the manual base Action')
    return data


def load(path=None):
    path = Path(path) if path is not None else ROOT/MANIFEST
    if not path.is_file():
        raise ValueError('No approved base is selected; run the base Action manually')
    return validate(json.loads(path.read_text()))


def promote(data, repository, revision, api):
    validate(data)
    if repository != 'modulairy/erpnext-container' or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Unexpected base publication target')
    repo = 'repos/'+repository
    head = api('GET', repo+'/git/ref/heads/main')
    if head['object']['sha'] != revision:
        raise ValueError('Main changed during base validation; refusing to update its manifest')
    parent = api('GET', repo+'/git/commits/'+revision)
    content = json.dumps(data, indent=2)+'\n'
    tree = api('POST', repo+'/git/trees', {'base_tree': parent['tree']['sha'],
        'tree': [{'path': MANIFEST, 'mode': '100644', 'type': 'blob', 'content': content}]})
    commit = api('POST', repo+'/git/commits', {'message': 'Select verified immutable base images',
        'tree': tree['sha'], 'parents': [revision]})
    # Fast-forward only: a concurrent main update rejects publication.
    api('PATCH', repo+'/git/refs/heads/main', {'sha': commit['sha'], 'force': False})
    saved = api('GET', repo+'/contents/'+MANIFEST+'?ref='+commit['sha'])
    if json.loads(base64.b64decode(saved['content'])) != data:
        raise RuntimeError('Published base manifest verification failed')
    return commit['sha']


if __name__ == '__main__':
    from release import api
    out = Path(os.environ.get('BUILD_DIR', '.build'))
    sha = promote(json.loads((out/'base-images.json').read_text()),
                  os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_SHA'], api)
    print('Approved base manifest published at '+sha)
