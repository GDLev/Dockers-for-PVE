"""Bounded restore staging before PBS download or archive decompression."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from . import operation_capacity as admission, retention, space
from .secure_io import anchored, read_json

ROOT = Path('/var/lib/betterdocker/restore-staging')
TEMP = Path('/var/tmp')
MiB = 1024 ** 2


def capacity():
    chosen = space.policy()
    free = shutil.disk_usage(TEMP).free - chosen['reserve_bytes']
    # Leave room for the imported images/data, and charge both the backing file
    # and its mounted contents to the temporary transfer budget.
    maximum = min(retention.policy()['transfer_bytes'] // 2,
                  free * 100 // (2 * (100 + chosen['overhead_percent'])))
    maximum = maximum // MiB * MiB
    if maximum < 16 * MiB:
        raise ValueError('Insufficient peak space for restore staging')
    return maximum


def cleanup(record):
    from . import projects as w
    token = w.project_id(record['token'])
    parent = TEMP / ('betterdocker-restore-' + token)
    if not parent.exists():
        (ROOT / (token + '.json')).unlink(missing_ok=True)
        return
    with anchored(parent, directory=True) as fd:
        info = os.fstat(fd)
        if [info.st_dev, info.st_ino] != record['directory']:
            raise ValueError('Restore staging ownership changed; recovery retained')
    image, mount = parent / 'staging.img', parent / 'filesystem'
    if mount.is_mount():
        source = json.loads(w.capture(['findmnt', '--json', '--mountpoint', str(mount)]))['filesystems'][0]['source']
        devices = json.loads(w.capture(['losetup', '--list', '--json', '--output', 'NAME,BACK-FILE']))['loopdevices']
        if not any(row['name'] == source and row['back-file'] == str(image) for row in devices):
            raise ValueError('Restore staging mount changed; recovery retained')
        w.run(['umount', str(mount)], capture_output=True)
    shutil.rmtree(parent)
    (ROOT / (token + '.json')).unlink(missing_ok=True)


def sweep():
    if not ROOT.exists():
        return 0
    removed = 0
    with anchored(ROOT, directory=True) as directory:
        gate = admission.open_lock(directory, 'cleanup.lock')
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            for path in ROOT.glob('*.json'):
                record = read_json(path)
                if path.stem != record['token']:
                    raise ValueError('Invalid restore staging journal')
                if not retention.active(record['owner']):
                    cleanup(record)
                    removed += 1
        finally:
            os.close(gate)
    return removed


@contextlib.contextmanager
def staging():
    from . import projects as w
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    sweep()
    token = uuid.uuid4().hex
    parent = TEMP / ('betterdocker-restore-' + token)
    parent.mkdir(mode=0o700)
    info = parent.stat()
    record = {'token': token, 'directory': [info.st_dev, info.st_ino], 'owner': space.slot_owner()}
    w.write_json(ROOT / (token + '.json'), record)
    try:
        retention.register([parent])
        maximum = capacity()
        image, mount = parent / 'staging.img', parent / 'filesystem'
        mount.mkdir(mode=0o700)
        with space.reserve([space.filesystem(parent, maximum)], key=token):
            fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            w.run(['fallocate', '--length', str(maximum), str(image)], capture_output=True)
            if image.stat().st_blocks * 512 < maximum:
                raise ValueError('Restore staging requires fully allocated backing storage')
            # The host filesystem now accounts for these bytes itself. Keeping
            # the claim would let later work reuse an already consumed budget.
            space.release(token)
        w.run(['mkfs.ext4', '-q', '-F', '-m', '0', '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0', str(image)], capture_output=True)
        w.run(['mount', '-o', 'loop,nodev,nosuid,noexec,nodiscard', str(image), str(mount)], capture_output=True)
        os.chmod(mount, 0o700)
        yield mount
    finally:
        cleanup(record)


@admission.bounded
def restore(volid, identifier, node, options):
    from . import projects as w
    import guest_state
    w.project_id(identifier)
    if node != w.current_node():
        raise ValueError('Restore was sent to the wrong node')
    adapter = str(w.HELPERS / 'backup.pl')
    with staging() as directory:
        source = json.loads(w.capture(['perl', adapter, 'source', volid]))
        state = directory / 'state'
        if source['type'] == 'pbs':
            w.run(['perl', adapter, 'fetch', volid, str(state)], timeout=86400, capture_output=True)
        elif source['type'] == 'file':
            archive = Path(source['path'])
            if source.get('compression'):
                command = {'gz': ['gzip', '-dc'], 'zst': ['zstd', '-q', '-dc'],
                           'lzo': ['lzop', '-dc'], 'bz2': ['bzip2', '-dc']}.get(source['compression'])
                if command is None:
                    raise ValueError('Unsupported DCT backup compression')
                with anchored(archive) as fd, (directory / 'archive.tar').open('xb') as output:
                    os.chmod(output.name, 0o600)
                    w.run([*command, '--', '/proc/self/fd/' + str(fd)], pass_fds=(fd,), stdout=output, stderr=subprocess.PIPE, timeout=86400)
                archive = directory / 'archive.tar'
            guest_state.unpack_state(archive, state)
        else:
            raise ValueError('Unsupported DCT backup source')
        value = read_json(Path(options))
        return guest_state.restore_state(state, identifier, node, value.get('deployment', {}), value.get('storage', ''))
