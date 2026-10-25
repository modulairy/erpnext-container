"""Resolve public sources once per run; never execute upstream metadata."""
import ast
import base64
import hashlib
import json
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from semantic_version import NpmSpec, Version as NodeVersion

# New framework majors require a reviewed runtime and integration test update.
MAJOR = 16
APPS = ('frappe', 'erpnext', 'payments', 'telephony', 'helpdesk', 'builder',
        'webshop', 'crm', 'wiki', 'print_designer')
SNAPSHOTS = {'payments': 'version-16', 'webshop': 'version-16',
             'telephony': 'develop'}
STABLE = re.compile(r'^v?(\d+\.\d+\.\d+)$')


@lru_cache(maxsize=None)
def request(url):
    headers = {'User-Agent': 'erpnext-community-resolver'}
    token = os.environ.get('GITHUB_TOKEN')
    if token and url.startswith('https://api.github.com/'):
        headers['Authorization'] = f'Bearer {token}'
    with urlopen(Request(url, headers=headers), timeout=90) as response:
        return response.read()


def api(path):
    return json.loads(request('https://api.github.com/' + path))


def pages(path):
    page = 1
    while True:
        data = api(f'{path}?per_page=100&page={page}')
        if not isinstance(data, list):
            raise ValueError(f'Expected list: {path}')
        yield from data
        if len(data) < 100:
            break
        page += 1


def stable_releases(rows):
    return sorted((r for r in rows if not r.get('draft') and not r.get('prerelease')
                   and STABLE.fullmatch(r['tag_name'])),
                  key=lambda r: Version(r['tag_name']), reverse=True)


def source(app, ref, path, optional=False):
    try:
        row = api(f'repos/frappe/{app}/contents/{path}?ref={quote(ref, safe="")}')
    except HTTPError as error:
        if optional and error.code == 404:
            return ''
        raise
    return base64.b64decode(row['content']).decode()


def literal(text, name, default=None):
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                               for t in node.targets):
            return ast.literal_eval(node.value)
    return default


@lru_cache(maxsize=None)
def candidate(app, ref, kind):
    sha = api(f'repos/frappe/{app}/commits/{quote(ref, safe="")}')['sha']
    meta = tomllib.loads(source(app, sha, 'pyproject.toml'))
    hooks = source(app, sha, f'{app}/hooks.py', optional=True)
    required = literal(hooks, 'required_apps', [])
    if not isinstance(required, list) or any(a not in APPS for a in required):
        raise ValueError(f'{app}: undeclared app dependency {required}')
    version = literal(source(app, sha, f'{app}/__init__.py'), '__version__')
    if not version:
        raise ValueError(f'{app}: missing version')
    if kind == 'release' and Version(version) != Version(ref):
        raise ValueError(f'{app}: release/source mismatch: {ref}/{version}')
    deps = meta.get('tool', {}).get('bench', {}).get('frappe-dependencies', {})
    if set(deps) - set(APPS):
        raise ValueError(f'{app}: unknown dependency {deps}')
    packages = []
    for path in ('package.json', 'frontend/package.json'):
        text = source(app, sha, path, optional=True)
        if text:
            packages.append(json.loads(text))
    return {'name': app, 'url': f'https://github.com/frappe/{app}.git',
            'ref': ref, 'sha': sha, 'kind': kind, 'version': version,
            'python': meta['project']['requires-python'], 'dependencies': deps,
            'required_apps': required,
            'node': [p['engines']['node'] for p in packages if p.get('engines', {}).get('node')]}


def compatible(rows):
    selected = {r['name']: r for r in rows}
    for row in rows:
        for dep, constraint in row['dependencies'].items():
            if dep in selected and Version(selected[dep]['version']) not in SpecifierSet(constraint):
                return False
    return True


def solve(options, chosen=()):
    if not options:
        return list(chosen)
    app, refs = options[0]
    for ref, kind in refs:
        row = candidate(app, ref, kind)
        if compatible((*chosen, row)):
            result = solve(options[1:], (*chosen, row))
            if result:
                return result
    return None


def install_order(rows):
    pending = {r['name']: r for r in rows}
    ordered = []
    while pending:
        ready = [name for name, row in pending.items()
                 if not (set(row['required_apps']) | set(row['dependencies'])) & pending.keys()]
        if not ready:
            raise ValueError('Cyclic application dependencies')
        for name in ready:
            ordered.append(pending.pop(name))
    return ordered


