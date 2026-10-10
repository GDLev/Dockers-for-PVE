#!/usr/bin/env python3
"""Stopped DCT transfers using PVE SSH trust and a cluster-locked ownership handoff."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

import provision as worker
import guest_state as state

TRANSFERS = worker.ROOT.parent / 'transfers'
GLOBAL_METADATA = Path('/etc/pve/betterdocker')
HELPER = '/usr/local/lib/betterdocker/transfer.py'


def directory(token):
    worker.project_id(token)
    return TRANSFERS / token


def ssh_command(node):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', node):
        raise ValueError('Invalid destination node')
    program = ('use PVE::Cluster; use PVE::SSHInfo; use JSON; PVE::Cluster::cfs_update(); '
               'print encode_json(PVE::SSHInfo::ssh_info_to_command(PVE::SSHInfo::get_ssh_info($ARGV[0])));')
    command = json.loads(worker.capture(['perl', '-e', program, node]))
    return command[:-1] + ['-o', 'ConnectTimeout=10', command[-1]]


def remote(node, *args):
    return worker.capture(ssh_command(node) + [shlex.join([HELPER, *map(str, args)])])


def check_runtime(target):
    worker.node_runtime.assert_compatible(json.loads(remote(target, 'runtime')))


@contextlib.contextmanager
def private_metadata(folder, info):
    """An incoming stopped guest is not published before ownership is committed."""
    metadata = folder / 'metadata'
    metadata.mkdir(parents=True, exist_ok=True, mode=0o700)
    (metadata / 'ids').mkdir(exist_ok=True, mode=0o700)
    state.write(metadata / 'ids' / (str(info['vmid'])+'.json'),
                {'id': info['id'], 'node': worker.current_node(), 'vmid': info['vmid']})
    original = worker.METADATA
    worker.METADATA = metadata
    try:
        yield metadata
    finally:
        worker.METADATA = original


def receive(token):
    folder = directory(token)
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive = folder / 'state.tar'
    if archive.exists():
        raise ValueError('Incoming archive already exists')
    partial = folder / 'state.partial'
    with partial.open('xb') as output:
        os.chmod(partial, 0o600)
        shutil.copyfileobj(sys.stdin.buffer, output, 1024*1024)
        output.flush()
        os.fsync(output.fileno())
    partial.replace(archive)


def prepare(token, identifier, source, storage):
    folder = directory(token)
    current = state.read(GLOBAL_METADATA / (worker.project_id(identifier)+'.json'))
    worker.node_runtime.assert_compatible({'mode': current.get('runtime_mode', 'rootful'), 'count': 65536})
    if current['node'] != source or current.get('migration') != token:
        raise ValueError('DCT migration ownership changed')
    if current.get('ipv4mode', 'static') != 'static':
        raise ValueError('Stopped DCT transfer currently requires a static IPv4 endpoint')
    state.unpack_state(folder / 'state.tar', folder / 'state')
    volume = current.get('replication')
    if volume:
        import replica
        selected = volume.get('targets', {}).get(worker.current_node())
        if not selected:
            raise ValueError('Replicated DCT migration requires a successful replica on the destination')
        replica.recovery_source(volume['volid'], selected['snapshot'], selected['guid'])
    with private_metadata(folder, current):
        # A fenced former HA owner can retain its stopped local copy. Reclaim
        # only that enrolled copy after validating the incoming ownership above.
        project = worker.ROOT / identifier
        if current.get('ha_enrolled') and (project / 'ha.json').exists():
            local = dict(current, node=worker.current_node())
            path = worker.METADATA / (identifier + '.json')
            state.write(path, local)
            clean_local(identifier, local)
            path.unlink()
        state.restore_state(folder / 'state', identifier, worker.current_node(),
                            {key: current[key] for key in ('name', 'bridge', 'address', 'cidr', 'hwaddr', 'mtu') if key in current}, storage, live_firewall=current.get('firewall', 0))
        info = state.read(worker.METADATA / (identifier+'.json'))
        info['template'] = bool(current.get('template'))
        info['onboot'] = current.get('onboot', 0)
        if volume:
            volume = dict(volume, node=worker.current_node(), targets=dict(volume['targets']))
            selected = volume['targets'].pop(worker.current_node())
            volume['targets'][source] = selected
            info['replication'] = volume
            state.write(worker.ROOT / identifier / 'replication.json', volume)
        if current.get('ha_enrolled'):
            state.write(worker.ROOT / identifier / 'ha.json', {'managed': True})
        worker.write_json(worker.METADATA / (identifier+'.json'), info)
        state.write(folder / 'ready.json', {'id': identifier, 'source': source, 'info': info})
    print(json.dumps(info))


def clean_local(identifier, info):
    containers = worker.container_records(identifier)
    inspected = worker.snapshot_containers(identifier) if containers else []
    if containers:
        state.owned_mounts(identifier, inspected)
    anonymous = {mount['Name'] for container in inspected for mount in container.get('Mounts', [])
                 if mount['Type'] == 'volume'}
    if any(c['state'] in ('running', 'restarting', 'paused') for c in containers):
        raise ValueError('Cannot clean an active transfer copy')
    cleanup = worker.ROOT / identifier / 'transfer-cleanup.json'
    if cleanup.exists():
        previous = state.read(cleanup).get('volumes', [])
        if any(not (name.startswith('bd-' + identifier + '_') or re.fullmatch(r'[0-9a-f]{64}', name)) for name in previous):
            raise ValueError('Invalid transfer volume ownership')
        anonymous.update(previous)
    if cleanup.parent.exists():
        state.write(cleanup, {'volumes': sorted(anonymous)})
    if containers:
        worker.run(['docker', 'rm', *[c['id'] for c in containers]], capture_output=True)
    networks = worker.capture(['docker', 'network', 'ls', '-q', '--filter',
                               'label=com.docker.compose.project=bd-'+identifier]).split()
    if networks:
        worker.run(['docker', 'network', 'rm', *networks], capture_output=True)
    volumes = worker.capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-'+identifier]).split()
    existing = set(worker.capture(['docker', 'volume', 'ls', '-q']).split())
    volumes = sorted((set(volumes) | anonymous) & existing)
    if volumes:
        worker.run(['docker', 'volume', 'rm', *volumes], capture_output=True)
    worker.remove_network(info)
    disk = worker.storage_config(identifier)
    if disk:
        worker.storage_unmount(identifier)
        worker.pve_storage(identifier, 'free', volid=disk['volid'])
        for volid in disk.get('unused', []):
            worker.pve_storage(identifier, 'free', volid=volid)
    unit = worker.capture(['systemd-escape','--path','--suffix=mount',str(worker.ROOT / identifier / 'storage-data')]).strip()
    worker.run(['systemctl','disable',unit],check=False,capture_output=True)
    (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
    worker.run(['systemctl','daemon-reload'],capture_output=True)
    shutil.rmtree(worker.ROOT / identifier, ignore_errors=True)
    (worker.ROOT.parent / 'restart-paused' / (identifier+'.json')).unlink(missing_ok=True)


def abort(token):
    folder = directory(token)
    if not folder.exists():
        return
    ready = folder / 'ready.json'
    if ready.exists():
        value = state.read(ready)
        identifier, info = value['id'], value['info']
        published = state.read(GLOBAL_METADATA / (identifier+'.json'))
        if published['node'] == worker.current_node():
            raise ValueError('Cannot abort an already committed destination guest')
        with private_metadata(folder, info):
            with worker.operation(identifier, 'migration-abort'), worker.locked():
                clean_local(identifier, info)
    # Interrupted imports retain their ownership journal until rollback succeeds.
    elif (folder / 'metadata').exists():
        original = worker.METADATA
        worker.METADATA = folder / 'metadata'
        try:
            manifest = state.read(folder / 'state/manifest.json')
            identifier = worker.project_id(manifest['id'])
            published = state.read(GLOBAL_METADATA / (identifier+'.json'))
            if published['node'] == worker.current_node():
                raise ValueError('Cannot abort an already committed destination guest')
            with worker.operation(identifier, 'migration-abort'), worker.locked():
                if (worker.ROOT.parent / 'imports' / (identifier+'.json')).exists():
                    state.cleanup_import(identifier)
                elif (worker.METADATA / (identifier+'.json')).exists():
                    clean_local(identifier, state.read(worker.METADATA / (identifier+'.json')))
        finally:
            worker.METADATA = original
    shutil.rmtree(folder)


def commit(identifier, node, target, token, info):
    program = r'''use strict; use warnings; use JSON qw(decode_json encode_json); use PVE::Cluster; use PVE::Tools;
my ($id,$source,$target,$token) = @ARGV; local $/; my $next=decode_json(<STDIN>);
die "Invalid transfer identity\n" unless $id =~ /\A[0-9a-f]{32}\z/ && $token =~ /\A[0-9a-f]{32}\z/;
PVE::Cluster::cfs_lock_file('datacenter.cfg', 10, sub {
    my $path="/etc/pve/betterdocker/$id.json";
    my $current=decode_json(PVE::Tools::file_get_contents($path));
    die "Migration ownership changed\n" unless $current->{node} eq $source && $current->{migration} eq $token;
    die "Invalid destination metadata\n" unless $next->{id} eq $id && $next->{node} eq $target && $next->{vmid} == $current->{vmid};
    my $reservation="/etc/pve/betterdocker/ids/$current->{vmid}.json";
    my $owner=decode_json(PVE::Tools::file_get_contents($reservation));
    die "Guest reservation changed\n" unless $owner->{id} eq $id && $owner->{node} eq $source;
    $owner->{node}=$target;
    PVE::Tools::file_set_contents($reservation, encode_json($owner));
    eval { PVE::Tools::file_set_contents($path, encode_json($next)); };
    if (my $error=$@) {
        $owner->{node}=$source;
        PVE::Tools::file_set_contents($reservation, encode_json($owner));
        die $error;
    }
}); die $@ if $@;'''
    worker.run(['perl','-e',program,identifier,node,target,token],input=json.dumps(info))


def recover(token):
    folder = directory(token)
    info = state.read(folder / 'source.json')
    identifier = worker.project_id(info['id'])
    if worker.operation_state(identifier).get('active'):
        return
    with worker.operation(identifier, 'migration-recovery'):
        current = state.read(GLOBAL_METADATA / (identifier+'.json'))
        target = state.read(folder / 'target.json')['node']
        if current['node'] == target:
            if info.get('replication'):
                import ha
                ha.switch_jobs(info['vmid'], target, info['node'])
            with private_metadata(folder, info):
                worker.write_json(worker.METADATA / (identifier+'.json'), info)
                clean_local(identifier, info)
            remote(target, 'finish', token)
            shutil.rmtree(folder)
            return
        if current['node'] != info['node'] or current.get('migration') != token:
            raise ValueError('Migration ownership changed; recovery was not performed')
        remote(target, 'abort', token)
        # A killed commit can leave the reservation changed before metadata changed.
        program = r"""use PVE::Cluster; use PVE::Tools; use JSON qw(decode_json encode_json);
