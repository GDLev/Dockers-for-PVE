import live_environment as env
#!/usr/bin/env python3
f"Run on a test PVE node. Creates disposable DCTs, writes a PBS backup, then removes its guests.\n\nSupply three unused addresses on the bridge and a second node with BetterDockers.\nExample: python3 tests/live_acceptance.py --target {env.node('rootless')} --backup-storage {env.backup_storage()}\n  --address {env.address(225)} --clone-address {env.address(224)} --restore-address {env.address(223)}\n"
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
import transfer


def api(method, route, **values):
    command = ['pvesh', method, route, '--output-format', 'json']
    for key, value in values.items():
        command += ['--' + key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return json.loads(output.splitlines()[-1])


def task(upid):
    # Remote pvesh can include CLI worker output in its proxied string result.
    matches = re.findall(r'UPID:[^"\s]+:', upid)
    assert len(matches) == 1, upid
    upid = matches[0]
    node = upid.split(':')[1]
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = api('get', f'/nodes/{node}/tasks/{upid}/status')
        if status['status'] == 'stopped':
            if status.get('exitstatus') != 'OK':
                raise RuntimeError(json.dumps(api('get', f'/nodes/{node}/tasks/{upid}/log')))
            print('OK', upid.split(':')[5], flush=True)
            return
        time.sleep(1)
    raise RuntimeError('Task did not finish: ' + upid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('target', 'backup-storage', 'address', 'clone-address', 'restore-address'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--bridge', default='vmbr0')
    parser.add_argument('--prefix', type=int, default=20)
    parser.add_argument('--data-storage', default='local-lvm')
    args = parser.parse_args()
    node = worker.current_node()
    assert node != args.target
    assert len({args.address, args.clone_address, args.restore_address}) == 3
    nonce = uuid.uuid4().hex[:8]
    name = 'acceptance-' + nonce
    owned = []
    manifest = Path('/root/betterdocker-stage8-live-' + nonce + '.json')

    def remember(label):
        rows = [row for row in worker.summaries() if row['name'] == label]
        assert len(rows) == 1, rows
        owned.append((rows[0]['id'], label))
        manifest.write_text(json.dumps(owned))
        return rows[0]['id']

    def base(identifier):
        owner = json.loads((worker.METADATA / (identifier + '.json')).read_text())['node']
        return f'/nodes/{owner}/docker/{identifier}'

    def management(identifier):
        return api('get', base(identifier) + '/management')

    def digest(identifier):
        return api('get', base(identifier))['digest']

    def power(identifier, verb):
        task(api('create', base(identifier) + '/action', action=verb))

    def docker(identifier, *arguments):
        owner = json.loads((worker.METADATA / (identifier + '.json')).read_text())['node']
        command = ['docker', *arguments]
        if owner != node:
            command = transfer.ssh_command(owner) + [shlex.join(command)]
        return subprocess.check_output(command, text=True)

    def app(identifier):
        instances = docker(identifier, 'ps', '-aq', '--filter', 'label=betterdocker.id=' + identifier,
                           '--filter', 'label=com.docker.compose.service=app').split()
        assert len(instances) == 1, instances
        return instances[0]

    def value(identifier, filename):
        return docker(identifier, 'exec', app(identifier), 'cat', filename).strip()

    compose = {'services': {'app': {'image': 'busybox:1.37', 'restart': 'no',
        'cpus': 1, 'mem_limit': '128m', 'pids_limit': 64,
        'command': ['sh', '-c', "echo acceptance-ready; trap 'exit 0' TERM; while true; do sleep 1; done"],
        'volumes': ['./data:/data', 'records:/records']}}, 'volumes': {'records': {}}}
    deployment = {'name': name, 'mode': 'compose', 'compose': json.dumps(compose), 'environment': 'CHECK=stage8\n',
                  'bridge': args.bridge, 'ipv4mode': 'static', 'address': args.address,
                  'cidr': f'{args.address}/{args.prefix}'}
    try:
        task(api('create', f'/nodes/{node}/docker', deployment=deployment))
        identifier = remember(name)
        initial = management(identifier)
        assert initial['status'] == 'running'
        settings = {'cpus': 0.5, 'memory': 64, 'pids': 32, 'restart': 'no'}
        task(api('set', base(identifier) + '/settings', service='app', settings=settings, digest=initial['digest']))
        host = json.loads(docker(identifier, 'inspect', app(identifier)))[0]['HostConfig']
        assert host['NanoCpus'] == 500000000 and host['Memory'] == 67108864 and host['PidsLimit'] == 32
        task(api('set', base(identifier) + '/settings', service='app', settings=dict(settings, dns=['9.9.9.9']), digest=digest(identifier)))
        pending = api('get', base(identifier) + '/pending', service='app')
        assert any(row.get('key') == 'dns' and row.get('pending') for row in pending), pending
        task(api('set', base(identifier) + '/pending', service='app', revert='dns', digest=digest(identifier)))
        before = digest(identifier)
        api('set', base(identifier) + '/config', description='Stage 8 acceptance', tags='acceptance', digest=before)
        try:
            api('set', base(identifier) + '/config', description='stale', digest=before)
        except RuntimeError as error:
            assert 'modified' in str(error).lower() or 'digest' in str(error).lower(), error
        else:
            raise AssertionError('Stale configuration write was accepted')
        power(identifier, 'shutdown')
        task(api('set', base(identifier) + '/network', mtu=1450, digest=digest(identifier)))
        assert api('get', base(identifier) + '/network')['mtu'] == 1450
        power(identifier, 'start')
        docker(identifier, 'exec', app(identifier), 'sh', '-c', 'echo original > /data/value; echo named > /records/value; echo layer > /layer-value')
        task(api('create', base(identifier) + '/snapshot', action='create', snapname='before'))
        docker(identifier, 'exec', app(identifier), 'sh', '-c', 'echo changed > /data/value; echo changed > /layer-value')
        task(api('create', base(identifier) + '/snapshot', action='restore', snapname='before'))
        assert management(identifier)['status'] == 'stopped'
        snapshots = api('get', base(identifier) + '/snapshots')
        assert [row['name'] for row in snapshots if row['name'] != 'current'] == ['before'], snapshots
        power(identifier, 'start')
        assert value(identifier, '/data/value') == 'original' and value(identifier, '/layer-value') == 'layer'
        assert 'acceptance-ready' in api('get', base(identifier) + '/logs', service='app')
        task(api('create', base(identifier) + '/snapshot', action='delete', snapname='before'))
        power(identifier, 'shutdown')
        task(api('set', base(identifier) + '/storage-limit', size_mib=256, storage=args.data_storage, digest=digest(identifier)))
        power(identifier, 'start')
        assert value(identifier, '/data/value') == 'original' and value(identifier, '/records/value') == 'named'
        docker(identifier, 'exec', app(identifier), 'sh', '-c', 'echo portable-layer > /layer-value')
        power(identifier, 'shutdown')
        task(api('create', base(identifier) + '/clone', deployment={'name':name+'-clone','bridge':args.bridge,
            'address':args.clone_address,'cidr':f'{args.clone_address}/{args.prefix}'}, digest=digest(identifier)))
        clone = remember(name+'-clone')
        assert management(clone)['status'] == 'stopped'
        power(clone, 'start')
        assert value(clone, '/data/value') == 'original' and value(clone, '/layer-value') == 'portable-layer'
        docker(clone, 'exec', app(clone), 'sh', '-c', 'echo independent > /data/value')
        power(clone, 'shutdown')
        vmid = management(identifier)['vmid']
        subprocess.run(['vzdump', str(vmid), '--storage', args.backup_storage, '--mode', 'stop'], check=True)
        backups = api('get', f'/nodes/{node}/storage/{args.backup_storage}/content', content='backup', vmid=vmid)
        archive = sorted(backups, key=lambda row:row.get('ctime',0))[-1]['volid']
        task(api('create', f'/nodes/{node}/docker', archive=archive, deployment={'name':name+'-restore','bridge':args.bridge,
            'address':args.restore_address,'cidr':f'{args.restore_address}/{args.prefix}'}))
        restored = remember(name+'-restore')
        assert management(restored)['status'] == 'stopped'
        power(restored, 'start')
        assert value(restored, '/data/value') == 'original' and value(restored, '/layer-value') == 'portable-layer'
        power(restored, 'shutdown')
        current = management(identifier)
        identity = {key: current[key] for key in ('id', 'vmid', 'address', 'hwaddr')}
        task(api('create', base(identifier) + '/migrate', target=args.target, digest=digest(identifier)))
        current = management(identifier)
        assert current['node'] == args.target
        assert {key: current[key] for key in identity} == identity
        power(identifier, 'start')
        assert value(identifier, '/data/value') == 'original' and value(identifier, '/layer-value') == 'portable-layer'
        power(identifier, 'shutdown')
        task(api('create', base(identifier) + '/template', digest=digest(identifier)))
        assert management(identifier)['template']
        try:
            power(identifier, 'start')
        except RuntimeError as error:
            assert 'template' in str(error).lower(),error
        else:
            raise AssertionError('Template was started')
        print('STAGE8_CREATE_SETTINGS_PENDING_DIGEST_SNAPSHOT_NETWORK_DISK_CLONE_PBS_RESTORE_MIGRATION_TEMPLATE_VERIFIED', flush=True)
    finally:
        for identifier, label in reversed(owned):
            path = worker.METADATA / (identifier + '.json')
            if not path.exists():continue
            current=json.loads(path.read_text())
            assert current['name']==label and label.startswith(name), 'Fixture ownership changed'
            power(identifier,'force-stop')
            task(api('create',base(identifier)+'/action',action='delete'))
            command=['/usr/local/lib/betterdocker/provision.py','purge-retained-storage',identifier]
            if current['node']!=node: command=transfer.ssh_command(current['node'])+[shlex.join(command)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode and 'No retained PVE disks' not in result.stderr:
                raise RuntimeError(result.stderr.strip())
        manifest.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
