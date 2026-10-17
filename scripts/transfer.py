#!/usr/bin/env python3
"""Stopped DCT transfers using PVE SSH trust and a cluster-locked ownership handoff."""
# Check the installed code tree before importing non-frozen modules.
import os as _boot_os
import sys as _boot_sys
if __file__.startswith('/usr/local/lib/betterdocker/'):
    _boot_root = '/usr/local/lib/betterdocker'
    for _boot_dir, _boot_dirs, _boot_files in _boot_os.walk(_boot_root, followlinks=False):
        for _boot_name in [_boot_dir, *(_boot_os.path.join(_boot_dir, n) for n in _boot_dirs + _boot_files)]:
            try:
                _boot_info = _boot_os.stat(_boot_name, follow_symlinks=False)
            except FileNotFoundError:
                # CPython replaces these temporary files with canonical bytecode.
                if _boot_os.path.basename(_boot_dir) == '__pycache__' and '.pyc.' in _boot_name and _boot_name.rsplit('.pyc.', 1)[1].isdigit():
                    continue
                raise
            if _boot_info.st_uid != 0 or _boot_info.st_mode & 0o022 or _boot_os.path.islink(_boot_name):
                _boot_sys.stderr.write('BetterDocker: unsafe installed helper ownership or permissions\n')
                raise SystemExit(1)
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
import selectors
from dctlib import space

from dctlib import projects as worker
import guest_state as state
import migration_history as history

TRANSFERS = worker.ROOT.parent / 'transfers'
GLOBAL_METADATA = Path('/etc/pve/betterdocker')
HELPER = '/usr/local/lib/betterdocker/transfer.py'


from dctlib.operation_capacity import bounded
from dctlib.retention import temporary, transfer_paths


def directory(token):
    worker.project_id(token)
    return TRANSFERS / token


def ssh_command(node):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', node):
        raise ValueError('Invalid destination node')
    program = ('use PVE::Cluster; use PVE::DataCenterConfig; use PVE::SSHInfo; use JSON; PVE::Cluster::cfs_update(); '
               'my $dc=PVE::Cluster::cfs_read_file("datacenter.cfg"); '
               'print encode_json(PVE::SSHInfo::ssh_info_to_command(PVE::SSHInfo::get_ssh_info($ARGV[0],$dc->{migration}->{network})));')
    command = json.loads(worker.capture(['perl', '-e', program, node]))
    return command[:-1] + ['-o', 'ConnectTimeout=10', command[-1]]


def remote(node, *args):
    try:
        return worker.run(ssh_command(node) + [shlex.join([HELPER, *map(str, args)])], capture_output=True, timeout=86400 if args and args[0] in ('prepare', 'abort', 'resume') else 120).stdout
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or 'No error details from destination').strip().splitlines()[-1]
        raise ValueError(f'Destination {node}: {detail}') from error


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


@bounded
@space.loaned_transfer
@temporary(transfer_paths)
def receive(token):
    folder = directory(token)
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive = folder / 'state.tar'
    if archive.exists():
        raise ValueError('Incoming archive already exists')
    partial = folder / 'state.partial'
    with partial.open('xb') as output:
        os.chmod(partial, 0o600)
        from dctlib.retention import own_partial
        own_partial(partial, output.fileno())
        from dctlib.retention import copy_stream, remaining_transfer
        copy_stream(sys.stdin.buffer, output, remaining_transfer())
        output.flush()
        os.fsync(output.fileno())
    partial.replace(archive)


