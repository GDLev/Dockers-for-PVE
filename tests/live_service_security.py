#!/usr/bin/env python3
"""Exercise security, health, immutable updates and recovery on a disposable PVE DCT."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, '/usr/local/lib/betterdocker')
import runtime
from dctlib import projects as w, images


def call(*args, fail=False):
    runner = runtime.run if args[0] == 'docker' else subprocess.run
    result = runner(list(map(str, args)), text=True, capture_output=True)
    if fail:
        assert result.returncode, 'Unexpected success'
        return result.stdout + result.stderr
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def api(method, path, **values):
    command = ['pvesh', method, path, '--output-format', 'json']
    for key, value in values.items():
        command += ['--' + key, str(value)]
    output = call(*command)
    try: result = json.loads(output)
    except json.JSONDecodeError:
        result = json.loads(output.strip().splitlines()[-1])
    if isinstance(result, str) and result.startswith('UPID:'):
        node = result.split(':')[1]
        for _ in range(1800):
            status = json.loads(call('pvesh', 'get', '/nodes/' + node + '/tasks/' + result + '/status', '--output-format', 'json'))
            if status['status'] == 'stopped':
                if status['exitstatus'] != 'OK':
                    raise RuntimeError(call('pvesh', 'get', '/nodes/' + node + '/tasks/' + result + '/log', '--output-format', 'json'))
                return result
            time.sleep(.2)
        raise TimeoutError(result)
    return result


def main():
    before = json.loads(call('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))
    vmid = next(value for value in range(960 if runtime.load().rootless else 940, 980 if runtime.load().rootless else 960) if value not in {int(r['vmid']) for r in before})
    identifier = None
    config = {'services': {
        'app': {'image': 'busybox:1.37', 'init': True, 'command': ['sleep', '86400'],
            'tmpfs': ['/tmp'], 'volumes': ['./data:/data'],
            'healthcheck': {'test': ['CMD', 'test', '-f', '/tmp/healthy'], 'interval': '1s', 'timeout': '1s', 'retries': 1}},
        'sidecar': {'image': 'busybox:1.37', 'init': True, 'command': ['sleep', '86400']}}}
    with tempfile.TemporaryDirectory(prefix='bd-security-test-') as folder:
        source = Path(folder) / 'compose.json'; source.write_text(json.dumps(config))
        try:
            call('dct', 'create', vmid, source, '--hostname', 'security-update-test', '--net0', 'bridge=vmbr0,ip=' + sys.argv[1], '--start', 0)
            row = next(r for r in json.loads(call('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json')) if int(r['vmid']) == vmid)
            identifier, node = row['id'].split('/')[-1], row['node']
            base = '/nodes/' + node + '/docker/' + identifier
            project = w.ROOT / identifier
            call('dct', 'start', vmid)
            app = next(r['id'] for r in w.container_records(identifier) if r['service'] == 'app')
            call('docker', 'exec', app, 'chmod', '777', '/data')
            settings = {'user': '65534:65534', 'read_only': True, 'cap_drop': ['ALL'], 'no_new_privileges': True}
            if not runtime.load().rootless:
                settings['apparmor'] = 'docker-default'
            old = w.container_records(identifier)
            api('set', base + '/settings', service='app', settings=json.dumps(settings), digest=w.detail(identifier, node)['digest'])
            assert any('pending' in r for r in api('get', base + '/pending', service='app'))
            assert w.container_records(identifier)[0]['id'] == old[0]['id']
            call('dct', 'reboot', vmid)
            rows = w.container_records(identifier)
            app = next(r['id'] for r in rows if r['service'] == 'app')
            sidecar = next(r['id'] for r in rows if r['service'] == 'sidecar')
            record = json.loads(call('docker', 'inspect', app))[0]
            assert record['Config']['User'] == '65534:65534'
            assert record['HostConfig']['ReadonlyRootfs']
            assert record['HostConfig']['CapDrop'] == ['ALL']
            assert any('no-new-privileges' in opt for opt in record['HostConfig']['SecurityOpt'])
            if not runtime.load().rootless:
                assert record['AppArmorProfile'] == 'docker-default'
            status = call('docker', 'exec', app, 'cat', '/proc/self/status')
            assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
            assert 'Read-only file system' in call('docker', 'exec', app, 'touch', '/forbidden', fail=True)
            call('docker', 'exec', app, 'sh', '-c', 'echo original > /data/value; touch /tmp/healthy')
            call('docker', 'exec', sidecar, 'sh', '-c', 'echo retained > /evidence')
            for _ in range(30):
                if next(r for r in w.container_records(identifier) if r['service'] == 'app')['health'] == 'healthy': break
                time.sleep(.5)
            else: raise AssertionError('Health never became healthy')
            call('docker', 'exec', app, 'rm', '/tmp/healthy')
            for _ in range(30):
                if next(r for r in w.container_records(identifier) if r['service'] == 'app')['health'] == 'unhealthy': break
                time.sleep(.5)
            else: raise AssertionError('Health never became unhealthy')
            print('Security and health verified on ' + node, flush=True)
            call('dct', 'shutdown', vmid)
            assert next(r for r in w.container_records(identifier) if r['service'] == 'app')['health'] == 'inactive'
            assert w.delegated_access(identifier, node)['safe']
            bad = Path(folder) / 'bad.json'; bad.write_text(json.dumps({'apparmor': 'not-loaded-stage8'}))
            assert ('rootful' if runtime.load().rootless else 'unavailable') in call('/usr/local/lib/betterdocker/provision.py', 'settings', identifier, node, 'app', bad, w.detail(identifier, node)['digest'], fail=True)
            api('create', base + '/images/preview', images=json.dumps({'app': 'busybox:1.36'}), digest=w.detail(identifier, node)['digest'])
            plan = api('get', base + '/images')
            assert plan['changes'][0]['changed']
            candidate = plan['changes'][0]['candidate']
            api('create', base + '/images', token=plan['token'], recovery='image-recovery', digest=plan['digest'])
            rows = w.container_records(identifier)
            assert next(r for r in rows if r['service'] == 'sidecar')['id'] == sidecar
            assert all(r['state'] == 'created' or r['state'] == 'exited' for r in rows)
            new_app = next(r['id'] for r in rows if r['service'] == 'app')
            assert json.loads(call('docker', 'inspect', new_app))[0]['Image'] == candidate
            call('dct', 'snapshot', vmid, 'pinned-image')
            import tarfile
            with tarfile.open(w.snapshot_root(identifier) / 'pinned-image' / 'images.tar') as archive:
                assert any(candidate.split(':')[1] in member.name for member in archive), 'Pinned image missing from portable snapshot'
            call('dct', 'delsnapshot', vmid, 'pinned-image')
            call('dct', 'start', vmid)
            assert call('docker', 'exec', sidecar, 'cat', '/evidence').strip() == 'retained'
            call('docker', 'exec', new_app, 'sh', '-c', 'echo changed > /data/value')
            call('dct', 'shutdown', vmid)
            call('dct', 'rollback', vmid, 'image-recovery')
            call('dct', 'start', vmid)
            rows = w.container_records(identifier)
            app = next(r['id'] for r in rows if r['service'] == 'app')
            assert call('docker', 'exec', app, 'cat', '/data/value').strip() == 'original'
            call('dct', 'shutdown', vmid)
            call('dct', 'delsnapshot', vmid, 'image-recovery')
            print('Image update and rollback verified on ' + node, flush=True)
            # Inject a failure after the durable recovery snapshot and runtime rewrite.
            images.preview(identifier, node, {'app': 'busybox:1.36'}, w.detail(identifier, node)['digest'])
            plan = images.state(project)
            original_run = w.run
            def interrupted(command, **kwargs):
                if '--force-recreate' in command: raise RuntimeError('Injected interrupted update')
                return original_run(command, **kwargs)
            with w.operation(identifier, 'update'), patch.object(w, 'run', side_effect=interrupted):
                try: images.apply(identifier, node, plan['token'], 'interrupted-update', plan['digest'])
                except ValueError as error: assert 'roll back' in str(error)
                else: raise AssertionError('Interruption did not fail')
            assert 'Interrupted image update' in call('dct', 'start', vmid, fail=True)
            call('dct', 'rollback', vmid, 'interrupted-update')
            call('dct', 'delsnapshot', vmid, 'interrupted-update')
            print('PASS: real security flags, health transitions, immutable update, data/layer recovery and interrupted-update guard on ' + node, flush=True)
        finally:
            if identifier:
                # Always recover an incomplete update before destroying only this fixture.
                state = images.state(w.ROOT / identifier)
                if state.get('phase') == 'applying': call('dct', 'rollback', vmid, state['recovery'])
                for snapshot in w.snapshot_list(identifier, row['node']):
                    if snapshot['name'] != 'current': call('dct', 'delsnapshot', vmid, snapshot['name'])
                call('dct', 'stop', vmid)
                call('dct', 'destroy', vmid, '--destroy-owned-data', 1)
            after = json.loads(call('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))
            assert {r['vmid'] for r in before if r.get('name') != 'security-update-test'} == {r['vmid'] for r in after if r.get('name') != 'security-update-test'}, 'Inventory changed'


if __name__ == '__main__':
    main()
