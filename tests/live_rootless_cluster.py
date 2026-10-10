#!/usr/bin/env python3
"""Rootless data and cluster acceptance on explicitly approved test nodes.

Run on pve1. Retain the manifest on failure; --cleanup removes only fixtures
recorded here. The temporary ZFS pools never touch an existing storage pool.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
from live_pools_hooks import api, task, wait_for
from live_firewall import remote

STATE = Path('/root/dct-rootless-cluster.json')
STORAGE = 'bdrootless-zfs'
POOL = 'bdrootless'
VDEV = '/var/lib/betterdocker/rootless-test-vdev'


def run(cleanup=False):
    assert worker.current_node() == 'pve1'
    state = json.loads(STATE.read_text()) if STATE.exists() else dict(name='rootless-cluster-' + uuid.uuid4().hex[:8], guests=[])
    def save():
        STATE.write_text(json.dumps(state, indent=2)); STATE.chmod(0o600)
    def info(identifier):
        return next(i for i in worker.summaries() if i['id'] == identifier)
    def base(item):
        return '/nodes/' + info(item['id'])['node'] + '/docker/' + item['id']
    def power(item, action):
        task(api('create', base(item) + '/action', action=action, timeout=10))
    def check(item):
        current = info(item['id'])
        cid = remote(current['node'], ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=bd-' + item['id']], check=True).stdout.strip()
        result = remote(current['node'], ['docker', 'exec', cid, 'sh', '-c', 'cat /data/proof; stat -c %u:%g /data/proof'], check=True).stdout.strip()
        assert result == 'rootless-data-proof\n1234:2345', result
    save()
    if cleanup:
        for identifier in state['guests']:
            current = next((i for i in worker.summaries() if i['id'] == identifier), None)
            if not current:
                continue
            sid = 'docker:' + str(current['vmid'])
            if any(i['sid'] == sid for i in api('get', '/cluster/ha/resources')):
                api('set', '/cluster/ha/resources/' + sid, state='stopped')
                wait_for(lambda: info(identifier)['status'] == 'stopped', timeout=180)
                api('delete', '/cluster/ha/resources/' + sid)
            for job in api('get', '/cluster/replication'):
                if job['guest'] == current['vmid']:
                    api('delete', '/cluster/replication/' + job['id'])
                    remote(current['node'], ['pvesr', 'run', '--id', job['id']], check=True, timeout=300)
            if current.get('replication'):
                remote(current['node'], ['/usr/local/lib/betterdocker/replica.py', 'cleanup', identifier, current['node']], check=True)
            power(current, 'force-stop'); task(api('delete', base(current)))
            remote(current['node'], ['/usr/local/lib/betterdocker/provision.py', 'purge-retained-storage', identifier], check=True)
            for node in state.get('pools', []):
                script = ('import sys,json,shutil; sys.path.insert(0,"/usr/local/lib/betterdocker"); import provision as w; '
                          'p=w.ROOT/' + repr(identifier) + ';\nif p.exists():\n'
                          ' assert json.loads((p/"request.json").read_text())["name"] == ' + repr(state['name']) + '\n'
                          ' w.storage_unmount(' + repr(identifier) + ')\n'
                          ' unit=w.capture(["systemd-escape","--path","--suffix=mount",str(p/"storage-data")]).strip()\n'
                          ' (Path("/etc/systemd/system")/unit).unlink(missing_ok=True)\n'
                          ' shutil.rmtree(p)\n')
                script = 'from pathlib import Path;\n' + script
                containers = remote(node, ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=bd-' + identifier], check=True).stdout.split()
                if containers:
                    remote(node, ['docker', 'rm', *containers], check=True)
                volumes = remote(node, ['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier], check=True).stdout.split()
                if volumes:
                    remote(node, ['docker', 'volume', 'rm', *volumes], check=True)
                remote(node, ['python3', '-c', script], check=True)
        if state.get('rule') and any(i['rule'] == state['rule'] for i in api('get', '/cluster/ha/rules')):
            api('delete', '/cluster/ha/rules/' + state['rule'])
        if any(i['storage'] == STORAGE for i in api('get', '/storage')):
            api('delete', '/storage/' + STORAGE)
        for node in state.get('pools', []):
            datasets = remote(node, ['zfs', 'list', '-H', '-o', 'name', '-r', POOL], check=True).stdout.splitlines()
            for dataset in datasets[1:]:
                prefix = POOL + '/vm-' + str(state['vmid']) + '-disk-'
                assert dataset.startswith(prefix) and dataset[len(prefix):].isdigit(), dataset
                remote(node, ['zfs', 'destroy', '-r', dataset], check=True)
            datasets = remote(node, ['zfs', 'list', '-H', '-o', 'name', '-r', POOL], check=True).stdout.splitlines()
            assert datasets == [POOL], datasets
            remote(node, ['zpool', 'destroy', POOL], check=True)
            remote(node, ['rm', '--', VDEV], check=True)
        STATE.unlink(); print('PASS rootless cluster fixtures removed', flush=True); return
    identities = [json.loads(remote(n, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout) for n in ('pve2', 'pve3')]
    assert all(i['mode'] == 'rootless' for i in identities) and identities[0]['subuid'] != identities[1]['subuid']
    for node in ('pve2', 'pve3'):
        state.setdefault('pools', [])
        if node not in state['pools']:
            assert remote(node, ['zpool', 'list', POOL]).returncode != 0
            assert remote(node, ['test', '-e', VDEV]).returncode != 0
            remote(node, ['truncate', '-s', '4G', VDEV], check=True)
            remote(node, ['zpool', 'create', '-m', 'none', POOL, VDEV], check=True)
            state['pools'].append(node); save()
    if not any(i['storage'] == STORAGE for i in api('get', '/storage')):
        api('create', '/storage', storage=STORAGE, type='zfspool', pool=POOL, nodes='pve2,pve3', content='rootdir', sparse=1)
    current = next((i for i in worker.summaries() if i['name'] == state['name']), None)
    if not current:
        compose = 'services:\n  app:\n    image: busybox:stable\n    init: true\n    command: [sh, -c, "mkdir -p /data; test -f /data/proof || { echo rootless-data-proof > /data/proof; chown 1234:2345 /data/proof; }; exec sleep infinity"]\n    volumes: [data:/data]\nvolumes:\n  data: {}\n'
        task(api('create', '/nodes/pve3/docker', deployment=dict(name=state['name'], mode='compose', compose=compose, bridge='vmbr0', cidr='192.0.2.214/20')))
        current = next(i for i in worker.summaries() if i['name'] == state['name'])
    if current['id'] not in state['guests']:
        state['guests'].append(current['id']); save()
    state['id'] = current['id']; state['vmid'] = current['vmid']; save()
    power(current, 'start'); check(current)
    power(current, 'shutdown')
    disk = json.loads(remote(info(current['id'])['node'], ['/usr/local/lib/betterdocker/provision.py', 'storage-config', current['id'], info(current['id'])['node']], check=True).stdout)
    if not disk.get('size_mib'):
        task(api('set', base(current) + '/storage-limit', size_mib=512, storage=STORAGE))
    power(current, 'start'); check(current); power(current, 'shutdown')
    if info(current['id'])['node'] != 'pve2':
        task(api('create', base(current) + '/migrate', target='pve2', storage=STORAGE))
    power(current, 'start'); check(current); power(current, 'shutdown')
    assert info(current['id'])['vmid'] == state['vmid']
    print('PASS rootless migration preserves logical owners across different UID ranges', flush=True)
    before = info(current['id'])
    try:
        task(api('create', base(current) + '/migrate', target='pve1', storage='local-lvm'))
    except RuntimeError as error:
        assert 'modes differ' in str(error), str(error)
    else:
        raise AssertionError('Cross-mode migration succeeded')
    assert info(current['id'])['node'] == before['node'] and info(current['id'])['status'] == 'stopped'
    print('PASS incompatible target rejected before source changes', flush=True)
    job = str(current['vmid']) + '-0'; state['job'] = job; save()
    if not any(i['id'] == job for i in api('get', '/cluster/replication')):
        api('create', '/cluster/replication', id=job, target='pve3', type='local', schedule='00:59')
    remote('pve2', ['pvesr', 'run', '--id', job], check=True, timeout=300)
    wait_for(lambda: 'pve3' in api('get', base(current) + '/management').get('replication', {}).get('targets', {}))
    print('PASS native ZFS replication completes between rootless nodes', flush=True)
    state['replicated'] = True; save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true')
    run(parser.parse_args().cleanup)