@bounded
@space.loaned_transfer
@temporary(transfer_paths)
def prepare(token, identifier, source, storage):
    folder = directory(token)
    current = state.read(GLOBAL_METADATA / (worker.project_id(identifier)+'.json'))
    worker.node_runtime.assert_compatible({'mode': current.get('runtime_mode', 'rootful'), 'count': 65536})
    if current['node'] != source or current.get('migration') != token:
        raise ValueError('DCT migration ownership changed')
    state.unpack_state(folder / 'state.tar', folder / 'state', history=True)
    volume = current.get('replication')
    if volume:
        import replica
        selected = volume.get('targets', {}).get(worker.current_node())
        if not selected:
            raise ValueError('Replicated DCT migration requires a successful replica on the destination')
        replica.recovery_source(volume['volid'], selected['snapshot'], selected['guid'])
    with worker.operation(identifier, 'migration-prepare') as lock, private_metadata(folder, current):
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
                            {key: current[key] for key in ('name', 'bridge', 'address', 'cidr', 'ipv4mode', 'hwaddr', 'mtu', 'interfaces') if key in current}, storage, lock_fd=lock.fileno(), live_firewall=current.get('firewall', 0))
        provisional = state.read(worker.METADATA / (identifier + '.json'))
        state.write(folder / 'ready.json', {'id': identifier, 'source': source, 'info': provisional})
        history.restore(identifier, worker.current_node(), folder / 'state')
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
    from dctlib.recovery import require_docker
    require_docker()
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
    history.remove(identifier)
    disk = worker.storage_config(identifier)
    if disk:
        worker.storage_unmount(identifier)
        from dctlib.disks import volume_ids
        for volid in volume_ids(disk):
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
    identifier = state.read(folder / 'state/manifest.json')['id'] if (folder / 'state/manifest.json').exists() else None
    if identifier:
        with worker.operation(identifier, 'migration-abort', wait_timeout=1800):
            return abort_copy(token)
    return abort_copy(token)


def abort_copy(token):
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
            with worker.locked(identifier):
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
            with worker.locked(identifier):
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
    my $current=decode_json(PVE::Cluster::get_config("betterdocker/$id.json"));
    die "Migration ownership changed\n" unless $current->{node} eq $source && $current->{migration} eq $token;
    die "Invalid destination metadata\n" unless $next->{id} eq $id && $next->{node} eq $target && $next->{vmid} == $current->{vmid};
    my $reservation="/etc/pve/betterdocker/ids/$current->{vmid}.json";
    my $owner=decode_json(PVE::Cluster::get_config("betterdocker/ids/$current->{vmid}.json"));
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
        from dctlib.recovery import require_docker
        require_docker()
        if current['node'] == target:
            if info.get('replication'):
                import ha
                ha.switch_jobs(info['vmid'], target, info['node'])
            with private_metadata(folder, info):
                worker.write_json(worker.METADATA / (identifier+'.json'), info)
                clean_local(identifier, info)
            history.cleanup_exports(identifier, folder)
            remote(target, 'finish', token)
            resume = state.read(folder / 'target.json').get('resume', [])
            if resume:
                remote(target, 'resume', identifier, json.dumps(resume))
            shutil.rmtree(folder)
            return
        if current['node'] != info['node'] or current.get('migration') != token:
            raise ValueError('Migration ownership changed; recovery was not performed')
        history.cleanup_exports(identifier, folder)
        remote(target, 'abort', token)
        # A killed commit can leave the reservation changed before metadata changed.
        program = r"""use PVE::Cluster; use PVE::Tools; use JSON qw(decode_json encode_json);
my($id,$node,$token)=@ARGV; PVE::Cluster::cfs_lock_file('datacenter.cfg',10,sub {
my $path="/etc/pve/betterdocker/$id.json"; my $info=decode_json(PVE::Cluster::get_config("betterdocker/$id.json"));
die "Migration ownership changed\n" unless $info->{node} eq $node && $info->{migration} eq $token;
my $reservation="/etc/pve/betterdocker/ids/$info->{vmid}.json";
my $owner=decode_json(PVE::Cluster::get_config("betterdocker/ids/$info->{vmid}.json")); die "Reservation changed\n" unless $owner->{id} eq $id;
$owner->{node}=$node; PVE::Tools::file_set_contents($reservation,encode_json($owner));
}); die $@ if $@;"""
        worker.run(['perl','-e',program,identifier,info['node'],token])
        worker.ensure_address(info)
        info.pop('migration', None)
        worker.write_json(GLOBAL_METADATA / (identifier+'.json'), info, allow_migration_change=True)
        marker = worker.ROOT.parent / 'restart-paused' / (identifier+'.json')
        marker.parent.mkdir(exist_ok=True,parents=True,mode=0o700)
        state.write(marker, {'paused': True})
        resume = state.read(folder / 'target.json').get('resume', [])
        if resume:
            resume_instances(identifier, resume)
        shutil.rmtree(folder)


