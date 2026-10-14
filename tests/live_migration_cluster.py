#!/usr/bin/env python3
"""Verify replicated DCT ownership changes and native HA relocation."""
import live_environment as env
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from live_cluster import *

STATE = Path('/root/bd-migration-cluster.json')


def cleanup():
    saved = json.loads(STATE.read_text()); identifier = saved.get('id')
    if not identifier:
        STATE.unlink(); return
    if saved.get('ha') and any(row['sid'] == saved['ha'] for row in api('get', '/cluster/ha/resources')):
        api('set', '/cluster/ha/resources/' + saved['ha'], state='stopped')
        wait_for(lambda: current(identifier)['status'] == 'stopped', timeout=180)
        api('delete', '/cluster/ha/resources/' + saved['ha'])
    if saved.get('job') and any(row['id'] == saved['job'] for row in api('get', '/cluster/replication')):
        jobs = api('get', '/cluster/replication')
        job = next(row for row in jobs if row['id'] == saved['job'])
        if not job.get('remove_job'):
            api('delete', '/cluster/replication/' + saved['job'])
        remote(get(identifier)['node'], ['pvesr', 'run', '--id', saved['job']], check=True, timeout=300)
        wait_for(lambda: not current(identifier).get('replication'))
    assert get(identifier)['name'] == saved['name']
    power(identifier, 'force-stop')
    task(api('delete', base(get(identifier)), **{'destroy-owned-data': 1}))
    STATE.unlink()


def run():
    assert not STATE.exists()
    saved = {'name': 'migration-cluster-' + uuid.uuid4().hex[:8]}; STATE.write_text(json.dumps(saved))
    deployment = {'name': saved['name'], 'mode': 'compose', 'bridge': 'vmbr0', 'cidr': f'{env.address(236)}/{env.prefix()}',
        'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /data; test -e /data/value || echo cluster-ok >/data/value; exec httpd -f -p 8080 -h /data"]\n    ports: ["8080:8080"]\n    volumes: ["./data:/data"]\n'}
    task(api('create', f"/nodes/{env.node('primary')}/docker", deployment=json.dumps(deployment), start=1))
    identifier = next(row['id'] for row in w.summaries() if row['name'] == saved['name'])
    saved['id'] = identifier; STATE.write_text(json.dumps(saved))
    power(identifier, 'force-stop')
    task(api('set', base(get(identifier)) + '/storage-limit', size_mib=512, storage='local-zfs', digest=current(identifier)['digest']))
    power(identifier, 'start')
    command(identifier, 'sh', '-c', 'echo retained >/preserved')
    job = str(get(identifier)['vmid']) + '-0'
    api('create', '/cluster/replication', id=job, target=env.node('secondary'), type='local', schedule='00:59')
    saved['job'] = job; STATE.write_text(json.dumps(saved))
    remote(env.node('primary'), ['pvesr', 'run', '--id', job], check=True, timeout=300)
    wait_for(lambda: env.node('secondary') in current(identifier).get('replication', {}).get('targets', {}))
    task(api('create', base(get(identifier)) + '/migrate', target=env.node('secondary'), restart=1, timeout=10, digest=current(identifier)['digest']))
    assert get(identifier)['node'] == env.node('secondary') and command(identifier, 'cat', '/preserved') == 'retained'
    jobs = api('get', '/cluster/replication')
    assert next(row for row in jobs if row['id'] == job)['target'] == env.node('primary')
    print('PASS migration switches replication target with guest ownership', flush=True)
    remote(env.node('secondary'), ['pvesr', 'run', '--id', job], check=True, timeout=300)
    wait_for(lambda: env.node('primary') in current(identifier).get('replication', {}).get('targets', {}))
    sid = 'docker:' + str(get(identifier)['vmid'])
    api('create', '/cluster/ha/resources', sid=sid, state='started')
    saved['ha'] = sid; STATE.write_text(json.dumps(saved))
    wait_for(lambda: current(identifier)['status'] == 'running', timeout=180)
    task(api('create', base(get(identifier)) + '/migrate', target=env.node('primary'), digest=current(identifier)['digest']))
    wait_for(lambda: get(identifier)['node'] == env.node('primary') and current(identifier)['status'] == 'running', timeout=300)
    assert command(identifier, 'cat', '/preserved') == 'retained'
    assert fetch(env.address(236)).stdout.strip() == 'cluster-ok'
    print('PASS native HA relocation preserves data and service', flush=True)
    cleanup()


if __name__ == '__main__':
    cleanup() if '--cleanup' in sys.argv else run()
