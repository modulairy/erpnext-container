"""Rebuild the matching CPython XML modules against the reviewed system Expat."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
from urllib.request import urlopen

FILES = {'pyexpat.c': 'Modules/pyexpat.c', '_elementtree.c': 'Modules/_elementtree.c',
         'clinic/_elementtree.c.h': 'Modules/clinic/_elementtree.c.h',
         'clinic/pyexpat.c.h': 'Modules/clinic/pyexpat.c.h',
         'expat_config.h': 'Modules/expat/expat_config.h', 'pyexpat.h': 'Include/pyexpat.h'}


def main():
    lock = json.loads(Path('/opt/build/lock.json').read_text())
    if platform.python_version() != lock['python'] or set(lock['pyexpat_sources']) != set(FILES):
        raise ValueError('CPython XML module source/version mismatch')
    packages = list(Path('/backports').glob('libexpat1_*.deb'))+list(Path('/build-deps').glob('libexpat1-dev_*.deb'))
    if len(packages) != 2:
        raise ValueError('Missing matching reviewed Expat build packages')
    subprocess.run(['apt-get', 'install', '-y', '--no-remove', '--no-install-recommends',
                    *map(str, packages)], check=True)
    compile_modules(lock, Path('/tmp/cpython-xml-source'), Path('/backports/pyexpat'))


def compile_modules(lock, work, out):
    work = Path(work)
    out = Path(out)
    work.mkdir()
    for name, relative in FILES.items():
        item = lock['pyexpat_sources'][name]
        url = f'https://raw.githubusercontent.com/python/cpython/v{lock["python"]}/{relative}'
        if item['url'] != url:
            raise ValueError('Unexpected CPython source authority')
        with urlopen(url, timeout=90) as response:
            source = response.read()
        if hashlib.sha256(source).hexdigest() != item['sha256']:
            raise ValueError('CPython source checksum mismatch')
        target = work/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
    out.mkdir()
    include = Path(sysconfig.get_path('include'))
    binaries = {}
    for name in ('pyexpat', '_elementtree'):
        target = out/(name+sysconfig.get_config_var('EXT_SUFFIX'))
        subprocess.run(['gcc', '-shared', '-fPIC', '-O2', '-fstack-protector-strong',
                        '-Wl,-z,relro,-z,now', '-I'+str(include), '-I'+str(include/'internal'),
                        '-I'+str(work), str(work/(name+'.c')), '-lexpat', '-o', str(target)], check=True)
        binaries[name] = {'file': target.name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    env = dict(os.environ, PYTHONPATH=str(out))
    check = ("import json,pyexpat,xml.etree.ElementTree as ET; "
             "assert tuple(map(int,pyexpat.EXPAT_VERSION.split('_')[-1].split('.'))) >= (2,10,0); "
             "assert pyexpat.ParserCreate().Parse('<root>ok</root>',True)==1; "
             "assert ET.fromstring('<root><item>ok</item></root>')[0].text=='ok'; "
             "print(json.dumps({'expat_version':pyexpat.EXPAT_VERSION}))")
    actual = json.loads(subprocess.check_output([sys.executable, '-c', check], text=True, env=env))
    (out/'provenance.json').write_text(json.dumps(
        {'python': lock['python'], 'sources': lock['pyexpat_sources'], 'binaries': binaries,
         'expat_version': actual['expat_version'], 'tests_passed': True}, indent=2)+'\n')
    shutil.rmtree(work)


if __name__ == '__main__':
    main()
