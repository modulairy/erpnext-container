"""Resolve public sources once per run; never execute upstream metadata."""
import ast
import hashlib
import gzip
import lzma
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


def redis_lua(out):
    # Reviewed stable protocol client; source is immutable and checked at build time.
    sha = '3fdc727f176f565adca8c2b350a636bd74e93de2'
    data = request(f'https://raw.githubusercontent.com/openresty/lua-resty-redis/{sha}/lib/resty/redis.lua')
    (out/'redis.lua').write_bytes(data)
    return {'version': 'v0.33', 'sha': sha, 'sha256': hashlib.sha256(data).hexdigest()}


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


def published_runtime(repository, versions, suffix):
    for version in sorted(versions, key=Version, reverse=True):
        tag = version+suffix
        try:
            metadata = json.loads(request(f'https://hub.docker.com/v2/repositories/library/{repository}/tags/{tag}'))
        except HTTPError as error:
            if error.code != 404:
                raise
            print(f'Official runtime image not published yet: {repository}:{tag}', flush=True)
            continue
        images = metadata.get('images')
        if not isinstance(images, list) or any(not isinstance(image, dict) for image in images):
            raise ValueError(f'Invalid runtime image metadata: {repository}:{tag}')
        if not any(image.get('os') == 'linux' and image.get('architecture') == 'amd64'
                   for image in images):
            print(f'Official linux/amd64 runtime not published yet: {repository}:{tag}', flush=True)
            continue
        return version, image_digest(f'{repository}:{tag}')
    raise ValueError(f'No published compatible linux/amd64 {repository} runtime image')


def mariadb_client(out):
    repository = 'https://deb.mariadb.org/11.8/debian'
    key = request('https://mariadb.org/mariadb_release_signing_key.pgp')
    (out/'mariadb-keyring.asc').write_bytes(key)
    index = gzip.decompress(request(repository+'/dists/trixie/main/binary-amd64/Packages.gz')).decode()
    packages = ('mariadb-client', 'mariadb-client-core', 'mariadb-common', 'libmariadb3', 'libmariadb-dev')
    candidates = {name: set() for name in packages}
    for paragraph in index.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        name, version = fields.get('Package'), fields.get('Version', '')
        if name in candidates and re.fullmatch(r'1:11\.8\.\d+\+maria~deb13', version):
            candidates[name].add(version)
    common = set.intersection(*candidates.values())
    version = max(common, key=lambda v: Version(v.split(':')[1].split('+')[0]))
    return {'repository': repository, 'version': version, 'key_sha256': hashlib.sha256(key).hexdigest()}


def curl_backport():
    url = 'https://deb.debian.org/debian/dists/trixie-backports/main/binary-amd64/Packages.xz'
    index = lzma.decompress(request(url)).decode()
    candidates = {name: set() for name in ('curl', 'libcurl3t64-gnutls', 'libcurl4-gnutls', 'libcurl4t64')}
    for paragraph in index.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines()
                      if ': ' in line and not line.startswith(' '))
        name, version = fields.get('Package'), fields.get('Version', '')
        if name in candidates and re.fullmatch(r'\d+\.\d+\.\d+-\d+~bpo13\+\d+(?:\+b\d+)?', version):
            candidates[name].add(version)
    common = set.intersection(*candidates.values())
    return max(common, key=lambda v: tuple(map(int, re.findall(r'\d+', v))))


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


def os_backports():
    # Reviewed ABI-compatible source-package lines; never install sid binaries.
    url = 'https://deb.debian.org/debian/dists/sid/main/source/Sources.xz'
    index = lzma.decompress(request(url)).decode()
    reviewed = {'acl': '2.4.0-1', 'ncurses': '6.6+20260608-2',
                'perl': '5.42.3-1', 'systemd': '262.2-1', 'libsndfile': '1.2.2-4',
                'libx11': '2:1.8.14-1', 'libxrender': '1:0.9.13-1', 'libxtst': '2:1.2.6-1',
                'expat': '2.10.0-1', 'sqlite3': '3.54.1-2', 'libxi': '2:1.8.4-1'}
    result = {}
    for paragraph in index.split('\n\n'):
        fields = {}
        key = None
        for line in paragraph.splitlines():
            if line.startswith(' ') and key:
                fields[key] += '\n'+line.strip()
            elif ': ' in line or line.endswith(':'):
                key, value = line.split(':', 1)
                fields[key] = value.strip()
        name = fields.get('Package')
        if name in reviewed and fields.get('Version') == reviewed[name]:
            files = {}
            for line in fields['Checksums-Sha256'].splitlines():
                if not line.strip():
                    continue
                digest, size, filename = line.split()
                if not re.fullmatch('[a-f0-9]{64}', digest) or Path(filename).name != filename:
                    raise ValueError('Invalid OS source metadata')
                files[filename] = digest
            if not any(p.endswith('.dsc') for p in files):
                raise ValueError('Missing source descriptor')
            result[name] = {'version': reviewed[name], 'files': files, 'index': url}
    if set(result) != set(reviewed):
        raise ValueError('Reviewed OS source version unavailable; review required')
    for name, upstream, version, digest in (
        ('libx11', 'libX11', '1.8.14', 'bbc78c46217174655c2b32b138ad119f3ac15f50808b4d0b3a2677a4b1afc950'),
        ('libxrender', 'libXrender', '0.9.13', '3fe88c609df6782e1e7af2e4de36b2563d70cd70ff7dcb1cda561ad64912bb64'),
        ('libxtst', 'libXtst', '1.2.6', 'f94f0b37c07d80d877f3eda2c2405cc7e62aa9842f6310354aa349e3d66a4fe5')):
        directory = upstream+'-'+version
        result[name]['upstream'] = {'directory': directory, 'version': version,
            'url': 'https://xorg.freedesktop.org/archive/individual/lib/'+directory+'.tar.xz',
            'sha256': digest}
        # Stay newer than the exact reviewed Debian source revision we rebuild.
        result[name]['binary_version'] = reviewed[name] + '+container1'
    result['sqlite3'].update(binary_version='3.54.1-2+container1', build_tcl='9.0')
    result['libsndfile'].update(binary_version='1.2.2-4+container1', patch={
        'url': 'https://github.com/libsndfile/libsndfile/commit/cdadf9a65fdc1f28da9d235eb443d5f5e57010d2.patch',
        'sha256': '19828cfdc17b80f13c8d1fcc8b828ea5c328071d91171c6d80f94a2e1499b719'})
    return result



