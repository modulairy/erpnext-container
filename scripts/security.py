"""Explicit source overlays and removal of build-only executables; no CVE suppression."""
import json
import hashlib
from pathlib import Path
import re
import shutil
import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def overlay(root, overrides):
    overrides = dict(overrides, javascript=javascript_versions(overrides, root.name))
    changes = prepare_helpdesk_zod(root)
    versions = {canonicalize_name(k): v for k, v in overrides['python'].items()}
    project = root/'pyproject.toml'
    text = project.read_text()
    for original in tomllib.loads(text).get('project', {}).get('dependencies', []):
        requirement = Requirement(original)
        version = versions.get(canonicalize_name(requirement.name))
        if not version or requirement.url:
            continue
        for spec in requirement.specifier:
            if spec.operator in ('>=', '>', '==', '~=') and '*' not in spec.version:
                if Version(version) < Version(spec.version):
                    raise ValueError(f'Security policy would downgrade {original}')
        replacement = requirement.name+'=='+version
        if requirement.marker:
            replacement += '; '+str(requirement.marker)
        if json.dumps(original) not in text:
            raise ValueError(f'Unrecognized dependency declaration: {original}')
        text = text.replace(json.dumps(original), json.dumps(replacement))
        changes.append({'file': 'pyproject.toml', 'from': original, 'to': replacement})
    project.write_text(text)
    for manifest in root.rglob('package.json'):
        if '.git' in manifest.parts or 'node_modules' in manifest.parts:
            continue
        data = json.loads(manifest.read_text())
        yarn_lock = manifest.parent/'yarn.lock'
        locked = yarn_lock.read_text() if yarn_lock.exists() else ''
        declared_names = {name for group in ('dependencies', 'devDependencies', 'optionalDependencies')
                          for name in data.get(group, {})}
        resolutions = {name: version for name, version in overrides['javascript'].items()
                       if name in declared_names or re.search(r'(?m)^"?'+re.escape(name)+r'@', locked)}
        for group in ('dependencies', 'devDependencies', 'optionalDependencies'):
            for name, constraint in data.get(group, {}).items():
                if name not in overrides['javascript']:
                    continue
                lower = re.search(r'\d+(?:\.\d+){0,2}', constraint)
                if not lower:
                    raise ValueError(f'Unreviewed JavaScript requirement: {name}: {constraint}')
                selected, declared = Version(overrides['javascript'][name]), Version(lower.group())
                migrations = overrides.get('javascript_migrations', {}).get(name, [])
                if selected < declared or (selected.major != declared.major and declared.major not in migrations):
                    raise ValueError(f'Security policy conflicts with {name}: {constraint}')
                data[group][name] = str(selected)
        # Yarn resolutions update the actual graph before compilation, not just metadata.
        if not resolutions:
            continue
        data.setdefault('resolutions', {}).update(resolutions)
        manifest.write_text(json.dumps(data, indent=2)+'\n')
        changes.append({'file': str(manifest.relative_to(root)),
                        'resolutions': resolutions})
    # Resolve every dependency range again instead of retaining vulnerable lock entries.
    for locked in root.rglob('yarn.lock'):
        if '.git' in locked.parts or 'node_modules' in locked.parts:
            continue
        changes.append({'file': str(locked.relative_to(root)),
                        'previous_sha256': hashlib.sha256(locked.read_bytes()).hexdigest(),
                        'action': 'regenerate from updated manifests'})
        locked.unlink()
    return changes


def prepare_helpdesk_zod(root):
    manifest = root/'desk/package.json'
    if root.name != 'helpdesk' or not manifest.exists():
        return []
    data = json.loads(manifest.read_text())
    # The released Desk declares this adapter but uses Zod directly. Fail closed
    # if a later source starts consuming the adapter or adds unreviewed schemas.
    expected = {'src/utils.ts', 'src/components/ticket/SetContactPhoneModal.vue'}
    consumers = set()
    for path in manifest.parent.rglob('*'):
        if path.suffix not in ('.vue', '.ts', '.js', '.mjs', '.cjs') or 'node_modules' in path.parts:
            continue
        source = path.read_text()
        if '@vee-validate/zod' in source or 'toTypedSchema' in source:
            raise ValueError(f'Helpdesk now consumes the Zod adapter: {path}')
        if re.search(r'''["']zod(?:/[^"']*)?["']''', source):
            consumers.add(path.relative_to(manifest.parent).as_posix())
            if re.search(r'\b(?:z|zod)\s*\.\s*(?:array|set|map)\s*\(', source):
                raise ValueError(f'Helpdesk collection schemas require size-bound review: {path}')
    if consumers != expected:
        raise ValueError(f'Unreviewed Helpdesk Zod consumers: {sorted(consumers)}')
    if '@vee-validate/zod' not in data.get('dependencies', {}):
        return []
    del data['dependencies']['@vee-validate/zod']
    manifest.write_text(json.dumps(data, indent=2)+'\n')
    return [{'file': 'desk/package.json', 'action': 'remove unused @vee-validate/zod adapter',
             'verified_consumers': sorted(consumers)}]


def javascript_versions(overrides, app):
    return dict(overrides['javascript'], **overrides.get('javascript_by_app', {}).get(app, {}))


def verify_overrides(bench, overrides):
    failures = []
    for manifest in bench.rglob('package.json'):
        if 'node_modules' not in manifest.parts:
            continue
        data = json.loads(manifest.read_text())
        parts = manifest.relative_to(bench).parts
        app = parts[1] if parts[0] == 'apps' else ''
        expected = javascript_versions(overrides, app).get(data.get('name'))
        if expected and Version(data['version']) < Version(expected):
            failures.append(f'{manifest}: {data["version"]} < {expected}')
    if failures:
        raise ValueError('Security overrides were not installed:\n'+'\n'.join(failures))


def prune_build_tools(bench):
    removed = []
    # Frappe serves some node_modules files directly. Preserve those trees and
    # their scanner metadata; remove only known asset compiler packages/binaries.
    for modules in list(bench.rglob('node_modules')):
        if modules.is_symlink() or not modules.is_dir():
            continue
        paths = [modules/'.bin', modules/'esbuild', modules/'@esbuild']
        paths += list(modules.glob('esbuild-*'))
        for path in paths:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                continue
            removed.append(str(path.relative_to(bench)))
    return removed
