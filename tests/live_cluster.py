#!/usr/bin/env python3
'Disposable migration and endpoint checks on the three-node test PVE cluster.\n\nRun on <node> after deployment. The manifest records only test guests.\nUse --cleanup to remove them after a failed or interrupted run.\n'
import live_environment as env
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_pools_hooks import api, task, wait_for
from live_firewall import remote

MANIFEST = Path('/root/bd-stage67-fixture.json')


def base(info):
    return f'/nodes/{info["node"]}/docker/{info["id"]}'


def get(identifier):
    return next(row for row in w.summaries() if row['id'] == identifier)


def current(identifier):
    info = get(identifier)
    return api('get', base(info) + '/management')


def command(identifier, *args):
    info = get(identifier)
    cid = current(identifier)['containers'][0]['id']
    return remote(info['node'], ['docker', 'exec', cid, *args], check=True).stdout.strip()


def power(identifier, action):
    task(api('create', base(get(identifier)) + '/action', action=action, timeout=10))


def network(identifier, **values):
    task(api('set', base(get(identifier)) + '/network', digest=current(identifier)['digest'], **values))


def fetch(address, port=8080, path='value', timeout=3):
    url = 'http://' + ('[' + address + ']' if ':' in address else address) + f':{port}/{path}'
    return subprocess.run(['curl', '--noproxy', '*', '-fsS', '--max-time', str(timeout), url], text=True, capture_output=True)


def cleanup():
    saved = json.loads(MANIFEST.read_text())
    for identifier in saved['guests']:
        info = next((row for row in w.summaries() if row['id'] == identifier), None)
        if not info:
            continue
        assert info['name'].startswith(saved['prefix'])
        power(identifier, 'force-stop')
        for snapshot in api('get', base(info) + '/snapshots'):
            if snapshot['name'] != 'current':
                task(api('create', base(info) + '/snapshot', action='delete', snapname=snapshot['name']))
        task(api('delete', base(info), **{'destroy-owned-data': 1}))
    MANIFEST.unlink()
    print('PASS disposable fixtures removed', flush=True)


def create(saved, node, mode='static', cidr=f'{env.address(229)}/{env.prefix()}'):
    name = saved['prefix'] + '-' + str(len(saved['guests']))
    deployment = {'vmid': int(api('get', '/cluster/nextid')), 'name': name, 'mode': 'compose', 'bridge': 'vmbr0',
        'ipv4mode': mode, 'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /data; test -e /data/value || echo initial > /data/value; exec httpd -f -p 8080 -h /data"]\n    ports: ["8080:8080"]\n    volumes: ["./data:/data"]\n'}
    if mode == 'static':
        deployment['cidr'] = cidr
    task(api('create', f'/nodes/{node}/docker', deployment=json.dumps(deployment), start=1))
    info = next(row for row in w.summaries() if row['name'] == name)
    saved['guests'].append(info['id']); MANIFEST.write_text(json.dumps(saved))
    wait_for(lambda: fetch(get(info['id'])['address']).returncode == 0)
    return info['id']


def run():
    assert w.current_node() == env.node('primary')
    assert not MANIFEST.exists(), 'Clean up the earlier fixture first'
    saved = {'prefix': 'stage67-' + uuid.uuid4().hex[:8], 'guests': []}
    MANIFEST.write_text(json.dumps(saved))
    identifier = create(saved, env.node('primary'))
    power(identifier, 'force-stop')
    task(api('set', base(get(identifier)) + '/storage-limit', size_mib=512, storage='local-lvm', digest=current(identifier)['digest']))
    power(identifier, 'start')
    command(identifier, 'sh', '-c', 'echo layer-before >/preserved; echo before >/data/value')
    power(identifier, 'force-stop')
    task(api('create', base(get(identifier)) + '/snapshot', action='create', snapname='before'))
    power(identifier, 'start')
    command(identifier, 'sh', '-c', 'echo layer-after >/preserved; echo after >/data/value')
    power(identifier, 'force-stop')
    time.sleep(1)
    task(api('create', base(get(identifier)) + '/snapshot', action='create', snapname='after'))
    power(identifier, 'start')
    task(api('create', base(get(identifier)) + '/migrate', target=env.node('secondary'), restart=1, timeout=10, storage='local-lvm', digest=current(identifier)['digest']))
    assert get(identifier)['node'] == env.node('secondary')
    wait_for(lambda: fetch(get(identifier)['address']).stdout.strip() == 'after')
    assert command(identifier, 'cat', '/preserved') == 'layer-after'
    assert {row['name'] for row in api('get', base(get(identifier)) + '/snapshots')} >= {'before', 'after'}
    power(identifier, 'force-stop')
    task(api('create', base(get(identifier)) + '/snapshot', action='restore', snapname='before'))
    power(identifier, 'start')
    assert command(identifier, 'cat', '/preserved') == 'layer-before'
    assert command(identifier, 'cat', '/data/value') == 'before'
    print('PASS running migration, native snapshot history and writable-layer rollback', flush=True)
    power(identifier, 'force-stop')
    network(identifier, net='net1', bridge='vmbr0', cidr=f'{env.address(230)}/{env.prefix()}', ports='app:8080/tcp')
    power(identifier, 'start')
    wait_for(lambda: fetch(env.address(230)).stdout.strip() == 'before')
    assert command(identifier, 'cat', '/preserved') == 'layer-before'
    assert fetch(get(identifier)['address']).returncode != 0
    network(identifier, net='net1', link_down=1)
    assert current(identifier)['pending_network']['interfaces']['net1']['link_down'] == 1
    assert fetch(env.address(230)).returncode == 0
    power(identifier, 'reboot')
    assert fetch(env.address(230)).returncode != 0
    network(identifier, net='net1', link_down=0)
    power(identifier, 'reboot')
    wait_for(lambda: fetch(env.address(230)).returncode == 0)
    assert command(identifier, 'cat', '/preserved') == 'layer-before'
    print('PASS independent port assignment, pending Disconnect and preserved filesystem', flush=True)
    command(identifier, 'sh', '-c', 'dd if=/dev/zero of=/data/big bs=1M count=8 >/dev/null 2>&1')
    network(identifier, net='net1', rate=1)
    power(identifier, 'reboot')
    started = time.monotonic()
    result = fetch(env.address(230), path='big', timeout=25)
    elapsed = time.monotonic() - started
    assert result.returncode == 0 and len(result.stdout) == 8 * 1048576 and elapsed >= 3, (result.returncode, elapsed)
    print('PASS actual byte-rate limit', flush=True)
    dhcp = create(saved, env.node('primary'), mode='dhcp')
    command(dhcp, 'sh', '-c', 'echo migrated >/preserved')
    task(api('create', base(get(dhcp)) + '/migrate', target=env.node('secondary'), restart=1, timeout=10, digest=current(dhcp)['digest']))
    assert get(dhcp)['node'] == env.node('secondary')
    wait_for(lambda: fetch(get(dhcp)['address']).stdout.strip() == 'initial')
    assert command(dhcp, 'cat', '/preserved') == 'migrated'
    print('PASS DHCP migration and preserved writable layer', flush=True)
    cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--cleanup', action='store_true'); args = parser.parse_args()
    cleanup() if args.cleanup else run()
