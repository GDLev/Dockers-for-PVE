#!/usr/bin/env python3
"""Complete, inactive DCT state volumes transported by native PVE replication."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import uuid

import provision as worker
import guest_state as state


def record(identifier):
    path = worker.ROOT / worker.project_id(identifier) / 'replication.json'
    return state.read(path) if path.exists() else {}


def ensure(identifier, node, lock_fd=None):
    with state.operation(identifier, 'replication-configure', lock_fd), worker.locked():
        info = worker.detail(identifier, node)
        info.pop('deployment', None)
        directory = worker.ROOT / identifier
        volume = record(identifier)
        if volume:
            inspected = worker.pve_storage(identifier, 'inspect', volid=volume['volid'])
        else:
            disk = worker.storage_config(identifier)
            if disk.get('type') != 'zfspool':
                raise ValueError('DCT replication requires a managed ZFS data disk')
            inspected = worker.pve_storage(identifier, 'allocate', storage=disk['storage'], size_mib=256)
            volume = dict(inspected, node=node, vmid=info['vmid'], initialized=False)
            state.write(directory / 'replication.json', volume)
            (directory / 'allocated-volume.json').unlink()
        if not volume.get('initialized'):
            worker.run(['mkfs.ext4', '-q', '-m', '0', inspected['path']])
        volume.pop('path', None)
        volume.update(node=node, vmid=info['vmid'], initialized=True)
        state.write(directory / 'replication.json', volume)
        info['replication'] = volume
        worker.write_json(worker.METADATA / (identifier + '.json'), info)
        return volume


def mount_volume(volid, point, readonly=False):
    program = ('use PVE::Storage; my $c=PVE::Storage::config(); '
               'my($s)=PVE::Storage::parse_volume_id($ARGV[0]); '
               'die "ZFS required\\n" unless PVE::Storage::storage_config($c,$s)->{type} eq "zfspool"; '
               'PVE::Storage::activate_volumes($c,[$ARGV[0]]); print scalar(PVE::Storage::path($c,$ARGV[0]));')
    path = worker.capture(['perl', '-e', program, volid]).strip()
    point.mkdir(mode=0o700, parents=True)
    worker.run(['mount', '-o', 'ro,noload,nodev,nosuid' if readonly else 'nodev,nosuid', path, str(point)])


def prepare(identifier, node, lock_fd=None):
    if lock_fd is None:
        with worker.operation(identifier, 'replication') as lock:
            return prepare(identifier, node, lock.fileno())
    with state.operation(identifier, 'replication', lock_fd):
        volume = ensure(identifier, node, lock_fd)
        with tempfile.TemporaryDirectory(prefix='replica-', dir=worker.ROOT.parent) as temporary:
            temporary = Path(temporary)
            state.capture_state(identifier, node, temporary / 'state', lock_fd=lock_fd)
            state.pack_state(temporary / 'state', temporary / 'state.tar')
            size = max(256, (2 * (temporary / 'state.tar').stat().st_size + 64 * 1048576 + 1048575) // 1048576)
            if size > volume['size_mib']:
                with worker.locked():
                    inspected = worker.pve_storage(identifier, 'resize', volid=volume['volid'], size_mib=size)
                    check = worker.run(['e2fsck', '-p', inspected['path']], check=False, capture_output=True)
                    if check.returncode not in (0, 1):
                        raise ValueError('Replication state filesystem check failed')
                    worker.run(['resize2fs', inspected['path']], capture_output=True)
                    volume.update(size_mib=size)
                    state.write(worker.ROOT / identifier / 'replication.json', volume)
                    info = worker.detail(identifier, node); info.pop('deployment', None); info['replication'] = volume
                    worker.write_json(worker.METADATA / (identifier + '.json'), info)
            point = temporary / 'mount'
            mount_volume(volume['volid'], point)
            try:
                shutil.copyfile(temporary / 'state.tar', point / 'state.tar.new')
                os.chmod(point / 'state.tar.new', 0o600)
                (point / 'state.tar.new').replace(point / 'state.tar')
                os.sync()
            finally:
                worker.run(['umount', str(point)])
        # Unmounted ext4 state is immutable throughout native ZFS snapshot/send.


def publish(identifier, node, target, jobid, timestamp, lock_fd=None):
    with state.operation(identifier, 'replication', lock_fd), worker.locked():
        info = worker.detail(identifier, node)
        info.pop('deployment', None)
        if not re.fullmatch(str(info['vmid']) + r'-[0-9]{1,9}', jobid) or target == node:
            raise ValueError('Invalid replication checkpoint identity')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', target) or int(timestamp) <= 0:
            raise ValueError('Invalid replication checkpoint')
        volume = record(identifier)
        snap = '__replicate_' + jobid + '_' + str(int(timestamp)) + '__'
        program = 'use PVE::Storage; use JSON; my $c=PVE::Storage::config(); print encode_json(PVE::Storage::volume_snapshot_info($c,$ARGV[0])->{$ARGV[1]} // die "Replication snapshot missing\\n");'
        snapshot = json.loads(worker.capture(['perl', '-e', program, volume['volid'], snap]))
        volume.setdefault('targets', {})[target] = {'jobid': jobid, 'last_sync': int(timestamp),
                                                   'snapshot': snap, 'guid': snapshot['id']}
        state.write(worker.ROOT / identifier / 'replication.json', volume)
        info['replication'] = volume
        worker.write_json(worker.METADATA / (identifier + '.json'), info)


def cleanup(identifier, node, lock_fd=None):
    with state.operation(identifier, 'replication-cleanup', lock_fd), worker.locked():
        info = worker.detail(identifier, node)
        info.pop('deployment', None)
        program = 'use PVE::ReplicationConfig; my $c=PVE::ReplicationConfig->new(); die "Remove replication jobs first\\n" if grep {$_->{guest} == $ARGV[0]} values %{$c->{ids}};'
        worker.run(['perl', '-e', program, str(info['vmid'])])
        volume = record(identifier)
        if volume:
            worker.pve_storage(identifier, 'free', volid=volume['volid'])
            (worker.ROOT / identifier / 'replication.json').unlink()
        info.pop('replication', None)
        worker.write_json(worker.METADATA / (identifier + '.json'), info)


def recovery_source(volid, snapshot, guid):
    if not re.fullmatch(r'__replicate_[0-9]+-[0-9]+_[0-9]+__', snapshot):
        raise ValueError('A successful replication snapshot is required')
    if not re.fullmatch(r'[0-9]+', str(guid)):
        raise ValueError('A replication snapshot GUID is required')
    program = ('use PVE::Storage; use JSON; my $c=PVE::Storage::config(); '
               'my($s)=PVE::Storage::parse_volume_id($ARGV[0]); '
               'die "ZFS required\\n" unless PVE::Storage::storage_config($c,$s)->{type} eq "zfspool"; '
               'my $i=PVE::Storage::volume_snapshot_info($c,$ARGV[0])->{$ARGV[1]}; '
               'die "Successful replication checkpoint unavailable\\n" unless $i && "$i->{id}" eq $ARGV[2]; '
               'print scalar(PVE::Storage::path($c,$ARGV[0]));')
    path = worker.capture(['perl', '-e', program, volid, snapshot, str(guid)]).strip()
    if not path.startswith('/dev/zvol/') or '..' in path.split('/'):
        raise ValueError('Invalid replication ZFS device')
    return path.removeprefix('/dev/zvol/')


def export(volid, destination, snapshot, guid):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Export destination already exists')
    dataset = recovery_source(volid, snapshot, guid)
    clone = dataset.rsplit('/', 1)[0] + '/betterdocker-recovery-' + uuid.uuid4().hex
    journal = worker.ROOT.parent / (clone.rsplit('/', 1)[1] + '.json')
    # Keep the exact temporary dataset recorded if the process or host dies.
    state.write(journal, {'dataset': clone, 'source': dataset + '@' + snapshot})
    try:
        worker.run(['zfs', 'clone', '-o', 'readonly=on', '-o', 'volmode=dev',
                    dataset + '@' + snapshot, clone])
        # Recheck the selected source after clone creation, including a deletion race.
        recovery_source(volid, snapshot, guid)
        with tempfile.TemporaryDirectory(prefix='replica-read-', dir=worker.ROOT.parent) as temporary:
            point = Path(temporary) / 'mount'
            point.mkdir(mode=0o700)
            worker.run(['udevadm', 'settle'])
            worker.run(['mount', '-o', 'ro,noload,nodev,nosuid', '/dev/zvol/' + clone, str(point)])
            try:
                state.unpack_state(point / 'state.tar', Path(temporary) / 'checked')
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.dct-recovery-') as output:
                    with (point / 'state.tar').open('rb') as source:
                        shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                    os.link(output.name, destination)
            finally:
                worker.run(['umount', str(point)])
    finally:
        # A failed cleanup leaves the journal for explicit recovery.
        present = worker.run(['zfs', 'list', '-H', '-o', 'name', clone],
                             check=False, capture_output=True)
        if present.returncode == 0:
            worker.run(['zfs', 'destroy', clone])
        journal.unlink()


def main():
    if os.geteuid():
        raise SystemExit('Root required')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('ensure', 'prepare', 'publish', 'cleanup', 'export'))
    parser.add_argument('values', nargs='+')
    parser.add_argument('--lock-fd', type=int)
    args = parser.parse_args()
    if args.action == 'export':
        export(*args.values)
    else:
        result = globals()[args.action](*args.values, lock_fd=args.lock_fd)
        if result is not None:
            print(json.dumps(result))


if __name__ == '__main__':
    main()
