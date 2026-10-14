#!/usr/bin/env python3
"""Verify deployment and disk transactions on a disposable test-node DCT.

Run after the existing preservation suite. Supply an unused IPv4/CIDR.
"""
import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from live_service_security import api, call, w
from dctlib import disks
import guest_state


def main():
    before = api('get', '/cluster/resources', type='vm')
    vmid = next(i for i in range(940, 980) if i not in {int(row['vmid']) for row in before})
    identifier = None
    children = []
    config = {'services': {'app': {'image': 'busybox:1.37', 'init': True, 'command': ['sleep', '86400'],
        'deploy': {'replicas': 2}, 'volumes': ['./data:/data', '/anonymous'], 'environment': {'VALUE': 'before'}}}}
    with tempfile.TemporaryDirectory(prefix='bd-release-test-') as folder:
        folder = Path(folder)
        source = folder / 'compose.json'; source.write_text(json.dumps(config))
        try:
            call('dct', 'create', vmid, source, '--hostname', 'release-transaction-test', '--net0', 'bridge=vmbr0,ip=' + sys.argv[1], '--start', 1)
            row = next(row for row in api('get', '/cluster/resources', type='vm') if int(row['vmid']) == vmid)
            identifier, node = row['id'].split('/')[-1], row['node']
            base = '/nodes/' + node + '/docker/' + identifier
            project = w.ROOT / identifier
            originals = sorted(w.container_records(identifier), key=lambda row: row['number'])
            volumes = {}
            for row in originals:
                call('docker', 'exec', row['id'], 'sh', '-c', 'echo "$1" > /marker; echo "$1" > /anonymous/value; echo data > /data/value', 'sh', row['number'])
                inspected = json.loads(call('docker', 'inspect', row['id']))[0]
                volumes[row['number']] = next(m['Name'] for m in inspected['Mounts'] if m['Destination'] == '/anonymous')
            call('dct', 'stop', vmid)
            updated = copy.deepcopy(config)
            updated['services']['app']['environment']['VALUE'] = 'after-$$literal'
            updated['services']['app']['deploy']['replicas'] = 3
            info = w.detail(identifier, node)
            deployment = dict(info['deployment'], compose=json.dumps(updated), environment='SECRET=after')
            with w.operation(identifier, 'update'):
                w.update(identifier, node, w.validate(deployment), info['digest'])
            call('dct', 'start', vmid)
            current = sorted(w.container_records(identifier), key=lambda row: row['number'])
            assert len(current) == 3
            for row in current[:2]:
                assert call('docker', 'exec', row['id'], 'cat', '/marker').strip() == row['number']
                assert call('docker', 'exec', row['id'], 'cat', '/anonymous/value').strip() == row['number']
                inspected = json.loads(call('docker', 'inspect', row['id']))[0]
                assert next(m['Name'] for m in inspected['Mounts'] if m['Destination'] == '/anonymous') == volumes[row['number']]
                assert 'VALUE=after-$literal' in inspected['Config']['Env']
            assert call('docker', 'exec', current[2]['id'], 'cat', '/marker', fail=True)
            assert (project / '.env').read_text() == 'SECRET=after'
            print('PASS: YAML/env edit, literal dollars, replica scale and anonymous volumes preserve existing files', flush=True)
            call('dct', 'stop', vmid)
            # Anonymous per-replica volumes retain their own lifecycle. The project
            # data disk manages named volumes and project bind directories.
            updated['services']['app']['volumes'] = ['./data:/data']
            info = w.detail(identifier, node)
            with w.operation(identifier, 'update'):
                w.update(identifier, node, w.validate(dict(info['deployment'], compose=json.dumps(updated))), info['digest'])
            api('set', base + '/storage-limit', storage='local-lvm', size_mib=256, digest=w.detail(identifier, node)['digest'])
            api('set', base + '/disks/mp1', service='app', target='/extra', storage='local-lvm', size_mib=256, backup=0, digest=w.detail(identifier, node)['digest'])
            api('set', base + '/disks/mp1', service='app', target='/extra', storage='local-lvm', size_mib=384, backup=0, digest=w.detail(identifier, node)['digest'])
            call('dct', 'start', vmid)
            current = sorted(w.container_records(identifier), key=lambda row: row['number'])
            call('docker', 'exec', current[0]['id'], 'sh', '-c', 'echo extra > /extra/value; echo kept > /data/value')
            for row in current[:2]:
                assert call('docker', 'exec', row['id'], 'cat', '/marker').strip() == row['number']
                assert call('docker', 'exec', row['id'], 'cat', '/extra/value').strip() == 'extra'
            call('dct', 'stop', vmid)
            settings = w.storage_config(identifier)
            assert settings['additional']['mp1']['size_mib'] >= 384
            export = folder / 'export'
            guest_state.capture_state(identifier, node, export, backup_mode=True)
            manifest = json.loads((export / 'manifest.json').read_text())
            assert not any(Path(mount['source']).is_relative_to(disks.source(identifier, 'mp1')) for mount in manifest['mounts'])
            assert any('/storage-data' in mount['source'] for mount in manifest['mounts'])
            call('dct', 'snapshot', vmid, 'disk-files')
            call('dct', 'start', vmid)
            cid = w.container_records(identifier)[0]['id']
            call('docker', 'exec', cid, 'sh', '-c', 'echo changed > /extra/value')
            call('dct', 'stop', vmid)
            call('dct', 'rollback', vmid, 'disk-files')
            call('dct', 'start', vmid)
            assert call('docker', 'exec', w.container_records(identifier)[0]['id'], 'cat', '/extra/value').strip() == 'extra'
            call('dct', 'stop', vmid)
            call('dct', 'delsnapshot', vmid, 'disk-files')
            print('PASS: extra disk creation/growth, shared mounts, backup selection and snapshot rollback', flush=True)
            api('set', base + '/disks/mp1', detach=1, digest=w.detail(identifier, node)['digest'])
            assert 'mp1' not in w.storage_config(identifier).get('additional', {})
            assert settings['additional']['mp1']['volid'] in w.storage_config(identifier)['unused']
            print('PASS: detach retains the native volume as unused', flush=True)
            original_ids = {row['id'] for row in w.container_records(identifier)}
            code = '''import os,sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w, disks
identifier,node=sys.argv[1:]
storage=w.pve_storage
def interrupted(identifier,action,**values):
 result=storage(identifier,action,**values)
 if action=='allocate': os._exit(79)
 return result
w.pve_storage=interrupted
disks.configure(identifier,node,'mp2',{'service':'app','target':'/more','storage':'local-lvm','size_mib':256},w.detail(identifier,node)['digest'])
'''
            result = subprocess.run(['python3', '-c', code, identifier, node])
            assert result.returncode == 79
            assert (project / 'allocated-volume.json').exists()
            allocated = json.loads((project / 'allocated-volume.json').read_text())['volid']
            call('dct', 'recover', vmid)
            assert not (project / 'disk-transaction.json').exists()
            assert allocated in w.storage_config(identifier)['unused']
            assert {row['id'] for row in w.container_records(identifier)} == original_ids
            print('PASS: killed allocation worker recovers through dct recover and retains the allocated volume', flush=True)
            call('dct', 'template', vmid)
            child_vmid = next(i for i in range(940, 980) if i != vmid and i not in {int(row['vmid']) for row in api('get', '/cluster/resources', type='vm')})
            children.append(child_vmid)
            from dctlib.endpoints import resolved
            used_ips = {endpoint.get('address') for guest in w.summaries() for endpoint in resolved(guest).values()}
            clone_ip = sys.argv[2] if len(sys.argv) > 2 else next('192.0.2.' + str(last) + '/20' for last in range(200, 238) if '192.0.2.' + str(last) not in used_ips)
            call('dct', 'clone', vmid, child_vmid, '--full', 0, '--hostname', 'linked-disk-test', '--net0', 'bridge=vmbr0,ip=' + clone_ip)
            child = next(row for row in api('get', '/cluster/resources', type='vm') if int(row['vmid']) == child_vmid)
            child_id = child['id'].split('/')[-1]
            assert w.storage_config(child_id)['volid'] != w.storage_config(identifier)['volid']
            call('dct', 'start', child_vmid)
            cid = w.container_records(child_id)[0]['id']
            assert call('docker', 'exec', cid, 'cat', '/data/value').strip() == 'kept'
            call('docker', 'exec', cid, 'sh', '-c', 'echo child > /data/value')
            source_data = Path(next(iter(w.storage_config(identifier)['mapping'].values()))) / 'value'
            assert source_data.read_text().strip() == 'kept', 'Child changed template data'
            call('dct', 'stop', child_vmid)
            call('dct', 'destroy', child_vmid, '--destroy-owned-data', 1)
            children.remove(child_vmid)
            print('PASS: LVM-thin linked clone preserves files and separates child writes from template data', flush=True)
        finally:
            for child_vmid in children:
                guests = api('get', '/cluster/resources', type='vm')
                if any(int(row['vmid']) == child_vmid for row in guests):
                    call('dct', 'stop', child_vmid)
                    call('dct', 'destroy', child_vmid, '--destroy-owned-data', 1)
            if identifier:
                with w.operation(identifier, 'recovery'):
                    pass
                for snapshot in w.snapshot_list(identifier, node):
                    if snapshot['name'] != 'current':
                        call('dct', 'delsnapshot', vmid, snapshot['name'])
                if not w.detail(identifier, node).get('template'):
                    call('dct', 'stop', vmid)
                call('dct', 'destroy', vmid, '--destroy-owned-data', 1)
            after = api('get', '/cluster/resources', type='vm')
            assert {row['vmid'] for row in before} == {row['vmid'] for row in after}
            print('PASS: original inventory retained and disposable guest removed', flush=True)


if __name__ == '__main__':
    main()
