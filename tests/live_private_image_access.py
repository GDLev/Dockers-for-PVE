#!/usr/bin/env python3
"""A known private image ID must not substitute for permission to clone its guest."""
import live_environment as env
import json
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_security_policy import STATE
from live_pools_hooks import api, task
from live_permissions import http
from live_firewall import remote


def main():
    saved = json.loads(STATE.read_text())
    node = env.node('primary')
    source = next(r for r in w.summaries() if r['name'] == saved['prefix'] + '-clone-' + node)
    base = '/nodes/' + node + '/docker/' + source['id']
    task(api('create', base + '/action', action='start'))
    cid = api('get', base + '/management')['containers'][0]['id']
    remote(node, ['docker', 'exec', cid, 'sh', '-c', 'echo private-fixture >/private-proof'], check=True)
    task(api('create', base + '/action', action='force-stop'))
    task(api('set', base + '/security-policy', mode='hardened', digest=api('get', base + '/management')['digest']))
    cid = api('get', base + '/management')['containers'][0]['id']
    image = json.loads(remote(node, ['docker', 'inspect', cid], check=True).stdout)[0]['Image']
    user = next(u for u in saved['users'] if f"{env.node('primary')}@" in u)
    role = next(r for r in saved['roles'] if r.endswith(env.node('primary')))
    token_name = 'private-image-' + uuid.uuid4().hex[:8]
    token = api('create', '/access/users/' + user + '/token/' + token_name, privsep=1)
    vmid = int(api('get', '/cluster/nextid'))
    for path in ('/vms/' + str(vmid), '/sdn/zones/localnetwork/vmbr0'):
        for principal in ({'tokens': user + '!' + token_name}, {'users': user}):
            api('set', '/access/acl', path=path, roles=role, **principal)
    headers = {'Authorization': 'PVEAPIToken=' + user + '!' + token_name + '=' + token['value']}
    http('GET', base + '/management', headers=headers, expected=403)
    name = saved['prefix'] + '-foreign-image'
    result = http('POST', '/nodes/' + node + '/docker', {'deployment': {'vmid': vmid, 'name': name, 'mode': 'compose', 'compose': 'services:\n  app:\n    image: ' + image + '\n    pull_policy: never\n    command: [sleep, "86400"]\n', 'bridge': 'vmbr0', 'ipv4mode': 'none'}}, headers=headers, expected=(200,400,500))
    if result is None:
        print('PASS unauthorized private image rejected before task creation', flush=True); return
    try: task(result)
    except RuntimeError as error:
        assert 'private' in str(error).lower() or 'image' in str(error).lower(), str(error)
        print('PASS unauthorized private image rejected in worker', flush=True); return
    created = next(r for r in w.summaries() if r['name'] == name)
    data = http('GET', '/nodes/' + node + '/docker/' + created['id'] + '/management', headers=headers)
    assert data['capabilities']['console']
    proof = remote(node, ['docker', 'exec', data['containers'][0]['id'], 'cat', '/private-proof'], check=True).stdout.strip()
    assert proof == 'private-fixture'
    raise AssertionError('Known foreign image exposed a private writable file without source guest privileges')


if __name__ == '__main__': main()
