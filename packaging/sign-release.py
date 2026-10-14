#!/usr/bin/env python3
"""Run outside ordinary build CI with an externally held signing key."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import re

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--key', required=True, type=Path)
parser.add_argument('--output', type=Path, default=ROOT / 'dist')
args = parser.parse_args()
if args.key.resolve().is_relative_to(ROOT):
    parser.error('Private signing key must be outside the repository')
version = subprocess.check_output([sys.executable, str(ROOT / 'scripts/dctlib/version.py')], text=True).strip()
subprocess.run(['openssl', 'pkey', '-in', str(args.key), '-pubout', '-out', str(args.output / 'release-key.pem')], check=True)
subprocess.run(['openssl', 'rsa', '-pubin', '-in', str(args.output / 'release-key.pem'), '-noout'], check=True, capture_output=True)
public = subprocess.check_output(['openssl', 'pkey', '-pubin', '-in', str(args.output / 'release-key.pem'), '-text', '-noout'], text=True)
bits = re.search(r'Public-Key: \((\d+) bit\)', public)
if not bits or int(bits.group(1)) < 3072:
    raise ValueError('Release signing requires an RSA key of at least 3072 bits')
if re.search(r'local release_version=([0-9.]+)', (ROOT / 'install.sh').read_text()).group(1) != version:
    raise ValueError('Installer version differs from release')
shutil.copy2(ROOT / 'install.sh', args.output / 'install.sh')
files = ['install.sh', 'betterdockers-' + version + '.tar.gz', 'proxmox-betterdockers_' + version + '_all.deb']
# Source hashes must describe the archive, not a checkout that changed after building.
import tarfile
import hashlib
import io
with tarfile.open(args.output / files[1]) as archive:
    source = {}
    for member in archive:
        if member.isfile():
            name = str(Path(member.name).relative_to('betterdockers-' + version))
            source[name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
        elif not member.isdir():
            raise ValueError('Release archive contains a special file')
if source != release.tree(ROOT):
    raise ValueError('Rebuild source archive from the reviewed checkout before signing')
package = args.output / files[2]
if subprocess.check_output(['dpkg-deb', '--field', str(package), 'Version'], text=True).strip() != version:
    raise ValueError('DEB version differs from release')
payload = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(package)])
with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
    package_source = {}
    for member in archive:
        name = member.name.removeprefix('./')
        if name.startswith('opt/betterdocker/'):
            if member.isfile():
                package_source[name.removeprefix('opt/betterdocker/')] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            elif not member.isdir():
                raise ValueError('DEB source contains a special file')
expected = {name: sha for name, sha in source.items() if name.split('/')[0] in ('assets', 'frontend', 'backend', 'scripts', 'systemd', 'install.sh', 'start.sh')}
if package_source != expected:
    raise ValueError('DEB payload differs from signed source archive')
manifest = args.output / 'RELEASE.json'
manifest.write_text(json.dumps(dict(format=1, version=version, artifacts={name: release.digest(args.output / name) for name in files}, source=source), sort_keys=True, separators=(',', ':')) + '\n')
subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(args.key), '-out', str(args.output / 'RELEASE.sig'), str(manifest)], check=True)
print('Public-key SHA-256:', release.fingerprint(args.output / 'release-key.pem'))
print('Publish RELEASE.json, RELEASE.sig and the three artifacts only after required checks pass.')