def recover_all():
    for source in TRANSFERS.glob('*/source.json'):
        try:
            info = state.read(source)
            if info['node'] == worker.current_node() and not worker.operation_state(info['id']).get('active'):
                recover(source.parent.name)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f'DCT migration recovery pending: {error}', file=sys.stderr)


def hold_space(token, spec, storage=''):
    worker.project_id(token)
    spec = json.loads(spec)
    with space.reserve(space.destination_targets(token, spec, storage), key=token):
        print('READY', flush=True)
        for line in sys.stdin:
            if not line.strip(): break
            space.claim(space.destination_targets(token, json.loads(line), storage))
            print('READY', flush=True)


class DestinationCapacity:
    def __init__(self, child): self.child = child

    def ready(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.child.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                raise ValueError('Destination peak-space check timed out')
            if self.child.stdout.readline().strip() != 'READY':
                raise ValueError('Destination peak-space check failed; source was not transferred')

    def update(self, spec):
        self.child.stdin.write(json.dumps(spec) + '\n'); self.child.stdin.flush()
        self.ready()


@contextlib.contextmanager
def destination_capacity(target, token, spec, storage):
    command = ssh_command(target) + [shlex.join([HELPER, 'space-hold', token, json.dumps(spec), storage])]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lease = DestinationCapacity(child)
    try:
        lease.ready()
        yield lease
    finally:
        if child.poll() is None:
            try:
                child.stdin.write('\n'); child.stdin.flush(); child.stdin.close()
                child.wait(timeout=10)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                child.terminate(); child.wait(timeout=10)


@bounded
def migrate(identifier, node, target, storage, digest=None, restart=0, timeout=180):
    restart, timeout = int(restart), int(timeout)
    if restart not in (0, 1) or not 1 <= timeout <= 86400:
        raise ValueError('Invalid migration restart or timeout')
    if target == node:
        raise ValueError('Choose another cluster node')
    with worker.operation(identifier, 'migrate') as lock, contextlib.ExitStack() as peak:
        with worker.locked(identifier):
            info = worker.detail(identifier, node)
            worker.assert_configuration_digest(identifier, node, digest)
            if worker.load_pending_network(worker.ROOT / identifier):
                raise ValueError('Apply or revert pending network changes before migrating the DCT')
            check_runtime(target)
            worker.recover_guest_hook(info)
            active = worker.active_network_instances(identifier)
            if active and not restart:
                raise ValueError('Running DCT migration requires restart=1')
            if info.get('replication') and target not in info['replication'].get('targets', {}):
                raise ValueError('Replicated DCT migration requires a successful replica on the destination')
            worker.project_limits.preflight(identifier)
            token = uuid.uuid4().hex
            spec = space.export_size(identifier, history=True)
            space.claim(space.source_targets(identifier, directory(token), history=True), identifier)
            destination_space = peak.enter_context(destination_capacity(target, token, spec, storage))
            folder = directory(token); folder.mkdir(parents=True, mode=0o700)
            info.pop('deployment', None); info.pop('digest', None)
            if (worker.ROOT / identifier / 'ha.json').exists():
                info['ha_enrolled'] = True
            info['migration'] = token
            worker.write_json(worker.METADATA / (identifier+'.json'), info)
            state.write(folder / 'source.json', info)
            state.write(folder / 'target.json', {'node': target, 'resume': active, 'timeout': timeout})
            records = worker.container_records(identifier)
            if records:
                worker.run(['docker','update','--restart=no',*[c['id'] for c in records]],capture_output=True)
        committed = False
        try:
            if active:
                worker.action(identifier, node, 'shutdown', timeout)
            state.capture_state(identifier, node, folder / 'state', lock_fd=lock.fileno())
            history.capture(identifier, folder / 'state')
            state.pack_state(folder / 'state', folder / 'state.tar')
            spec['state_bytes'] = max(spec['state_bytes'], space.retention.tree_bytes(folder / 'state'), (folder / 'state.tar').stat().st_size)
            spec['image_bytes'] = max(spec['image_bytes'], (folder / 'state/images.tar').stat().st_size)
            destination_space.update(spec)
            with worker.locked(identifier):
                worker.remove_network(info)
            with (folder / 'state.tar').open('rb') as archive:
                from dctlib.transfer_io import bandwidth, send
                source_storage = worker.storage_config(identifier).get('storage')
                send(ssh_command(target) + [shlex.join([HELPER, 'receive', token])], archive,
                     bandwidth('migration', [source_storage, storage]))
            destination = json.loads(remote(target, 'prepare', token, identifier, node, storage))
            commit(identifier,node,target,token,destination)
            committed = True
            if info.get('replication'):
                import ha
                ha.switch_jobs(info['vmid'], target, node)
            with private_metadata(folder, info):
                worker.write_json(worker.METADATA / (identifier+'.json'), info)
                with worker.locked(identifier):
                    clean_local(identifier, info)
            remote(target, 'finish', token)
            if active:
                remote(target, 'resume', identifier, json.dumps(active))
            shutil.rmtree(folder)
        except BaseException:
            published = state.read(GLOBAL_METADATA / (identifier+'.json'))
            committed = committed or published['node'] == target
            if not committed:
                history.cleanup_exports(identifier, folder)
                remote(target, 'abort', token)
                with worker.locked(identifier):
                    worker.ensure_address(info)
                    info.pop('migration', None)
                    worker.write_json(GLOBAL_METADATA / (identifier+'.json'), info, allow_migration_change=True)
                    marker = worker.ROOT.parent / 'restart-paused' / (identifier+'.json')
                    marker.parent.mkdir(exist_ok=True,parents=True,mode=0o700)
                    state.write(marker, {'paused': True})
                if active:
                    resume_instances(identifier, active)
                shutil.rmtree(folder)
            raise


def resume_instances(identifier, instances):
    info = worker.detail(identifier, worker.current_node())
    worker.project_limits.preflight(identifier)
    worker.prepare_network_start(info)
    worker.guest_hook(info, 'pre-start')
    worker.expect_guest_hook(info, 'post-start')
    worker.prepare_firewall(info)
    records = worker.container_records(identifier)
    for saved in instances:
        row = next((r for r in records if r['service'] == saved['service'] and r.get('number', '1') == saved['number']), None)
        if not row:
            raise ValueError('Migration cannot find a previously active instance')
        if row['state'] not in ('running', 'paused'):
            worker.run(['docker', 'start', row['id']], capture_output=True)
        if saved.get('paused') and row['state'] != 'paused':
            worker.run(['docker', 'pause', row['id']], capture_output=True)
    worker.restore_restart_policy(identifier)
    worker.finish_network_start(info)
    worker.guest_hook(info, 'post-start')
    worker.refresh(worker.current_node())


def main():
    if os.geteuid():
        raise SystemExit('Root required')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('space-hold','receive','prepare','abort','finish','migrate','recover','runtime','check-runtime','resume'))
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    if args.action == 'runtime': print(json.dumps(worker.node_runtime.load().identity()))
    elif args.action == 'space-hold': hold_space(*args.values)
    elif args.action == 'check-runtime': check_runtime(*args.values)
    elif args.action == 'receive': receive(*args.values)
    elif args.action == 'prepare': prepare(*args.values)
    elif args.action == 'abort': abort(*args.values)
    elif args.action == 'recover': recover(*args.values)
    elif args.action == 'resume':
        identifier = worker.project_id(args.values[0])
        with worker.operation(identifier, 'start'), worker.locked(identifier):
            resume_instances(identifier, json.loads(args.values[1]))
    elif args.action == 'finish':
        folder=directory(*args.values)
        if not folder.exists():
            return
        ready=state.read(folder / 'ready.json')
        if state.read(GLOBAL_METADATA / (ready['id']+'.json'))['node'] != worker.current_node():
            raise ValueError('Destination ownership is not committed')
        shutil.rmtree(folder)
    else: migrate(*args.values)


if __name__ == '__main__':
    from dctlib.authorization import require_host_root
    from dctlib.security_errors import public_error
    try:
        require_host_root(sys.argv[1:])
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('BetterDocker: ' + public_error(error), file=sys.stderr)
        raise SystemExit(1)
