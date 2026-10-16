#!/usr/bin/env python3
"""Exercise IPv6, VLAN and routing using an isolated bridge and client namespace.

Run on a test node. No management interface or main-table default route changes.
Fixtures stay recorded on failure; --cleanup removes only these fixtures.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_pools_hooks import api, task, wait_for
from live_firewall import remote

STATE = Path('/root/bd-endpoint-families.json')
BRIDGE = 'bd67bridge'
NS = 'bd67client'


def call(*args, check=True):
    return subprocess.run(list(args), text=True, capture_output=True, check=check)


def endpoint(identifier):
    return f'/nodes/{w.current_node()}/docker/{identifier}'


def info(identifier):
    return api('get', endpoint(identifier) + '/management')


def net(identifier, **values):
    task(api('set', endpoint(identifier) + '/network', digest=info(identifier)['digest'], **values))


def power(identifier, action):
    task(api('create', endpoint(identifier) + '/action', action=action, timeout=10))


def fetch(address, port=8080):
    url = 'http://' + ('[' + address + ']' if ':' in address else address) + ':' + str(port) + '/'
    return call('ip', 'netns', 'exec', NS, 'curl', '--noproxy', '*', '-fsS', '--max-time', '3', url, check=False)


def cleanup():
    saved = json.loads(STATE.read_text())
    for identifier in saved['guests']:
        row = next((r for r in w.summaries() if r['id'] == identifier), None)
        if row:
            assert row['node'] == w.current_node() and row['name'] == saved['name']
            power(identifier, 'force-stop')
            task(api('delete', endpoint(identifier), **{'destroy-owned-data': 1}))
    call('ip', 'netns', 'del', NS, check=False)
    call('ip', 'link', 'del', 'bd67host', check=False)
    call('ip', 'link', 'del', BRIDGE, check=False)
    if 'cluster_options' in saved:
        before = saved['cluster_options']
        keys = ('enable', 'policy_in', 'policy_out')
        values = {key: before[key] for key in keys if key in before}
        missing = [key for key in keys if key not in before]
        if missing:
            values['delete'] = ','.join(missing)
        api('set', '/cluster/firewall/options', **values)
    elif 'cluster_enabled' in saved:
        api('set', '/cluster/firewall/options', enable=saved['cluster_enabled'])
    STATE.unlink()


def run():
    assert not STATE.exists()
    assert call('ip', 'link', 'show', BRIDGE, check=False).returncode != 0
    original_routes = json.loads(call('ip', '-j', 'route', 'show', 'default').stdout)
    name = 'endpoint-families-' + uuid.uuid4().hex[:8]
    saved = {'name': name, 'guests': [], 'cluster_options': api('get', '/cluster/firewall/options')}; STATE.write_text(json.dumps(saved))
    call('ip', 'link', 'add', BRIDGE, 'type', 'bridge', 'vlan_filtering', '1')
    call('ip', 'link', 'set', BRIDGE, 'up')
    call('ip', 'address', 'add', '172.31.67.1/24', 'dev', BRIDGE)
    call('ip', 'netns', 'add', NS)
    call('ip', 'link', 'add', 'bd67host', 'type', 'veth', 'peer', 'name', 'bd67peer')
    call('ip', 'link', 'set', 'bd67host', 'master', BRIDGE)
    call('ip', 'link', 'set', 'bd67host', 'up')
    call('ip', 'link', 'set', 'bd67peer', 'netns', NS)
    call('bridge', 'vlan', 'add', 'dev', BRIDGE, 'vid', '67', 'self')
    call('bridge', 'vlan', 'add', 'dev', 'bd67host', 'vid', '67')
    call('ip', '-n', NS, 'link', 'set', 'lo', 'up')
    call('ip', '-n', NS, 'link', 'set', 'bd67peer', 'up')
    call('ip', '-n', NS, 'link', 'add', 'link', 'bd67peer', 'name', 'tag67', 'type', 'vlan', 'id', '67')
    call('ip', '-n', NS, 'link', 'set', 'tag67', 'up')
    call('ip', '-n', NS, 'address', 'add', '172.31.68.2/24', 'dev', 'tag67')
    call('ip', '-n', NS, '-6', 'address', 'add', 'fd67:bd::2/64', 'dev', 'tag67')
    deployment = {'name': name, 'mode': 'compose', 'bridge': BRIDGE, 'cidr': '172.31.67.10/24',
        'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo endpoint-ok >/www/index.html; exec httpd -f -p 8080 -h /www"]\n    ports: ["8080:8080"]\n'}
    task(api('create', f'/nodes/{w.current_node()}/docker', deployment=json.dumps(deployment), start=1))
    identifier = next(row['id'] for row in w.summaries() if row['name'] == name)
    saved['guests'].append(identifier); STATE.write_text(json.dumps(saved))
    power(identifier, 'force-stop')
    net(identifier, net='net1', bridge=BRIDGE, tag=67, cidr='172.31.68.10/24', gw='172.31.68.2', ipv6mode='static', cidr6='fd67:bd::10/64', gw6='fd67:bd::2', ports='app:8080/tcp')
    power(identifier, 'start')
    wait_for(lambda: fetch('172.31.68.10').stdout.strip() == 'endpoint-ok')
    wait_for(lambda: fetch('fd67:bd::10').stdout.strip() == 'endpoint-ok')
    assert json.loads(call('ip', '-j', 'route', 'show', 'default').stdout) == original_routes
    print('PASS tagged IPv4 and IPv6 TCP traffic; node management routes unchanged', flush=True)
    api('set', '/cluster/firewall/options', enable=1, policy_in='ACCEPT', policy_out='ACCEPT')
    api('set', endpoint(identifier) + '/firewall/options', enable=1, policy_in='DROP', policy_out='ACCEPT')
    net(identifier, net='net1', firewall=1)
    power(identifier, 'reboot')
    assert fetch('172.31.68.10').returncode != 0 and fetch('fd67:bd::10').returncode != 0
    api('create', endpoint(identifier) + '/firewall/rules', type='in', action='ACCEPT', proto='tcp', dport='8080', source='fd67:bd::2', enable=1)
    wait_for(lambda: fetch('fd67:bd::10').stdout.strip() == 'endpoint-ok')
    assert fetch('172.31.68.10').returncode != 0
    api('create', endpoint(identifier) + '/firewall/rules', type='in', action='ACCEPT', proto='tcp', dport='8080', source='172.31.68.2', enable=1)
    wait_for(lambda: fetch('172.31.68.10').stdout.strip() == 'endpoint-ok')
    print('PASS native firewall enforces IPv4 and IPv6 source rules independently', flush=True)
    net(identifier, net='net1', link_down=1)
    power(identifier, 'reboot')
    assert fetch('172.31.68.10').returncode != 0 and fetch('fd67:bd::10').returncode != 0
    print('PASS Disconnect drops both address families', flush=True)
    cleanup()
    print('PASS isolated topology and DCT removed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--cleanup', action='store_true'); args = parser.parse_args()
    cleanup() if args.cleanup else run()
