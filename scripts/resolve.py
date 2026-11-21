"""Resolve public sources once per run; never execute upstream metadata."""
import ast
import json
import os
import re
import subprocess
import time
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
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=90) as response:
                return response.read()
        except HTTPError as error:
            detail = error.read().decode(errors='replace')[:500]
            transient = error.code in (429, 502, 503, 504) or (
                error.code == 403 and 'rate limit' in detail.lower())
            if transient and attempt < 2:
                time.sleep(min(int(error.headers.get('Retry-After', '30')), 60))
                continue
            error.add_note(f'Public source request failed: {url}: {detail}')
            raise


def api(path):
    return json.loads(request('https://api.github.com/' + path))


def pages(path):
    page = 1
    while True:
        data = api(f'{path}?per_page=100&page={page}')
        if not isinstance(data, list):
            raise ValueError(f'Expected list: {path}')
        yield from data
        # GitHub's releases endpoint exposes at most the first 1000 results.
        # Rows are newest-first, which is the only range used for candidate selection.
        if len(data) < 100 or page >= 10:
            break
        page += 1


def stable_releases(rows):
    return sorted((r for r in rows if not r.get('draft') and not r.get('prerelease')
                   and STABLE.fullmatch(r['tag_name'])),
                  key=lambda r: Version(r['tag_name']), reverse=True)


def source(app, ref, path, optional=False):
    try:
        return request(f'https://raw.githubusercontent.com/frappe/{app}/{quote(ref, safe="")}/{path}').decode()
    except HTTPError as error:
        if optional and error.code == 404:
            return ''
        raise


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
    if not isinstance(required, list) or any(
        not isinstance(name, str) or name.removeprefix('frappe/') not in APPS
        for name in required
    ):
        raise ValueError(f'{app}: undeclared app dependency {required}')
    required = [name.removeprefix('frappe/') for name in required]
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


def pypi_latest(name, python, constraint=''):
    data = json.loads(request(f'https://pypi.org/pypi/{name}/json'))
    for version in sorted(map(Version, data['releases']), reverse=True):
        if version.is_prerelease or version.is_devrelease or version not in SpecifierSet(constraint):
            continue
        files = data['releases'][str(version)]
        if any(not f.get('yanked') and Version(python) in SpecifierSet(f.get('requires_python') or '')
               for f in files):
            return str(version)
    raise ValueError(f'No compatible {name}')


def security_versions(python):
    policy = json.loads((Path(__file__).resolve().parents[1]/'security-policy.json').read_text())
    selected = {'python': {}, 'javascript': {}, 'javascript_by_app': {},
                'javascript_migrations': policy.get('javascript_migrations', {})}
    for name, constraint in policy['python'].items():
        selected['python'][name] = pypi_latest(name, python, constraint)
    groups = [(selected['javascript'], policy['javascript'])]
    for app, constraints in policy.get('javascript_by_app', {}).items():
        if app not in APPS:
            raise ValueError(f'Unknown app security policy: {app}')
        selected['javascript_by_app'][app] = {}
        groups.append((selected['javascript_by_app'][app], constraints))
    for target, constraints in groups:
        for name, constraint in constraints.items():
            data = json.loads(request('https://registry.npmjs.org/'+quote(name, safe='')))
            versions = [v for v in data['versions'] if STABLE.fullmatch(v)
                        and NpmSpec(constraint).match(NodeVersion(v))]
            target[name] = max(versions, key=Version)
    return selected


def image_digest(tag):
    text = subprocess.check_output(['docker', 'buildx', 'imagetools', 'inspect', tag], text=True)
    match = re.search(r'^Digest:\s+(sha256:[a-f0-9]{64})$', text, re.M)
    if not match:
        raise ValueError(f'Missing registry digest: {tag}')
    return tag + '@' + match[1]


def scanner_version():
    # Public registry metadata works on hosted runners where the vendor's
    # authenticated GitHub API is restricted by an organization IP allow list.
    prefix = 'https://hub.docker.com/v2/repositories/aquasec/trivy/tags'
    url = prefix + '?page_size=100'
    versions = []
    seen = set()
    while url:
        if not url.startswith(prefix + '?') or url in seen:
            raise ValueError('Unexpected scanner registry pagination')
        seen.add(url)
        page = json.loads(request(url))
        versions.extend(t['name'] for t in page['results'] if STABLE.fullmatch(t['name']))
        url = page['next']
    return max(versions, key=Version)


def runtime_inputs(rows, out):
    from base_manifest import load
    manifest = load()
    inputs = manifest['inputs']
    if (not all(Version(inputs['python']) in SpecifierSet(row['python']) for row in rows)
            or not all(NpmSpec(spec).match(NodeVersion(inputs['node']))
                       for row in rows for spec in row['node'])):
        raise ValueError('Selected applications do not match the approved base')
    (out/'base-images.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return inputs, manifest['verification_inputs']


def main():
    from community_overlay import selected_patches
    out = Path(os.environ.get('BUILD_DIR', '.build')).resolve()
    out.mkdir(parents=True, exist_ok=True)
    profile = os.environ.get('BUILD_PROFILE', 'erpnext')
    if profile not in ('frappe', 'erpnext'):
        raise ValueError('Unknown build profile')
    apps = ('frappe',) if profile == 'frappe' else APPS
    options = []
    for app in apps:
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
    inputs, verification = runtime_inputs(rows, out)
    python = inputs['python']
    lock = {'schema': 1, 'resolved_at': datetime.now(timezone.utc).isoformat(),
            'framework_major': MAJOR, 'apps': rows, **inputs, **verification,
            'security_overrides': security_versions(python),
            'pdf_engine': 'sandboxed-official-headless-shell'}
    if profile == 'frappe':
        lock.update(profile='frappe', frappe_tag=selected['frappe']['ref'])
    else:
        lock['erpnext_tag'] = selected['erpnext']['ref']
    lock['community_patches'] = selected_patches(rows)
    (out/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    print(json.dumps({k: lock[k] for k in ('frappe_tag', 'erpnext_tag', 'python', 'node', 'bench') if k in lock}))
    for row in rows:
        print(f"{row['name']}: {row['ref']} ({row['kind']}) @ {row['sha']}")


if __name__ == '__main__':
    main()

