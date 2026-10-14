#!/usr/bin/env python3
"""Verify signed release metadata, artifacts and cached installation sources."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile

CONTROL = Path('/etc/betterdocker')
STATE = Path('/var/lib/betterdocker/installer/release.json')
MANIFEST = '.release.json'
SIGNATURE = '.release.sig'
SOURCE_PATHS = ('assets', 'frontend', 'backend', 'scripts', 'systemd', 'packaging', 'install.sh', 'start.sh', 'README.md', 'API_DOCS.md', 'CONTRIBUTING.md', 'SECURITY.md', 'RELEASE_NOTES.md', 'requirements-test.txt')


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def fingerprint(key):
    result = subprocess.run(['openssl', 'pkey', '-pubin', '-in', str(key), '-outform', 'DER'], capture_output=True, check=True)
    return hashlib.sha256(result.stdout).hexdigest()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Invalid release version')
    return tuple(map(int, value.split('.')))


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate release metadata field')
        result[key] = value
    return result


def safe_hashes(values):
    if not isinstance(values, dict) or not values or len(values) > 10000:
        raise ValueError('Invalid release file list')
    for name, sha in values.items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or not path.parts or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('Invalid release filename or checksum')
    return values


def read_manifest(path, signature, key, pin, expected=None):
    if not re.fullmatch(r'[0-9a-f]{64}', pin or '') or fingerprint(key) != pin:
        raise ValueError('Release public-key fingerprint mismatch')
    raw = Path(path).read_bytes()
    if len(raw) > 4 * 1024 * 1024 or Path(signature).stat().st_size > 16384:
        raise ValueError('Release metadata or signature is too large')
    # Parse exactly the bytes authenticated by OpenSSL, even if a caller replaces the file.
    with tempfile.TemporaryDirectory() as folder:
        data = Path(folder) / 'metadata'
        data.write_bytes(raw)
        subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(key), '-signature', str(signature), str(data)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    result = json.loads(raw, object_pairs_hook=pairs)
    if set(result) != {'format', 'version', 'artifacts', 'source'} or type(result['format']) is not int or result['format'] != 1:
        raise ValueError('Unsupported release metadata')
    version(result['version'])
    if expected and result['version'] != expected:
        raise ValueError('Release version mismatch')
    safe_hashes(result['artifacts']); safe_hashes(result['source'])
    names = {'install.sh', 'betterdockers-' + result['version'] + '.tar.gz', 'proxmox-betterdockers_' + result['version'] + '_all.deb'}
    if set(result['artifacts']) != names:
        raise ValueError('Release must identify installer, source archive and DEB')
    return result


def verify_artifact(metadata, path, name):
    if name not in metadata['artifacts'] or digest(path) != metadata['artifacts'][name]:
        raise ValueError('Release artifact checksum mismatch: ' + name)


def tree(root, selected=SOURCE_PATHS):
    result = {}
    for name in selected:
        base = Path(root) / name
        paths = sorted(base.rglob('*')) if base.is_dir() else [base]
        for path in paths:
            if '__pycache__' in path.parts or path.suffix == '.pyc' or path.name == '.DS_Store':
                continue
            if path.is_symlink():
                raise ValueError('Release source cannot contain symbolic links')
            if path.is_file():
                result[str(path.relative_to(root))] = digest(path)
            elif not path.is_dir():
                raise ValueError('Missing or special release source file: ' + str(path))
    return result


def verify_tree(root, metadata, selected=SOURCE_PATHS):
    actual = tree(root, selected)
    expected = {name: sha for name, sha in metadata['source'].items() if name.split('/')[0] in selected}
    if actual != expected:
        raise ValueError('Installation source differs from signed release')


def protected(path, allow_sticky=False):
    for part in [path, *path.parents]:
        stat = part.lstat()
        sticky_parent = allow_sticky and part != path and part.is_dir() and stat.st_mode & 0o1000
        if part.is_symlink() or stat.st_uid != 0 or (stat.st_mode & 0o022 and not sticky_parent):
            raise ValueError('Release trust files must be root-owned and protected')


def trust():
    key, pin = CONTROL / 'release-key.pem', CONTROL / 'release-key.sha256'
    protected(key); protected(pin)
    return key, pin.read_text().strip()


def check_version(metadata, allow_downgrade=False, state=None, manifest_digest=None):
    state = STATE if state is None else state
    if state.exists():
        protected(state)
        saved = json.loads(state.read_text())
        installed = saved['version']
        if metadata['version'] == installed and manifest_digest and saved.get('manifest_sha256') != manifest_digest and not allow_downgrade:
            raise ValueError('This version already identifies a different signed release')
        if version(metadata['version']) < version(installed) and not allow_downgrade:
            raise ValueError('Downgrade refused; use --allow-downgrade deliberately')


def source(root, allow_downgrade=False):
    protected(root, allow_sticky=True)
    for name in (*SOURCE_PATHS, MANIFEST, SIGNATURE):
        base = root / name
        if base.exists():
            for path in ([base, *base.rglob('*')] if base.is_dir() else [base]):
                protected(path, allow_sticky=True)
    key, pin = trust()
    metadata = read_manifest(root / MANIFEST, root / SIGNATURE, key, pin)
    verify_tree(root, metadata, tuple(name for name in SOURCE_PATHS if (root / name).exists()))
    declared = re.search(r'''^VERSION\s*=\s*['"]([^'"]+)['"]\s*$''', (root / 'scripts/dctlib/version.py').read_text(), re.MULTILINE)
    if not declared or declared.group(1) != metadata['version']:
        raise ValueError('Source version differs from signed release')
    # The complete installed payload is mandatory, not merely whichever files survived.
    for name in ('assets', 'frontend', 'backend', 'scripts', 'systemd', 'install.sh', 'start.sh'):
        if not (root / name).exists():
            raise ValueError('Incomplete signed installation source')
    check_version(metadata, allow_downgrade, manifest_digest=digest(root / MANIFEST))
    return metadata


def record(root):
    metadata = source(root, os.environ.get('BETTERDOCKER_ALLOW_DOWNGRADE') == '1')
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    protected(STATE.parent)
    fd, temporary = tempfile.mkstemp(dir=STATE.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump({'version': metadata['version'], 'manifest_sha256': digest(root / MANIFEST)}, handle)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, STATE)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def package_lock():
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    protected(STATE.parent)
    path = STATE.parent / 'release-deb.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('Unprotected DEB authorization lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('source', 'record', 'artifact', 'deb'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--signature', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--allow-downgrade', action='store_true')
    args = parser.parse_args()
    allow = args.allow_downgrade or os.environ.get('BETTERDOCKER_ALLOW_DOWNGRADE') == '1'
    if args.action == 'source':
        source(args.path, allow)
    elif args.action == 'record':
        record(args.path)
    else:
        key, pin = trust()
        metadata = read_manifest(args.manifest, args.signature, key, pin, args.version)
        check_version(metadata, allow, manifest_digest=digest(args.manifest))
        verify_artifact(metadata, args.path, args.path.name)
        if args.action == 'deb':
            if os.geteuid() != 0:
                raise ValueError('Run as root')
            # Install a private copy of the bytes that were checked.
            with package_lock(), tempfile.TemporaryDirectory(prefix='betterdocker-deb-') as folder:
                copy = Path(folder) / args.path.name
                copy.write_bytes(args.path.read_bytes())
                verify_artifact(metadata, copy, copy.name)
                authorization = STATE.parent / 'deb-authorization'
                authorization.mkdir(mode=0o700, parents=True, exist_ok=True)
                protected(authorization)
                if any(authorization.iterdir()):
                    raise ValueError('Another DEB installation authorization exists')
                try:
                    (authorization / MANIFEST).write_bytes(args.manifest.read_bytes())
                    (authorization / SIGNATURE).write_bytes(args.signature.read_bytes())
                    (authorization / MANIFEST).chmod(0o600)
                    (authorization / SIGNATURE).chmod(0o600)
                    package = authorization / copy.name
                    package.write_bytes(copy.read_bytes())
                    verify_artifact(metadata, package, package.name)
                    subprocess.run(['dpkg', '-i', str(package)], check=True, env=dict(os.environ, BETTERDOCKER_ALLOW_DOWNGRADE='1' if allow else '0'))
                finally:
                    for item in authorization.iterdir():
                        item.unlink()
                    authorization.rmdir()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
