"""Build reviewed Debian source packages against stable libraries, retaining provenance."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    lock = json.loads(Path('/opt/build/lock.json').read_text())
    packages = lock['os_backports']
    if set(packages) != {'acl', 'ncurses', 'perl', 'systemd'}:
        raise ValueError('Unreviewed OS backport set')
    # Binary dependencies always come from stable; sid is source-only.
    Path('/etc/apt/sources.list.d/backport-source.list').write_text(
        'deb-src [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian sid main\n')
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', '--no-install-recommends', 'build-essential', 'dpkg-dev', 'fakeroot')
    run('useradd', '--create-home', 'package-builder')
    out = Path('/backports')
    out.mkdir()
    evidence = {}
    wanted = {'acl': {'libacl1'},
              'ncurses': {'libtinfo6', 'libncurses6', 'libncursesw6', 'ncurses-base', 'ncurses-bin'},
              'perl': {'perl', 'perl-base', 'perl-modules-5.42', 'libperl5.42'},
              'systemd': {'libsystemd0', 'libudev1'}}
    for name, source in packages.items():
        version = source['version']
        work = Path('/source')/name
        work.mkdir(parents=True)
        run('apt-get', 'build-dep', '-y', '--no-install-recommends', name+'='+version)
        run('apt-get', 'source', '--download-only', name+'='+version, cwd=work)
        for filename, digest in source['files'].items():
            if Path(filename).name != filename:
                raise ValueError('Invalid source filename')
            if hashlib.sha256((work/filename).read_bytes()).hexdigest() != digest:
                raise ValueError('OS source checksum mismatch: '+filename)
        dsc, = work.glob('*.dsc')
        run('dpkg-source', '-x', str(dsc), str(work/'build'))
        run('chown', '-R', 'package-builder:package-builder', str(work))
        # Do not disable upstream/package tests with nocheck.
        # Debian's build helpers must use Debian Python and its packaged modules.
        env = dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin')
        run('runuser', '-u', 'package-builder', '--', 'dpkg-buildpackage', '-b', '-us', '-uc', '-j2', cwd=work/'build', env=env)
        installed = {}
        for deb in work.glob('*.deb'):
            package = subprocess.check_output(['dpkg-deb', '-f', str(deb), 'Package'], text=True).strip()
            if package in wanted[name]:
                shutil.copy2(deb, out/deb.name)
                binary_version = subprocess.check_output(['dpkg-deb', '-f', str(deb), 'Version'], text=True).strip()
                installed[package] = {'file': deb.name, 'version': binary_version,
                                      'sha256': hashlib.sha256(deb.read_bytes()).hexdigest()}
        if set(installed) != wanted[name]:
            raise ValueError('Missing backport binary packages: '+name)
        evidence[name] = {'source': source, 'binaries': installed, 'tests_disabled': False}
    (out/'provenance.json').write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    main()
