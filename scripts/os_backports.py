"""Build reviewed Debian source packages against stable libraries, retaining provenance."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from datetime import datetime
from email.utils import format_datetime
from urllib.request import urlopen


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def download(item, target):
    with urlopen(item['url'], timeout=90) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Upstream input checksum mismatch')
    target.write_bytes(data)


def prepare_source(name, source, work, resolved_at):
    root = work/'build'
    if upstream := source.get('upstream'):
        archive = work/'upstream.tar.xz'
        download(upstream, archive)
        directory = upstream['directory']
        if Path(directory).name != directory:
            raise ValueError('Invalid upstream directory')
        with tarfile.open(archive) as tar:
            if any(m.name != directory and not m.name.startswith(directory+'/') for m in tar.getmembers()):
                raise ValueError('Unexpected upstream archive layout')
            tar.extractall(work, filter='data')
        target = work/directory
        shutil.copytree(root/'debian', target/'debian')
        root = target
        if name == 'libx11':
            # Explicit additions from the reviewed release; keep strict symbol checking.
            symbols = root/'debian/libx11-6.symbols'
            lines = symbols.read_text().splitlines()
            position = next(i for i, line in enumerate(lines) if line.startswith('libX11.so.6 '))+1
            lines[position:position] = [' _XimXTransSocket'+suffix+'Funcs@Base 2:1.8.14'
                for suffix in ('INET6', 'INET', 'Local', 'TCP', 'UNIX')]
            symbols.write_text('\n'.join(lines)+'\n')
    if name == 'sqlite3':
        if source.get('build_tcl') != '9.0':
            raise ValueError('Unreviewed SQLite build interpreter')
        control, rules = root/'debian/control', root/'debian/rules'
        if control.read_text().count('tcl9.1-dev') != 1 or rules.read_text().count('TCLV_SECONDARY = 9.1') != 1:
            raise ValueError('SQLite packaging changed; review required')
        control.write_text(control.read_text().replace('tcl9.1-dev', 'tcl9.0-dev'))
        rules.write_text(rules.read_text().replace('TCLV_SECONDARY = 9.1', 'TCLV_SECONDARY = 9.0'))
    if patch := source.get('patch'):
        target = root/'debian/patches/container-CVE-2026-37555.patch'
        download(patch, target)
        with (root/'debian/patches/series').open('a') as handle:
            handle.write('\n'+target.name+'\n')
        run('dpkg-source', '--before-build', '.', cwd=root)
    if version := source.get('binary_version'):
        changelog = root/'debian/changelog'
        date = format_datetime(datetime.fromisoformat(resolved_at))
        changelog.write_text(f'{name} ({version}) unstable; urgency=medium\n\n'
            '  * Rebuild reviewed upstream security fixes for the community container.\n\n'
            f' -- Container build <container@example.invalid>  {date}\n\n'+changelog.read_text())
    return root


def main():
    lock = json.loads(Path('/opt/build/lock.json').read_text())
    packages = lock['os_backports']
    if set(packages) != {'acl', 'ncurses', 'perl', 'systemd', 'libsndfile', 'libx11', 'libxrender', 'libxtst', 'expat', 'sqlite3', 'libxi'}:
        raise ValueError('Unreviewed OS backport set')
    # Binary dependencies always come from stable; sid is source-only.
    Path('/etc/apt/sources.list.d/backport-source.list').write_text(
        'deb-src [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian sid main\n')
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', '--no-install-recommends', 'build-essential', 'dpkg-dev', 'fakeroot')
    run('useradd', '--create-home', 'package-builder')
    out = Path('/backports')
    out.mkdir()
    build_out = Path('/build-deps')
    build_out.mkdir()
    build_wanted = {'libx11': {'libx11-dev'}, 'libxrender': {'libxrender-dev'},
                    'expat': {'libexpat1-dev'}, 'sqlite3': {'libsqlite3-dev'}, 'libxi': {'libxi-dev'}}
    evidence = {}
    wanted = {'acl': {'libacl1'},
              'ncurses': {'libtinfo6', 'libncurses6', 'libncursesw6', 'ncurses-base', 'ncurses-bin'},
              'perl': {'perl', 'perl-base', 'perl-modules-5.42', 'libperl5.42'},
              'systemd': {'libsystemd0', 'libudev1'}, 'libsndfile': {'libsndfile1'},
              'libx11': {'libx11-6', 'libx11-data', 'libx11-xcb1'},
              'libxrender': {'libxrender1'}, 'libxtst': {'libxtst6'},
              'expat': {'libexpat1'}, 'sqlite3': {'libsqlite3-0'}, 'libxi': {'libxi6'}}
    for name, source in packages.items():
        version = source['version']
        work = Path('/source')/name
        work.mkdir(parents=True)
        if name != 'sqlite3':
            run('apt-get', 'build-dep', '-y', '--no-install-recommends', name+'='+version)
        run('apt-get', 'source', '--download-only', name+'='+version, cwd=work)
        for filename, digest in source['files'].items():
            if Path(filename).name != filename:
                raise ValueError('Invalid source filename')
            if hashlib.sha256((work/filename).read_bytes()).hexdigest() != digest:
                raise ValueError('OS source checksum mismatch: '+filename)
        dsc, = work.glob('*.dsc')
        run('dpkg-source', '-x', str(dsc), str(work/'build'))
        root = prepare_source(name, source, work, lock['resolved_at'])
        if name == 'sqlite3':
            run('apt-get', 'build-dep', '-y', '--no-install-recommends', str(root))
        run('chown', '-R', 'package-builder:package-builder', str(work))
        # Do not disable upstream/package tests with nocheck.
        # Debian's build helpers must use Debian Python and its packaged modules.
        env = dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin')
        run('runuser', '-u', 'package-builder', '--', 'dpkg-buildpackage', '-b', '-us', '-uc', '-j2', cwd=root, env=env)
        if name == 'sqlite3':
            run('runuser', '-u', 'package-builder', '--', 'make', '-C', str(root/'debian/8.6'), 'quicktest', cwd=root, env=env)
        installed = {}
        build_installed = {}
        for deb in work.glob('*.deb'):
            package = subprocess.check_output(['dpkg-deb', '-f', str(deb), 'Package'], text=True).strip()
            if package in wanted[name] or package in build_wanted.get(name, set()):
                runtime = package in wanted[name]
                shutil.copy2(deb, (out if runtime else build_out)/deb.name)
                binary_version = subprocess.check_output(['dpkg-deb', '-f', str(deb), 'Version'], text=True).strip()
                if binary_version != source.get('binary_version', version):
                    raise ValueError('Unexpected binary package version: '+package)
                (installed if runtime else build_installed)[package] = {'file': deb.name, 'version': binary_version,
                                      'sha256': hashlib.sha256(deb.read_bytes()).hexdigest()}
        if set(installed) != wanted[name]:
            raise ValueError('Missing backport binary packages: '+name)
        if set(build_installed) != build_wanted.get(name, set()):
            raise ValueError('Missing matching builder packages: '+name)
        evidence[name] = {'source': source, 'binaries': installed,
                          'build_only_binaries': build_installed, 'tests_disabled': False}
        shutil.rmtree(work)
    (out/'provenance.json').write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    main()
