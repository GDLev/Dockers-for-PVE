#!/usr/bin/env python3
"""DCT HA actions. Ownership recovery is requested only by the native fenced CRM path."""
import argparse
import json
import os
import shutil
import uuid
from pathlib import Path
import tempfile

import provision as worker
import guest_state as state
import replica
import transfer


def info(identifier):
    return state.read(worker.METADATA / (worker.project_id(identifier) + '.json'))


def pause_restart(identifier):
    # Persist before changing any Docker policy so interrupted enrollment stays safe.
    state.write(worker.ROOT / identifier / 'ha.json', {'managed': True})
    marker = worker.ROOT.parent / 'restart-paused' / (identifier + '.json')
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.write(marker, {'paused': True})
    records = worker.container_records(identifier)
    if records:
        worker.run(['docker', 'update', '--restart=no', *[item['id'] for item in records]], capture_output=True)


def enroll(identifier, node):
    with worker.operation(identifier, 'ha-enroll'), worker.locked():
        current = worker.detail(identifier, node)
        if current.get('template') or current.get('migration'):
            raise ValueError('HA requires an ordinary, stationary DCT')
        if current.get('ipv4mode', 'static') != 'static':
            raise ValueError('DCT HA currently requires a static IPv4 endpoint')
        snapshots = worker.snapshot_root(identifier)
        if snapshots.exists() and any(snapshots.iterdir()):
            raise ValueError('DCT HA recovery does not include snapshot history')
        if not current.get('replication', {}).get('targets'):
            raise ValueError('Complete a DCT replication job before enabling HA')
        pause_restart(identifier)


def release(identifier, node):
    with worker.operation(identifier, 'ha-release', wait_timeout=300), worker.locked():
        current = info(identifier)
        if current['node'] != node or node != worker.current_node():
            raise ValueError('HA guest ownership changed')
        if not (worker.ROOT / identifier).exists():
            return
        worker.detail(identifier, node)
        (worker.ROOT / identifier / 'ha.json').unlink(missing_ok=True)
        (worker.ROOT / identifier / '.ha-compose.json').unlink(missing_ok=True)
        marker = worker.ROOT.parent / 'restart-paused' / (identifier + '.json')
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.write(marker, {'paused': True})
        worker.restore_restart_policy(identifier)


def checkpoint(current, target):
    volume = current.get('replication', {})
    selected = volume.get('targets', {}).get(target)
    if not selected:
        raise ValueError('No successful complete DCT replica on the recovery node')
    replica.recovery_source(volume['volid'], selected['snapshot'], selected['guid'])
    return selected


def check_recovery(identifier, target):
    if target != worker.current_node():
        raise ValueError('Recovery checkpoint check was sent to the wrong node')
    checkpoint(info(identifier), target)


def recover_guest(identifier, node):
    current = info(identifier)
    recovery = current.get('ha_recovery')
    if not recovery:
        return
    if current['node'] != node or node != worker.current_node():
        raise ValueError('HA recovery ownership changed')
    with worker.operation(identifier, 'ha-recovery', wait_timeout=300) as lock:
        selected = checkpoint(current, node)
        journal = worker.ROOT.parent / 'ha-recoveries' / (identifier + '.json')
        journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        previous = state.read(journal) if journal.exists() else {}
        with tempfile.TemporaryDirectory(prefix='ha-recovery-', dir=worker.ROOT.parent) as temporary:
            folder = Path(temporary)
            archive = folder / 'state.tar'
            replica.export(current['replication']['volid'], archive, selected['snapshot'], selected['guid'])
            state.unpack_state(archive, folder / 'state')
            # A fenced former owner can retain a stopped copy from an older generation.
            if (worker.ROOT / identifier).exists():
                if not (worker.ROOT / identifier / 'ha.json').exists() and previous.get('token') != recovery['token']:
                    raise ValueError('HA recovery found an unowned local deployment')
                with worker.locked():
                    transfer.clean_local(identifier, current)
            state.write(journal, {'token': recovery['token']})
            # The storage bridge accepts private metadata only in the transfer root.
            private = worker.ROOT.parent / 'transfers' / uuid.uuid4().hex
            private.mkdir(mode=0o700, parents=True)
            try:
                with transfer.private_metadata(private, current):
                    restored = state.restore_state(folder / 'state', identifier, node,
                        {key: current[key] for key in ('name', 'bridge', 'address', 'cidr', 'hwaddr', 'mtu') if key in current},
                        current['replication']['storage'], lock_fd=lock.fileno(), live_firewall=current.get('firewall', 0))
            finally:
                shutil.rmtree(private)
            volume = dict(current['replication'], node=node)
            volume['targets'] = {target: value for target, value in volume['targets'].items() if target != node}
            volume['targets'][recovery['source']] = selected
            restored.update(replication=volume)
            # Enrollment marker precedes publication and any possible Start.
            pause_restart(identifier)
            state.write(worker.ROOT / identifier / 'replication.json', volume)
            switch_jobs(current['vmid'], node, recovery['source'])
            publish(identifier, recovery['token'], restored)
            journal.unlink()


def switch_jobs(vmid, target, source):
    worker.run(['perl', '-MPVE::ReplicationConfig', '-e',
                'PVE::ReplicationConfig::switch_replication_job_target(@ARGV)', str(vmid), target, source])


def publish(identifier, token, value):
    program = r'''use PVE::Cluster; use PVE::Tools; use JSON;
my($id,$token)=@ARGV; local $/; my $next=decode_json(<STDIN>);
PVE::Cluster::cfs_lock_file('datacenter.cfg',10,sub {
my $path="/etc/pve/betterdocker/$id.json"; my $old=decode_json(PVE::Tools::file_get_contents($path));
die "HA recovery ownership changed\n" unless $old->{ha_recovery}->{token} eq $token && $old->{node} eq $next->{node} && $old->{vmid} == $next->{vmid};
my $owner=decode_json(PVE::Tools::file_get_contents("/etc/pve/betterdocker/ids/$old->{vmid}.json"));
die "HA reservation changed\n" unless $owner->{id} eq $id && $owner->{node} eq $next->{node} && $next->{id} eq $id;
PVE::Tools::file_set_contents($path,encode_json($next));
}); die $@ if $@;'''
    worker.run(['perl', '-e', program, identifier, token], input=json.dumps(value))


def running(identifier, node):
    worker.detail(identifier, node)
    print(json.dumps(any(item['state'] in ('running', 'restarting', 'paused')
                         for item in worker.container_records(identifier))))


def power(identifier, node, action, timeout='60'):
    if action == 'start':
        recover_guest(identifier, node)
    with worker.operation(identifier, 'ha-' + action, wait_timeout=300):
        worker.action(identifier, node, action, int(timeout))


def main():
    if os.geteuid():
        raise SystemExit('Root required')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('enroll', 'release', 'check_recovery', 'power', 'running', 'recover_guest'))
    parser.add_argument('values', nargs='+')
    args = parser.parse_args()
    globals()[args.action](*args.values)


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error))
