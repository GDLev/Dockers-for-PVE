#!/usr/bin/env python3
"""Back up and migrate a disposable DCT with additional managed disks."""
import live_environment as env
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from live_service_security import api, call, w
from transfer import ssh_command


def remote(node, *args, check=True):
    return subprocess.run(ssh_command(node) + [shlex.join(list(map(str, args)))], check=check, text=True, capture_output=True).stdout


def guest(identifier):
    return next(row for row in w.summaries() if row['id'] == identifier)


def base(identifier):
    return '/nodes/' + guest(identifier)['node'] + '/docker/' + identifier


def current(identifier):
    return api('get', base(identifier) + '/management')


def power(identifier, action):
    api('create', base(identifier) + '/action', action=action, timeout=10)


def exec_guest(identifier, *args, check=True):
    info = current(identifier)
    return remote(guest(identifier)['node'], 'docker', 'exec', info['containers'][0]['id'], *args, check=check)


def main():
    before = api('get', '/cluster/resources', type='vm')
    ids = [value for value in range(940, 980) if value not in {int(row['vmid']) for row in before}][:2]
    identifiers = []
    archive = None
    with tempfile.TemporaryDirectory(prefix='bd-cluster-release-') as folder:
        source = Path(folder) / 'compose.json'
        source.write_text(json.dumps({'services': {'app': {'image': 'busybox:1.37', 'init': True,
            'command': ['sleep', '86400'], 'volumes': ['./data:/data']}}}))
        try:
            call('dct', 'create', ids[0], source, '--hostname', 'release-cluster-test', '--net0', f'bridge=vmbr0,ip={env.address(225)}/{env.prefix()}', '--start', 1)
            identifier = next(row['id'] for row in w.summaries() if row.get('vmid') == ids[0])
            identifiers.append(identifier)
            exec_guest(identifier, 'sh', '-c', 'echo writable >/marker; echo persistent >/data/value')
            power(identifier, 'force-stop')
            api('set', base(identifier) + '/storage-limit', storage='local-lvm', size_mib=256, digest=current(identifier)['digest'])
            api('set', base(identifier) + '/disks/mp1', service='app', target='/excluded', storage='local-lvm', size_mib=256, backup=0, digest=current(identifier)['digest'])
            power(identifier, 'start')
            exec_guest(identifier, 'sh', '-c', 'echo all-transfers >/excluded/value')
            power(identifier, 'force-stop')
            api('create', f"/nodes/{env.node('primary')}/vzdump", vmid=ids[0], storage=env.backup_storage(), mode='stop')
            backups = api('get', f"/nodes/{env.node('primary')}/storage/{env.backup_storage()}/content", content='backup', vmid=ids[0])
            row = max(backups, key=lambda row: row['ctime'])
            archive = row['volid']
            assert row['subtype'] == 'docker'
            used = {entry.get('address') for entry in w.summaries()}
            restore_ip = next(env.address(last) + '/' + str(env.prefix()) for last in range(200, 224) if env.address(last) not in used)
            api('create', f"/nodes/{env.node('primary')}/docker", archive=archive, newid=ids[1], storage='local-lvm',
                deployment=json.dumps({'name': 'release-pbs-restore-test', 'bridge': 'vmbr0', 'cidr': restore_ip}))
            restored = next(row['id'] for row in w.summaries() if row.get('vmid') == ids[1])
            identifiers.append(restored)
            power(restored, 'start')
            assert exec_guest(restored, 'cat', '/marker').strip() == 'writable'
            assert exec_guest(restored, 'cat', '/data/value').strip() == 'persistent'
            assert not exec_guest(restored, 'find', '/excluded', '-name', 'value').strip()
            power(restored, 'force-stop')
            print('PASS: DCT PBS backup/restore preserves writable and primary data, and omits backup=0 disk contents', flush=True)
            api('create', base(identifier) + '/migrate', target=env.node('secondary'), storage='local-lvm', digest=current(identifier)['digest'])
            assert guest(identifier)['node'] == env.node('secondary')
            power(identifier, 'start')
            assert exec_guest(identifier, 'cat', '/marker').strip() == 'writable'
            assert exec_guest(identifier, 'cat', '/data/value').strip() == 'persistent'
            assert exec_guest(identifier, 'cat', '/excluded/value').strip() == 'all-transfers'
            power(identifier, 'force-stop')
            api('create', base(identifier) + '/migrate', target=env.node('primary'), storage='local-lvm', digest=current(identifier)['digest'])
            assert guest(identifier)['node'] == env.node('primary')
            print('PASS: cold migration and return preserve every managed disk, including backup-excluded data', flush=True)
        finally:
            for identifier in reversed(identifiers):
                row = next((row for row in w.summaries() if row['id'] == identifier), None)
                if row:
                    power(identifier, 'force-stop')
                    api('delete', base(identifier), **{'destroy-owned-data': 1})
            if archive:
                api('delete', f"/nodes/{env.node('primary')}/storage/{env.backup_storage()}/content/" + archive)
            after = api('get', '/cluster/resources', type='vm')
            assert {row['vmid'] for row in before} == {row['vmid'] for row in after}
            print('PASS: disposable cluster guests and PBS archive removed, original inventory retained', flush=True)


if __name__ == '__main__':
    main()
