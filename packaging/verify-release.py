#!/usr/bin/env python3
"""Verify signed release assets against this checkout and the accepted CI build."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release
from dctlib.version import VERSION


def verify(assets, build):
    key = ROOT / 'packaging/release-key.pem'
    pin = release.fingerprint(key)
    metadata = release.read_manifest(assets / 'RELEASE.json', assets / 'RELEASE.sig', key, pin, VERSION)
    release.verify_tree(ROOT, metadata)
    if release.fingerprint(assets / 'release-key.pem') != pin:
        raise ValueError('Uploaded public key differs from the reviewed release key')
    for name in metadata['artifacts']:
        release.verify_artifact(metadata, assets / name, name)
        if name != 'install.sh':
            release.verify_artifact(metadata, build / name, name)
    if metadata['artifacts']['install.sh'] != metadata['source']['install.sh']:
        raise ValueError('Installer differs from the reviewed source')
    names = sorted([*metadata['artifacts'], 'RELEASE.json', 'RELEASE.sig', 'release-key.pem'])
    expected = ''.join(release.digest(assets / name) + '  ' + name + '\n' for name in names)
    if (assets / 'SHA256SUMS').read_text() != expected:
        raise ValueError('Release checksums differ from the verified assets')
    if {p.name for p in assets.iterdir()} != {*names, 'SHA256SUMS'}:
        raise ValueError('Unexpected or missing release assets')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('assets', type=Path)
    parser.add_argument('--build', required=True, type=Path, help='Downloaded artifact from the accepted Validate workflow')
    args = parser.parse_args()
    verify(args.assets, args.build)
    print('Signed assets match the reviewed source and accepted CI build; nothing rebuilt or published.')