def pypi_latest(name, python):
    data = json.loads(request(f'https://pypi.org/pypi/{name}/json'))
    for version in sorted(map(Version, data['releases']), reverse=True):
        if version.is_prerelease or version.is_devrelease:
            continue
        files = data['releases'][str(version)]
        if any(not f.get('yanked') and Version(python) in SpecifierSet(f.get('requires_python') or '')
               for f in files):
            return str(version)
    raise ValueError(f'No compatible {name}')


def image_digest(tag):
    text = subprocess.check_output(['docker', 'buildx', 'imagetools', 'inspect', tag], text=True)
    match = re.search(r'^Digest:\s+(sha256:[a-f0-9]{64})$', text, re.M)
    if not match:
        raise ValueError(f'Missing registry digest: {tag}')
    return tag + '@' + match[1]


def main():
    out = Path(os.environ.get('BUILD_DIR', '.build')).resolve()
    out.mkdir(parents=True, exist_ok=True)
    options = []
    for app in APPS:
        print(f'Reading releases: {app}', flush=True)
        releases = stable_releases(pages(f'repos/frappe/{app}/releases'))
        if app in ('frappe', 'erpnext'):
            releases = [r for r in releases if Version(r['tag_name']).major == MAJOR]
        refs = [(r['tag_name'], 'release') for r in releases]
        if not refs and app in SNAPSHOTS:
            refs = [(SNAPSHOTS[app], 'snapshot')]
        if not refs:
            raise ValueError(f'{app}: no approved stable source')
        options.append((app, refs))
    options.sort(key=lambda p: (p[0] not in SNAPSHOTS, APPS.index(p[0])))
    rows = solve(options)
    if not rows:
        raise ValueError('No compatible complete application set; publication blocked')
    rows = install_order(rows)
    selected = {r['name']: r for r in rows}
    py_tags = api('repos/python/cpython/git/matching-refs/tags/v3.14.')
    versions = [r['ref'].split('/')[-1][1:] for r in py_tags
                if STABLE.fullmatch(r['ref'].split('/')[-1])]
    python = max((v for v in versions if all(Version(v) in SpecifierSet(r['python']) for r in rows)),
                 key=Version)
    nodes = json.loads(request('https://nodejs.org/dist/index.json'))
    node = next(n['version'][1:] for n in nodes if n['lts'] and n['version'].startswith('v24.')
                and all(NpmSpec(spec).match(NodeVersion(n['version'][1:]))
                        for r in rows for spec in r['node']))
    bench = pypi_latest('frappe-bench', python)
    yarn_data = json.loads(request('https://registry.npmjs.org/yarn'))
    yarn = max((v for v in yarn_data['versions'] if STABLE.fullmatch(v)
                and Version(v).major == 1), key=Version)
    wk_release = api('repos/wkhtmltopdf/packaging/releases/latest')
    if wk_release['prerelease'] or wk_release['draft']:
        raise ValueError('wkhtmltopdf release must be stable')
    asset = next(a for a in wk_release['assets'] if a['name'].endswith('.bookworm_amd64.deb'))
    asset_bytes = request(asset['browser_download_url'])
    published_digest = asset.get('digest')
    if published_digest and published_digest != 'sha256:' + hashlib.sha256(asset_bytes).hexdigest():
        raise ValueError('wkhtmltopdf asset checksum mismatch')
    (out/'wkhtmltox.deb').write_bytes(asset_bytes)
    trivy = stable_releases(pages('repos/aquasecurity/trivy/releases'))[0]['tag_name']
    lock = {'schema': 1, 'resolved_at': datetime.now(timezone.utc).isoformat(),
            'framework_major': MAJOR, 'erpnext_tag': selected['erpnext']['ref'], 'apps': rows,
            'python': python, 'node': node, 'bench': bench, 'yarn': yarn,
            'python_image': image_digest(f'python:{python}-slim-trixie'),
            'node_image': image_digest(f'node:{node}-trixie-slim'),
            'mariadb_image': image_digest('mariadb:11.8'),
            'redis_image': image_digest('redis:7.2-alpine'),
            'trivy_image': image_digest(f'aquasec/trivy:{trivy.lstrip("v")}'),
            'wkhtmltopdf': {'url': asset['browser_download_url'],
                           'sha256': hashlib.sha256(asset_bytes).hexdigest()}}
    (out/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    print(json.dumps({k: lock[k] for k in ('erpnext_tag', 'python', 'node', 'bench')}))
    for row in rows:
        print(f"{row['name']}: {row['ref']} ({row['kind']}) @ {row['sha']}")


if __name__ == '__main__':
    main()
