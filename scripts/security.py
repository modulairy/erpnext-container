"""Explicit source overlays and removal of build-only executables; no CVE suppression."""
import json
from pathlib import Path
import re
import shutil
import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def overlay(root, overrides):
    changes = []
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
                if selected < declared or selected.major != declared.major:
                    raise ValueError(f'Security policy conflicts with {name}: {constraint}')
        # Yarn resolutions update the actual graph before compilation, not just metadata.
        if not resolutions:
            continue
        data.setdefault('resolutions', {}).update(resolutions)
        manifest.write_text(json.dumps(data, indent=2)+'\n')
        changes.append({'file': str(manifest.relative_to(root)),
                        'resolutions': resolutions})
    return changes


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
