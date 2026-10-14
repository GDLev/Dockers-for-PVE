#!/usr/bin/env python3
"""Build and authenticate disposable releases. Never install packages or trust keys."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release
import installer


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this disposable package check as Linux root; no package is installed.')
    with tempfile.TemporaryDirectory(prefix='bd-release-build-', dir='/root') as folder:
        directory = Path(folder)
        source = directory / 'source'; source.mkdir()
        for name in release.SOURCE_PATHS:
            path = ROOT / name
            if path.is_dir():
                shutil.copytree(path, source / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
            else:
                shutil.copy2(path, source / name)
        key = directory / 'disposable-test-key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['bash', str(source / 'packaging/build-deb.sh')], check=True)
        subprocess.run(['bash', str(source / 'packaging/build-release.sh')], check=True)
        subprocess.run(['python3', str(source / 'packaging/sign-release.py'), '--key', str(key)], check=True)
        dist = source / 'dist'
        version = subprocess.check_output(['python3', str(source / 'scripts/dctlib/version.py')], text=True).strip()
        metadata = release.read_manifest(dist / 'RELEASE.json', dist / 'RELEASE.sig', dist / 'release-key.pem', release.fingerprint(dist / 'release-key.pem'), version)
        for name in metadata['artifacts']:
            release.verify_artifact(metadata, dist / name, name)
        archive = directory / 'archive'; archive.mkdir()
        subprocess.run(['tar', '-xzf', str(dist / ('betterdockers-' + version + '.tar.gz')), '-C', str(archive)], check=True)
        release.verify_tree(archive / ('betterdockers-' + version), metadata)
        package = directory / 'package'; package.mkdir()
        subprocess.run(['dpkg-deb', '--extract', str(dist / ('proxmox-betterdockers_' + version + '_all.deb')), str(package)], check=True)
        release.verify_tree(package / 'opt/betterdocker', metadata, ('assets', 'frontend', 'backend', 'scripts', 'systemd', 'install.sh', 'start.sh'))
        controls = directory / 'controls'; controls.mkdir()
        subprocess.run(['dpkg-deb', '--control', str(dist / ('proxmox-betterdockers_' + version + '_all.deb')), str(controls)], check=True)
        trust = directory / 'trust'; trust.mkdir()
        shutil.copy2(dist / 'release-key.pem', trust / 'release-key.pem')
        (trust / 'release-key.sha256').write_text(release.fingerprint(trust / 'release-key.pem') + '\n')
        for root in (archive / ('betterdockers-' + version), package / 'opt/betterdocker'):
            (root / release.MANIFEST).write_bytes((dist / 'RELEASE.json').read_bytes())
            (root / release.SIGNATURE).write_bytes((dist / 'RELEASE.sig').read_bytes())
            with patch.object(release, 'CONTROL', trust), patch.object(release, 'STATE', directory / 'installed.json'):
                release.source(root)
        class FixtureNode:
            def __init__(self, corrupt=None):
                self.folder = Path(tempfile.mkdtemp(prefix='betterdocker-install.'))
                self.corrupt = corrupt
            def run(self, node, args, **kwargs):
                if args[0] == 'mktemp':
                    return subprocess.CompletedProcess(args, 0, stdout=str(self.folder) + '\n')
                if args[0:2] == ['python3', '-c'] and 'metadata = read_manifest' in args[2]:
                    args = list(args)
                    args[2] = args[2].replace("CONTROL = Path('/etc/betterdocker')", 'CONTROL = Path(' + repr(str(trust)) + ')').replace("STATE = Path('/var/lib/betterdocker/installer/release.json')", 'STATE = Path(' + repr(str(directory / 'installed.json')) + ')')
                elif args[0] == 'python3' and args[1].endswith('/scripts/release.py'):
                    with patch.object(release, 'CONTROL', trust), patch.object(release, 'STATE', directory / 'installed.json'):
                        release.source(Path(args[3]))
                    return subprocess.CompletedProcess(args, 0)
                if args[0] == 'bash' and kwargs.get('input') is not None:
                    if self.corrupt == 'signature' and 'transport.release.sig' in args[2] or self.corrupt == 'archive' and 'source.tar.gz' in args[2]:
                        kwargs['input'] = b'changed ordinary transfer fixture'
                return subprocess.run(args, **kwargs)
        signed_source = archive / ('betterdockers-' + version)
        for corrupt in (None, 'signature', 'archive'):
            node = FixtureNode(corrupt)
            runner = installer.Runner(node, None)
            try:
                with patch.object(installer, 'SOURCE', signed_source), patch.object(release, 'CONTROL', trust), patch.object(release, 'STATE', directory / 'installed.json'):
                    try:
                        runner.prepare(['fixture'])
                    except subprocess.CalledProcessError:
                        if corrupt is None:
                            raise
                    else:
                        if corrupt is not None:
                            raise AssertionError('Changed transfer accepted')
                if corrupt is not None and (node.folder / 'scripts').exists():
                    raise AssertionError('Changed transfer reached source extraction')
                if corrupt == 'signature' and (node.folder / 'source.tar.gz').exists():
                    raise AssertionError('Untrusted target metadata reached archive transfer')
            finally:
                shutil.rmtree(node.folder)
        cached = package / 'opt/betterdocker/scripts/release.py'
        cached.write_bytes(cached.read_bytes() + b'\n# ordinary changed-cache fixture\n')
        with patch.object(release, 'CONTROL', trust), patch.object(release, 'STATE', directory / 'installed.json'):
            try:
                release.source(package / 'opt/betterdocker')
            except ValueError:
                pass
            else:
                raise AssertionError('Signed source admission accepted changed cached helper')
        auth = directory / 'authorization'; auth.mkdir(mode=0o700)
        (auth / release.MANIFEST).write_bytes((dist / 'RELEASE.json').read_bytes())
        (auth / release.SIGNATURE).write_bytes((dist / 'RELEASE.sig').read_bytes())
        deb = dist / ('proxmox-betterdockers_' + version + '_all.deb')
        shutil.copy2(deb, auth / deb.name)
        preinst = (controls / 'preinst').read_text().replace('/etc/betterdocker', str(trust)).replace('/var/lib/betterdocker/installer/deb-authorization', str(auth))
        guard = controls / 'fixture-preinst'; guard.write_text(preinst)
        # Exercise the generated guard with private fixture paths. No dpkg install,
        # post-install script, host trust file or runtime operation is executed.
        subprocess.run(['sh', '-n', str(guard)], check=True)
        subprocess.run(['sh', str(guard), 'install'], check=True)
        (auth / deb.name).write_bytes(b'changed ordinary package fixture')
        denied = subprocess.run(['sh', str(guard), 'install'], capture_output=True)
        if denied.returncode == 0:
            raise AssertionError('DEB pre-install guard accepted changed package')
        (auth / release.MANIFEST).write_text('{}')
        denied = subprocess.run(['sh', str(guard), 'upgrade'], capture_output=True)
        if denied.returncode == 0:
            raise AssertionError('DEB pre-install guard accepted changed metadata')
        (dist / 'install.sh').write_text('changed ordinary installer fixture\n')
        try:
            release.verify_artifact(metadata, dist / 'install.sh', 'install.sh')
        except ValueError:
            pass
        else:
            raise AssertionError('Changed installer accepted')
        print('PASS disposable source/DEB/installer build, signing, payload matching and altered-installer rejection')
        print('PASS actual DEB pre-install guard accepts matching fixtures and rejects changed package/metadata')
        print('PASS cluster transfer fixture authenticates before isolated extraction and rejects altered signature/archive')
        print('No package installed, production key provisioned or runtime changed.')


if __name__ == '__main__':
    main()
