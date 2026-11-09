"""Update only Python/npm bootstrap tooling on an immutable ready base."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile


def download(item, target):
    with urlopen(item['url'], timeout=180) as response:
        raw = response.read()
    if 'sha256' in item:
        valid = hashlib.sha256(raw).hexdigest() == item['sha256']
    else:
        valid = 'sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode() == item['integrity']
    if not valid:
        raise ValueError('Bootstrap artifact checksum mismatch')
    target.write_bytes(raw)


def install(kind):
    config = json.loads(Path('/opt/bootstrap/bootstrap.json').read_text())
    pip = config['pip']
    if (pip['version'] != '26.3' or not pip['url'].startswith('https://files.pythonhosted.org/packages/')
            or not pip['url'].endswith('/pip-26.3-py3-none-any.whl')
            or not re.fullmatch('[a-f0-9]{64}', pip['sha256'])):
        raise ValueError('Unreviewed pip bootstrap artifact')
    with tempfile.TemporaryDirectory(prefix='base-bootstrap-') as folder:
        work = Path(folder)
        wheel = work/'pip-26.3-py3-none-any.whl'
        download(pip, wheel)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '--upgrade', str(wheel)], check=True)
        import ensurepip
        init = Path(ensurepip.__file__)
        text = init.read_text()
        updated, count = re.subn(r'^_PIP_VERSION = "[^"]+"$', '_PIP_VERSION = "26.3"', text, flags=re.M)
        if count != 1:
            raise ValueError('CPython ensurepip layout changed')
        bundled = init.parent/'_bundled'
        for old in bundled.glob('pip-*.whl'):
            old.unlink()
        shutil.copy2(wheel, bundled/wheel.name)
        init.write_text(updated)
        subprocess.run([sys.executable, '-c',
                        'import ensurepip; assert ensurepip.version()=="26.3"'], check=True)
        if kind == 'builder':
            npm = config['npm']
            if npm['version'] != '11.21.2' or npm['url'] != 'https://registry.npmjs.org/npm/-/npm-11.21.2.tgz':
                raise ValueError('Unreviewed npm bootstrap artifact')
            archive = work/'npm.tgz'
            download(npm, archive)
            subprocess.run(['npm', 'install', '--global', '--no-audit', '--no-fund', str(archive)], check=True)
            actual = subprocess.check_output(['npm', '--version'], text=True).strip()
            if actual != npm['version']:
                raise ValueError('Installed npm differs from its lock')
        elif kind != 'runtime':
            raise ValueError('Unknown base kind')
    Path('/opt/bootstrap/provenance.json').write_text(json.dumps(config, indent=2)+'\n')


if __name__ == '__main__':
    install(sys.argv[1])