def pyexpat_sources(python):
    files = {'pyexpat.c': 'Modules/pyexpat.c', '_elementtree.c': 'Modules/_elementtree.c',
             'clinic/_elementtree.c.h': 'Modules/clinic/_elementtree.c.h',
         'clinic/pyexpat.c.h': 'Modules/clinic/pyexpat.c.h',
             'expat_config.h': 'Modules/expat/expat_config.h', 'pyexpat.h': 'Include/pyexpat.h'}
    return {name: {'url': f'https://raw.githubusercontent.com/python/cpython/v{python}/{path}',
                   'sha256': hashlib.sha256(request(f'https://raw.githubusercontent.com/python/cpython/v{python}/{path}')).hexdigest()}
            for name, path in files.items()}

def runtime_inputs(rows, out, base_build):
    from base_manifest import BASE_FIELDS, load
    if not base_build:
        manifest = load()
        inputs = manifest['inputs']
        python, node = inputs['python'], inputs['node']
        if (not all(Version(python) in SpecifierSet(row['python']) for row in rows)
                or not all(NpmSpec(spec).match(NodeVersion(node)) for row in rows for spec in row['node'])):
            raise ValueError('Selected apps require a newer runtime; update the base manually')
        (out/'base-images.json').write_text(json.dumps(manifest, indent=2)+'\n')
        return {**inputs, 'bootstrap': manifest['bootstrap']}, manifest['security']['trivy_image'], manifest['security']['grype_image']
    py_tags = api('repos/python/cpython/git/matching-refs/tags/v3.14.')
    versions = [r['ref'].split('/')[-1][1:] for r in py_tags
                if STABLE.fullmatch(r['ref'].split('/')[-1])]
    python, python_image = published_runtime('python',
        [v for v in versions if all(Version(v) in SpecifierSet(r['python']) for r in rows)], '-slim-trixie')
    nodes = json.loads(request('https://nodejs.org/dist/index.json'))
    node, node_image = published_runtime('node',
        [n['version'][1:] for n in nodes if n['lts'] and n['version'].startswith('v24.')
                and all(NpmSpec(spec).match(NodeVersion(n['version'][1:]))
                        for r in rows for spec in r['node'])], '-trixie-slim')
    yarn_data = json.loads(request('https://registry.npmjs.org/yarn'))
    yarn = max((v for v in yarn_data['versions'] if STABLE.fullmatch(v)
                and Version(v).major == 1), key=Version)

    inputs = {'python': python, 'python_image': python_image, 'node': node, 'node_image': node_image,
              'yarn': yarn, 'mariadb_client': mariadb_client(out), 'curl_backport': curl_backport(),
              'os_backports': os_backports(),
              'chromium': json.loads((Path(__file__).resolve().parents[1]/'resources/chromium.json').read_text()),
              'pyexpat_sources': pyexpat_sources(python)}
    return inputs, image_digest(f'aquasec/trivy:{scanner_version().lstrip("v")}'), image_digest('anchore/grype:v0.121.2')


def main():
    from community_overlay import selected_patches
    out = Path(os.environ.get('BUILD_DIR', '.build')).resolve()
    out.mkdir(parents=True, exist_ok=True)
    base_build = os.environ.get('BASE_BUILD') == 'true'
    profile = os.environ.get('BUILD_PROFILE', 'erpnext')
    if profile not in ('frappe', 'erpnext'):
        raise ValueError('Unknown build profile')
    apps = ('frappe',) if profile == 'frappe' and not base_build else APPS
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
    inputs, trivy_image, grype_image = runtime_inputs(rows, out, base_build)
    python = inputs['python']
    bench = pypi_latest('frappe-bench', python)
    lock = {'schema': 1, 'resolved_at': datetime.now(timezone.utc).isoformat(),
            'framework_major': MAJOR, 'apps': rows, **inputs, 'bench': bench,
            'bootstrap': json.loads((Path(__file__).resolve().parents[1]/'resources/bootstrap.json').read_text()) if base_build else inputs['bootstrap'],
            'security_overrides': security_versions(python),
            'mariadb_image': image_digest('mariadb:11.8'),
            'redis_image': image_digest('redis:8-alpine'),
            'trivy_image': trivy_image, 'grype_image': grype_image,
            'pdf_engine': 'sandboxed-official-headless-shell'}
    if profile == 'frappe' and not base_build:
        lock.update(profile='frappe', frappe_tag=selected['frappe']['ref'])
    else:
        lock['erpnext_tag'] = selected['erpnext']['ref']
    lock['redis_lua'] = redis_lua(out)
    lock['community_patches'] = selected_patches(rows)
    (out/'lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    print(json.dumps({k: lock[k] for k in ('frappe_tag', 'erpnext_tag', 'python', 'node', 'bench') if k in lock}))
    for row in rows:
        print(f"{row['name']}: {row['ref']} ({row['kind']}) @ {row['sha']}")


if __name__ == '__main__':
    main()
