#!/usr/bin/env python3
'Verify writable layers and interrupted service swaps on a disposable PVE guest.\n\nRun on a test PVE node: python3 live_service_preservation.py <address>/<prefix>\nThe guest and its volumes are removed, leaving the original guests untouched.\n'
import live_environment as env
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from live_service_security import api, call, runtime, w


def main():
    before = api('get', '/cluster/resources', type='vm')
    vmid = next(i for i in range(940, 980) if i not in {int(r['vmid']) for r in before})
    identifier = None
    owned_anonymous = set()
    config = {'services': {
        'app': {'image': 'busybox:1.37', 'init': True, 'command': ['sleep', '86400'],
            'deploy': {'replicas': 2}, 'restart': 'unless-stopped',
            'tmpfs': ['/tmp'], 'volumes': ['shared:/shared', '/anonymous', './data:/data']},
        'sidecar': {'image': 'busybox:1.37', 'init': True, 'command': ['sleep', '86400']}},
        'volumes': {'shared': {}}}
    with tempfile.TemporaryDirectory(prefix='bd-preserve-test-') as folder:
        source = Path(folder) / 'compose.json'; source.write_text(json.dumps(config))
        try:
            call('dct', 'create', vmid, source, '--hostname', 'service-preservation-test',
                '--net0', 'bridge=vmbr0,ip=' + sys.argv[1], '--start', 1)
            row = next(r for r in api('get', '/cluster/resources', type='vm') if int(r['vmid']) == vmid)
            identifier, node = row['id'].split('/')[-1], row['node']
            base = '/nodes/' + node + '/docker/' + identifier
            project = w.ROOT / identifier

            def apps():
                return sorted((r for r in w.container_records(identifier) if r['service'] == 'app'), key=lambda r: r['number'])

            def settings(values):
                api('set', base + '/settings', service='app', settings=json.dumps(values), digest=w.detail(identifier, node)['digest'])

            def action(verb):
                api('create', base + '/service-action', service='app', action=verb)

            def assert_files():
                for app in apps():
                    cid, number = app['id'], app['number']
                    assert call('docker', 'exec', '--user', '1234:2345', cid, 'cat', '/retained/value').strip() == number
                    assert call('docker', 'exec', '--user', '1234:2345', cid, 'stat', '-c', '%u:%g:%a', '/retained/value').strip() == '1234:2345:640'
                    assert call('docker', 'exec', '--user', '1234:2345', cid, 'readlink', '/retained/link').strip() == 'value'
                    assert call('docker', 'exec', '--user', '1234:2345', cid, 'cat', '/anonymous/value').strip() == number
                    assert call('docker', 'exec', '--user', '1234:2345', cid, 'cat', '/shared/value').strip() == 'shared'
                    assert call('docker', 'exec', '--user', '1234:2345', cid, 'cat', '/data/value').strip() == 'bind'

            originals = apps()
            anonymous = {}
            for app in originals:
                cid = app['id']
                call('docker', 'exec', cid, 'sh', '-c', 'mkdir /retained; echo "$1" > /retained/value; chown 1234:2345 /retained/value; chmod 640 /retained/value; ln -s value /retained/link; echo "$1" > /anonymous/value; echo shared > /shared/value; echo bind > /data/value', 'sh', app['number'])
                inspect = json.loads(call('docker', 'inspect', cid))[0]
                anonymous[app['number']] = next(m['Name'] for m in inspect['Mounts'] if m['Destination'] == '/anonymous')
                owned_anonymous.add(anonymous[app['number']])
            sidecar = next(r['id'] for r in w.container_records(identifier) if r['service']=='sidecar')
            call('docker', 'exec', sidecar, 'sh', '-c', 'echo untouched > /sidecar-marker')
            secure = {'user': '65534:65534', 'read_only': True, 'cap_drop': ['ALL'], 'no_new_privileges': True,
                'dns': ['1.1.1.1'], 'memory': 64, 'swap': 64, 'cpus': 0.5}
            if not runtime.load().rootless: secure['apparmor'] = 'docker-default'
            settings(secure)
            assert [r['id'] for r in apps()] == [r['id'] for r in originals]
            assert any('pending' in r for r in api('get', base + '/pending', service='app'))
            action('stop')
            # Applying to a stopped service must not start it or touch its sidecar.
            settings(secure)
            assert all(r['state'] in ('created','exited') for r in apps())
            assert next(r for r in w.container_records(identifier) if r['service']=='sidecar')['id'] == sidecar
            assert call('docker', 'exec', sidecar, 'cat', '/sidecar-marker').strip() == 'untouched'
            assert not (project / 'snapshot-tree.json').exists(), 'Settings created an unsolicited snapshot'
            action('start')
            assert_files()
            for app in apps():
                actual = json.loads(call('docker', 'inspect', app['id']))[0]
                host = actual['HostConfig']
                assert actual['Config']['User'] == '65534:65534'
                assert host['ReadonlyRootfs'] and host['CapDrop'] == ['ALL']
                assert host['Dns'] == ['1.1.1.1'] and host['Memory'] == 64*1024*1024 and host['NanoCpus'] == 500000000
                assert next(m['Name'] for m in actual['Mounts'] if m['Destination']=='/anonymous') == anonymous[app['number']]
                status = call('docker', 'exec', app['id'], 'cat', '/proc/self/status')
                assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
                assert 'Read-only file system' in call('docker', 'exec', app['id'], 'touch', '/forbidden', fail=True)
                if not runtime.load().rootless: assert actual['AppArmorProfile'] == 'docker-default'
            print('PASS: replicas, ownership, permissions, volumes, bindings and security on ' + node, flush=True)
            call('dct', 'stop', vmid)
            call('dct', 'snapshot', vmid, 'preserved-files')
            call('dct', 'rollback', vmid, 'preserved-files')
            settings({'user': '', 'read_only': False, 'cap_drop': [], 'no_new_privileges': False, 'apparmor': ''})
            action('start'); assert_files()
            assert all(call('docker','exec',r['id'],'id','-u').strip() == '0' for r in apps())
            action('stop')
            api('create', base + '/reset-settings', service='app', digest=w.detail(identifier,node)['digest'])
            action('start'); assert_files()
            assert all(not json.loads(call('docker','inspect', r['id']))[0]['HostConfig']['ReadonlyRootfs'] for r in apps())
            action('stop')
            print('PASS: snapshot rollback, clearing image user and Compose defaults preserve files', flush=True)
            original_ids = [r['id'] for r in apps()]
            original_engine = w.docker_engine
            creates = 0
            def fail_second(method, path, body=None):
                nonlocal creates
                if path.startswith('/containers/create'):
                    creates += 1
                    if creates == 2: raise ValueError('Injected second replica failure')
                return original_engine(method, path, body)
            with w.operation(identifier, 'settings'), patch.object(w, 'docker_engine', side_effect=fail_second):
                try: w.service_settings(identifier,node,'app', {'read_only': True}, w.detail(identifier,node)['digest'])
                except ValueError as error: assert 'Injected' in str(error)
                else: raise AssertionError('Failure injection not reached')
            assert [r['id'] for r in apps()] == original_ids
            assert not (project/'settings-transaction.json').exists()
            # Kill after Docker created a replacement, before recording its ID.
            code = '''import os,sys
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as w
identifier,node=sys.argv[1:]
engine=w.docker_engine
def interrupted(method,path,body=None):
 result=engine(method,path,body)
 if path.startswith('/containers/create'): os._exit(79)
 return result
w.docker_engine=interrupted
with w.operation(identifier,'settings'):
 w.service_settings(identifier,node,'app',{'read_only':True},w.detail(identifier,node)['digest'])
'''
            result = subprocess.run(['python3','-c',code,identifier,node])
            assert result.returncode == 79
            assert (project/'settings-transaction.json').exists()
            with w.operation(identifier, 'settings'):
                assert [r['id'] for r in apps()] == original_ids
                w.revert_settings(identifier,node,'app','read_only',w.detail(identifier,node)['digest'])
            action('start'); assert_files()
            action('stop')
            settings({'read_only': True}); action('start'); assert_files()
            print('PASS: failed replica and killed worker recover originals; retry preserves files', flush=True)
        finally:
            if identifier:
                with w.operation(identifier,'settings'): pass
                for snapshot in w.snapshot_list(identifier,node):
                    if snapshot['name'] != 'current': call('dct','delsnapshot',vmid,snapshot['name'])
                call('dct','stop',vmid)
                call('dct','destroy',vmid,'--destroy-owned-data',1)
                for volume in owned_anonymous:
                    check = runtime.run(['docker','volume','inspect',volume],text=True,capture_output=True)
                    if check.returncode == 0:
                        call('docker','volume','rm',volume)
            after = api('get','/cluster/resources',type='vm')
            assert {r['vmid'] for r in before} == {r['vmid'] for r in after}, 'Guest inventory changed'
            print('PASS: fixture removed, original guests preserved', flush=True)


if __name__ == '__main__': main()
