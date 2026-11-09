"""Install the reviewed official headless shell, preserving namespace sandboxing."""
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
from urllib.request import urlopen
import zipfile


def component(chrome):
    return {'type': 'application', 'name': 'chrome-headless-shell', 'version': chrome['version'],
            'purl': 'pkg:generic/google/chrome-headless-shell@'+chrome['version'],
            'cpe': 'cpe:2.3:a:google:chrome:'+chrome['version']+':*:*:*:*:linux:*:*',
            'hashes': [{'alg': 'SHA-256', 'content': chrome['sha256']}],
            'externalReferences': [{'type': 'distribution', 'url': chrome['url']}]}


def install():
    chrome = json.loads(Path('/opt/build/lock.json').read_text())['chromium']
    version = chrome['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        raise ValueError('Invalid Chromium version')
    expected = 'https://storage.googleapis.com/chrome-for-testing-public/'+version+'/linux64/chrome-headless-shell-linux64.zip'
    if chrome['url'] != expected or not re.fullmatch('[a-f0-9]{64}', chrome['sha256']):
        raise ValueError('Unreviewed Chromium artifact')
    with urlopen(expected, timeout=180) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != chrome['sha256']:
        raise ValueError('Chromium artifact checksum mismatch')
    root = Path('/opt/chromium')
    root.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for entry in bundle.infolist():
            relative = Path(entry.filename)
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'chrome-headless-shell-linux64':
                raise ValueError('Unsafe Chromium archive entry')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unexpected Chromium archive symlink')
            target = root/relative
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(entry))
                target.chmod(0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)
    binary = root/'chrome-headless-shell-linux64/chrome-headless-shell'
    binary.chmod(0o755)
    Path('/usr/bin/chromium-headless-shell').symlink_to(binary)
    actual = subprocess.check_output([str(binary), '--version'], text=True)
    if version not in actual:
        raise ValueError('Installed Chromium version differs from the lock')
    dependencies = subprocess.check_output(['ldd', str(binary)], text=True)
    if 'not found' in dependencies:
        raise ValueError('Chromium runtime library missing: '+dependencies)
    sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1, 'components': [component(chrome)]}
    (root/'sbom.cdx.json').write_text(json.dumps(sbom, indent=2)+'\n')


if __name__ == '__main__':
    install()
