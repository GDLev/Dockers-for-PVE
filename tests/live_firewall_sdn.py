#!/usr/bin/env python3
"""Exercise a disposable native SDN VNet on pve3, including SDN.Use."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
from live_pools_hooks import api, task
from live_firewall import remote
from live_permissions import http

path = Path('/root/dct-firewall-sdn.json')
state = json.loads(path.read_text()) if path.exists() else {'zone': 'fwzone', 'vnet': 'fwvnet', 'name': 'fwtest-sdn', 'node': 'pve3'}
if not path.exists():
    assert not any(i['zone'] == state['zone'] for i in api('get', '/cluster/sdn/zones'))
    assert not any(i['vnet'] == state['vnet'] for i in api('get', '/cluster/sdn/vnets'))
    assert not any(i['name'] == state['name'] for i in worker.summaries())
    assert not any(i['userid'] == 'fwtest-sdn@pve' for i in api('get', '/access/users'))
    assert not any(i['roleid'] == 'FWTestSDN' for i in api('get', '/access/roles'))
path.write_text(json.dumps(state)); path.chmod(0o600)
zone, vnet, node = state['zone'], state['vnet'], state['node']

def apply():
    result = api('set', '/cluster/sdn')
    if isinstance(result, str) and result.startswith('UPID:'): task(result)

def cleanup():
    for info in worker.summaries():
        if info['name'] == state['name']:
            base = f'/nodes/{info["node"]}/docker/{info["id"]}'
            task(api('create', base + '/action', action='force-stop', timeout=10))
            task(api('delete', base))
    for user in api('get', '/access/users'):
        if user['userid'] == 'fwtest-sdn@pve': api('delete', '/access/users/fwtest-sdn@pve')
    for role in api('get', '/access/roles'):
        if role['roleid'] == 'FWTestSDN': api('delete', '/access/roles/FWTestSDN')
    if any(i['vnet'] == vnet for i in api('get', '/cluster/sdn/vnets')):
        for subnet in api('get', '/cluster/sdn/vnets/' + vnet + '/subnets'):
            api('delete', '/cluster/sdn/vnets/' + vnet + '/subnets/' + subnet['subnet'])
        api('delete', '/cluster/sdn/vnets/' + vnet)
    if any(i['zone'] == zone for i in api('get', '/cluster/sdn/zones')):
        api('delete', '/cluster/sdn/zones/' + zone)
    apply(); path.unlink(); print('PASS SDN cleanup', flush=True)

if '--cleanup' in sys.argv:
    cleanup(); sys.exit()
if '--ipam' not in sys.argv:
    # These fixed IDs must not predate the fixture manifest.
    if not any(i['zone'] == zone for i in api('get', '/cluster/sdn/zones')):
        api('create', '/cluster/sdn/zones', zone=zone, type='simple', nodes=node)
    if not any(i['vnet'] == vnet for i in api('get', '/cluster/sdn/vnets')):
        api('create', '/cluster/sdn/vnets', vnet=vnet, zone=zone)
    if not api('get', '/cluster/sdn/vnets/' + vnet + '/subnets'):
        api('create', '/cluster/sdn/vnets/' + vnet + '/subnets', type='subnet', subnet='10.88.99.0/24', gateway='10.88.99.1', snat=1)
    apply()
    assert any(i['iface'] == vnet for i in api('get', '/nodes/' + node + '/network', type='any_bridge'))
    if any(i['userid'] == 'fwtest-sdn@pve' for i in api('get', '/access/users')):
        api('delete', '/access/users/fwtest-sdn@pve')
    if any(i['roleid'] == 'FWTestSDN' for i in api('get', '/access/roles')):
        api('delete', '/access/roles/FWTestSDN')
    api('create', '/access/roles', roleid='FWTestSDN', privs='VM.Allocate VM.Audit VM.PowerMgmt VM.Config.Network')
    api('create', '/access/users', userid='fwtest-sdn@pve', password=secrets.token_urlsafe(24))
    token = api('create', '/access/users/fwtest-sdn@pve/token/test', privsep=1)
    headers = {'Authorization': 'PVEAPIToken=fwtest-sdn@pve!test=' + token['value']}
    for principal in ({'users': 'fwtest-sdn@pve'}, {'tokens': 'fwtest-sdn@pve!test'}):
        api('set', '/access/acl', path='/vms', roles='FWTestSDN', **principal)
    source = 'services:\n  app:\n    image: busybox:1.37\n    command: [sh, -c, "mkdir -p /www; echo sdn-ok > /www/index.html; exec httpd -f -p 80 -h /www"]\n    ports: ["18080:80"]\n'
    deployment = dict(name=state['name'], mode='compose', compose=source, bridge=vnet, cidr='10.88.99.2/24', firewall=1)
    http('POST', '/nodes/' + node + '/docker', {'deployment': deployment}, headers=headers, expected=403)
    for principal in ({'users': 'fwtest-sdn@pve'}, {'tokens': 'fwtest-sdn@pve!test'}):
        api('set', '/access/acl', path='/sdn/zones/' + zone + '/' + vnet, roles='PVESDNUser', **principal)
    existing = next((i for i in worker.summaries() if i['name'] == state['name']), None)
    if not existing:
        task(http('POST', '/nodes/' + node + '/docker', {'deployment': deployment}, headers=headers))
    else:
        task(api('create', f'/nodes/{node}/docker/{existing["id"]}/action', action='start'))
    info = next(i for i in worker.summaries() if i['name'] == state['name'])
    base = f'/nodes/{node}/docker/{info["id"]}'
    api('set', base + '/firewall/options', enable=1)
    command = ['wget', '--tries=1', '-q', '-T', '3', '-O', '-', 'http://10.88.99.2:18080/']
    assert remote(node, command, timeout=10).returncode != 0
    api('create', base + '/firewall/rules', type='in', action='ACCEPT', proto='tcp', dport='18080', enable=1)
    assert 'sdn-ok' in remote(node, command, timeout=10).stdout
    print('PASS native SDN VNet, delegated SDN.Use and actual firewall enforcement', flush=True)
# Native PVE IPAM is configured only to verify the explicit DCT rejection.
for info in worker.summaries():
    if info['name'] == state['name']:
        base = f'/nodes/{info["node"]}/docker/{info["id"]}'
        task(api('create', base + '/action', action='force-stop', timeout=10))
        task(api('delete', base))
for subnet in api('get', '/cluster/sdn/vnets/' + vnet + '/subnets'):
    api('delete', '/cluster/sdn/vnets/' + vnet + '/subnets/' + subnet['subnet'])
api('set', '/cluster/sdn/zones/' + zone, ipam='pve')
apply()
code = 'import sys;sys.path.insert(0,"/usr/local/lib/betterdocker");import provision;provision.check_sdn_bridge("fwvnet")'
result = remote(node, ['python3', '-c', code])
assert result.returncode and 'IPAM' in result.stderr, result.stderr
print('PASS unsupported automatic IPAM rejected explicitly', flush=True)
cleanup()
