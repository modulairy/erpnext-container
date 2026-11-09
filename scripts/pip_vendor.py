"""Inspect the reviewed pip bundle without importing optional third-party packages."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import sysconfig
import zipfile


def inspect():
    config = json.loads(Path('/opt/bootstrap/provenance.json').read_text())
    version = config['pip']['version']
    import ensurepip
    seed = Path(ensurepip.__file__).parent/'_bundled'/('pip-'+version+'-py3-none-any.whl')
    if ensurepip.version() != version or hashlib.sha256(seed.read_bytes()).hexdigest() != config['pip']['sha256']:
        raise ValueError('ensurepip bundle differs from the locked official wheel')
    global_site = Path(sys.base_prefix)/'lib'/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'site-packages'
    roots = {global_site, Path(sysconfig.get_path('purelib')),
             Path('/home/frappe/frappe-bench/env/lib')/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'site-packages',
             Path('/usr/lib/python3/dist-packages')}
    setuptools = []
    for root in roots:
        if root.is_dir():
            versions = [d.version for d in importlib.metadata.distributions(path=[str(root)])
                        if d.metadata['Name'].lower() == 'setuptools']
            if (root/'setuptools/package_index.py').is_file() and not versions:
                raise ValueError('Untracked setuptools PackageIndex module is present')
            setuptools += versions
    installed = []
    with zipfile.ZipFile(seed) as wheel:
        if any(n.startswith('pip/_vendor/setuptools/') or n.endswith('setuptools/package_index.py') for n in wheel.namelist()):
            raise ValueError('Vulnerable setuptools module is present in pip')
        for root in roots:
            if not (root/'pip').is_dir():
                continue
            for name in wheel.namelist():
                if name.startswith('pip/') and not name.endswith('/'):
                    target = root/name
                    if not target.is_file() or target.read_bytes() != wheel.read(name):
                        raise ValueError('Installed pip content differs from the reviewed wheel: '+name)
            installed.append(str(root/'pip'))
    return {'config': config, 'wheel_sha256': hashlib.sha256(seed.read_bytes()).hexdigest(),
            'pip_version': version, 'installed_pip_verified': installed,
            'standalone_setuptools': setuptools, 'affected_module_present': False}


def verify_finding(proof, lock, policy):
    from packaging.version import Version
    rule = policy['pip_vendor']
    expected = lock['bootstrap']
    if (proof['config'] != expected or proof['wheel_sha256'] != rule['wheel_sha256']
            or proof['pip_version'] != rule['pip_version']
            or proof['affected_module_present'] is not False
            or any(Version(v) < Version('78.1.1') for v in proof['standalone_setuptools'])):
        raise ValueError('Pip vendored-component proof does not match the reviewed scope')
    return {'finding': {'cve': rule['cve'], 'package': rule['package'], 'version': rule['version'],
                        'severity': 'HIGH', 'type': 'python-pkg', 'classification': 'lang-pkgs'},
            'authority': rule['authority'], 'maintainer': rule['maintainer'],
            'wheel_sha256': proof['wheel_sha256'], 'reason': 'vulnerable_code_not_present',
            'evidence': proof}


if __name__ == '__main__':
    print(json.dumps(inspect()))