my($id,$node,$token)=@ARGV; PVE::Cluster::cfs_lock_file('datacenter.cfg',10,sub {
my $path="/etc/pve/betterdocker/$id.json"; my $info=decode_json(PVE::Tools::file_get_contents($path));
die "Migration ownership changed\n" unless $info->{node} eq $node && $info->{migration} eq $token;
my $reservation="/etc/pve/betterdocker/ids/$info->{vmid}.json";
my $owner=decode_json(PVE::Tools::file_get_contents($reservation)); die "Reservation changed\n" unless $owner->{id} eq $id;
$owner->{node}=$node; PVE::Tools::file_set_contents($reservation,encode_json($owner));
}); die $@ if $@;"""
        worker.run(['perl','-e',program,identifier,info['node'],token])
        worker.ensure_address(info)
        info.pop('migration', None)
        worker.write_json(GLOBAL_METADATA / (identifier+'.json'), info, allow_migration_change=True)
        marker = worker.ROOT.parent / 'restart-paused' / (identifier+'.json')
        marker.parent.mkdir(exist_ok=True,parents=True,mode=0o700)
        state.write(marker, {'paused': True})
        shutil.rmtree(folder)


def recover_all():
    for source in TRANSFERS.glob('*/source.json'):
        try:
            info = state.read(source)
            if info['node'] == worker.current_node() and not worker.operation_state(info['id']).get('active'):
                recover(source.parent.name)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f'DCT migration recovery pending: {error}', file=sys.stderr)


def migrate(identifier, node, target, storage, digest=None):
    if target == node:
        raise ValueError('Choose another cluster node')
    with worker.operation(identifier, 'migrate') as lock:
        with worker.locked():
            info = worker.detail(identifier, node)
            worker.assert_configuration_digest(identifier, node, digest)
            if worker.load_pending_network(worker.ROOT / identifier):
                raise ValueError('Apply or revert pending network changes before migrating the DCT')
            check_runtime(target)
            worker.recover_guest_hook(info)
            worker.require_stopped(worker.container_records(identifier))
            if info.get('replication') and target not in info['replication'].get('targets', {}):
                raise ValueError('Replicated DCT migration requires a successful replica on the destination')
            if info.get('ipv4mode', 'static') != 'static':
                raise ValueError('Stopped DCT transfer currently requires a static IPv4 endpoint')
            snapshots = worker.snapshot_root(identifier)
            if snapshots.exists() and any(snapshots.iterdir()):
                raise ValueError('DCT snapshot history transfer is not implemented yet')
            token = uuid.uuid4().hex
            folder = directory(token); folder.mkdir(parents=True, mode=0o700)
            info.pop('deployment', None); info.pop('digest', None)
            if (worker.ROOT / identifier / 'ha.json').exists():
                info['ha_enrolled'] = True
            info['migration'] = token
            worker.write_json(worker.METADATA / (identifier+'.json'), info)
            state.write(folder / 'source.json', info)
            state.write(folder / 'target.json', {'node': target})
            records = worker.container_records(identifier)
            if records:
                worker.run(['docker','update','--restart=no',*[c['id'] for c in records]],capture_output=True)
        committed = False
        try:
            state.capture_state(identifier, node, folder / 'state', lock_fd=lock.fileno())
            state.pack_state(folder / 'state', folder / 'state.tar')
            with worker.locked():
                worker.remove_network(info)
            with (folder / 'state.tar').open('rb') as archive:
                subprocess.run(ssh_command(target) + [shlex.join([HELPER, 'receive', token])],stdin=archive,check=True,timeout=86400)
            destination = json.loads(remote(target, 'prepare', token, identifier, node, storage))
            commit(identifier,node,target,token,destination)
            committed = True
            if info.get('replication'):
                import ha
                ha.switch_jobs(info['vmid'], target, node)
            with private_metadata(folder, info):
                worker.write_json(worker.METADATA / (identifier+'.json'), info)
                with worker.locked():
                    clean_local(identifier, info)
            remote(target, 'finish', token)
            shutil.rmtree(folder)
        except BaseException:
            published = state.read(GLOBAL_METADATA / (identifier+'.json'))
            committed = committed or published['node'] == target
            if not committed:
                remote(target, 'abort', token)
                with worker.locked():
                    worker.ensure_address(info)
                    info.pop('migration', None)
                    worker.write_json(GLOBAL_METADATA / (identifier+'.json'), info, allow_migration_change=True)
                    marker = worker.ROOT.parent / 'restart-paused' / (identifier+'.json')
                    marker.parent.mkdir(exist_ok=True,parents=True,mode=0o700)
                    state.write(marker, {'paused': True})
                shutil.rmtree(folder)
            raise


def main():
    if os.geteuid():
        raise SystemExit('Root required')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('receive','prepare','abort','finish','migrate','recover','runtime','check-runtime'))
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    if args.action == 'runtime': print(json.dumps(worker.node_runtime.load().identity()))
    elif args.action == 'check-runtime': check_runtime(*args.values)
    elif args.action == 'receive': receive(*args.values)
    elif args.action == 'prepare': prepare(*args.values)
    elif args.action == 'abort': abort(*args.values)
    elif args.action == 'recover': recover(*args.values)
    elif args.action == 'finish':
        folder=directory(*args.values)
        if not folder.exists():
            return
        ready=state.read(folder / 'ready.json')
        if state.read(GLOBAL_METADATA / (ready['id']+'.json'))['node'] != worker.current_node():
            raise ValueError('Destination ownership is not committed')
        shutil.rmtree(folder)
    else: migrate(*args.values)


if __name__ == '__main__': main()
