#!/usr/bin/env python3
"""Check retained snapshot, disk and anonymous-volume cleanup on a test node."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from live_service_security import api, call, w
from dctlib.data import retained_data


def main():
    before = {int(row['vmid']) for row in api('get', '/cluster/resources', type='vm')}
    vmid = next(value for value in range(940, 980) if value not in before)
    identifier = None
    with tempfile.TemporaryDirectory(prefix='bd-retention-test-') as folder:
        source = Path(folder) / 'compose.json'
        source.write_text(json.dumps({'services': {'app': {'image': 'busybox:1.37', 'init': True, 'command': ['sleep', '86400'], 'volumes': ['./data:/data', '/anonymous']}}}))
        try:
            call('dct', 'create', vmid, source, '--hostname', 'retained-cleanup-test', '--net0', 'bridge=vmbr0,ip=none,ip6=none')
            row = next(row for row in api('get', '/cluster/resources', type='vm') if int(row['vmid']) == vmid)
            identifier, node = row['id'].split('/')[-1], row['node']
            # First retain the automatically allocated anonymous volume after deletion.
            call('dct', 'start', vmid)
            cid = w.container_records(identifier)[0]['id']
            volume = next(m['Name'] for m in json.loads(call('docker', 'inspect', cid))[0]['Mounts'] if m['Destination'] == '/anonymous')
            call('dct', 'stop', vmid)
            cid = w.container_records(identifier)[0]['id']
            info = w.detail(identifier, node)
            inspected = json.loads(call('docker', 'inspect', cid))
            from dctlib.policy import validate_delegated_containers
            validate_delegated_containers(inspected, identifier)
            config = json.loads(source.read_text())
            config['services']['app']['environment'] = {'DELEGATED': 'accepted'}
            with patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '0'}), w.operation(identifier, 'update'):
                w.update(identifier, node, w.validate(dict(info['deployment'], compose=json.dumps(config))), info['digest'])
            current = w.container_records(identifier)[0]['id']
            assert next(m['Name'] for m in json.loads(call('docker', 'inspect', current))[0]['Mounts'] if m['Destination'] == '/anonymous') == volume
            print('PASS delegated anonymous-volume inspection and preserving deployment edit', flush=True)
            config['services']['app']['volumes'] = ['./data:/data']
            info = w.detail(identifier, node)
            with w.operation(identifier, 'update'):
                w.update(identifier, node, w.validate(dict(info['deployment'], compose=json.dumps(config))), info['digest'])
            api('set', '/nodes/' + node + '/docker/' + identifier + '/storage-limit', storage='local-lvm', size_mib=256, digest=w.detail(identifier, node)['digest'])
            call('dct', 'snapshot', vmid, 'retained')
            call('dct', 'destroy', vmid)
            plan = retained_data(identifier)
            assert plan['dry_run'] and plan['snapshots'] == ['retained']
            assert plan['disks']
            assert (w.snapshot_root(identifier) / 'retained').exists()
            call('docker', 'volume', 'inspect', volume)
            retained_data(identifier, destroy=True)
            assert not (w.ROOT / identifier).exists()
            assert not w.snapshot_root(identifier).exists()
            call('docker', 'volume', 'inspect', volume, fail=True)
            after = {int(row['vmid']) for row in api('get', '/cluster/resources', type='vm')}
            assert before <= after and vmid not in after
            print('PASS retained snapshot dry run, owned volume deletion and guest reservation cleanup', flush=True)
        finally:
            if identifier and any(row['id'] == identifier for row in w.summaries()):
                call('dct', 'stop', vmid)
                for snapshot in w.snapshot_list(identifier, node):
                    if snapshot['name'] != 'current':
                        call('dct', 'delsnapshot', vmid, snapshot['name'])
                call('dct', 'destroy', vmid, '--destroy-owned-data', 1)


if __name__ == '__main__':
    main()
