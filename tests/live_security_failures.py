#!/usr/bin/env python3
"""Refuse unavailable profiles and daemon errors without touching unrelated guests."""
import json
import subprocess
import sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_pools_hooks import api, task
from live_security_stage23 import STATE, SOURCE, check_kernel
from live_permissions import http
from live_firewall import remote


def fails(upid, message):
    try: task(upid)
    except RuntimeError as error:
        assert message in str(error), str(error)
        return
    raise AssertionError('Forbidden operation succeeded')


def main():
    saved = json.loads(STATE.read_text()); assert saved.get('complete')
    for node in ('pve1', 'pve3'):
        info = next(r for r in w.summaries() if r['name'] == saved['prefix'] + '-' + node)
        code = '''import sys,json,dataclasses
from unittest.mock import patch
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as w
import runtime
p=w.ROOT/sys.argv[1]/'storage-transaction.json'
assert not p.exists()
w.write_json(p,{'operation':'grow','before':{}})
before=p.read_bytes();actual=runtime.load()
try:
 with patch.object(runtime,'load',return_value=dataclasses.replace(actual,socket='/run/betterdocker-acceptance-missing.sock')):
  try: w.run(['docker','volume','rm','bd-security-absent'],check=False,capture_output=True)
  except ValueError as e: assert 'recovery state was retained' in str(e)
  else: raise AssertionError('unavailable daemon treated as deletion')
 with patch.object(runtime,'service',return_value=__import__('subprocess').CompletedProcess([],3,'','unavailable')):
  try:w.recover_storage_transaction(sys.argv[1])
  except ValueError as e:assert 'storage recovery was not performed' in str(e)
  else:raise AssertionError('unavailable daemon treated as stopped')
 assert p.read_bytes()==before
finally:p.unlink()
'''
        remote(node, ['python3', '-c', code, info['id']], check=True)
        print('PASS ' + node + ' failed daemon connection and recovery preserve signed journal', flush=True)
    owned = [r for r in w.summaries() if r['node'] == 'pve1' and r['name'].startswith(saved['prefix'])]
    for info in owned:
        task(api('create', '/nodes/pve1/docker/' + info['id'] + '/action', action='force-stop'))
    rows = remote('pve1', ['docker', 'ps', '-aq'], check=True).stdout.split()
    inspected = json.loads(remote('pve1', ['docker', 'inspect', *rows], check=True).stdout) if rows else []
    assert all(r.get('AppArmorProfile') != 'betterdocker-default' or r['Config']['Labels'].get('betterdocker.id') in {i['id'] for i in owned} for r in inspected)
    user = next(u for u in saved['users'] if 'pve1@' in u)
    role = next(r for r in saved['roles'] if r.endswith('pve1'))
    token = api('create', '/access/users/' + user + '/token/profile-check', privsep=1)
    vmid = int(api('get', '/cluster/nextid'))
    for path in ('/vms/' + str(vmid), '/sdn/zones/localnetwork/vmbr0'):
        api('set', '/access/acl', path=path, roles=role, tokens=user + '!profile-check')
        api('set', '/access/acl', path=path, roles=role, users=user)
    headers = {'Authorization': 'PVEAPIToken=' + user + '!profile-check=' + token['value']}
    source = next(r for r in owned if r['name'] == saved['prefix'] + '-pve1')
    profile = '/etc/apparmor.d/betterdocker'
    subprocess.run(['apparmor_parser', '-R', profile], check=True)
    try:
        fails(api('create', '/nodes/pve1/docker/' + source['id'] + '/action', action='start'), 'enforcing AppArmor')
        result = http('POST', '/nodes/pve1/docker', {'start': 0, 'deployment': {'vmid': vmid, 'name': saved['prefix'] + '-missing-profile', 'mode': 'compose', 'compose': SOURCE, 'bridge': 'vmbr0', 'ipv4mode': 'none'}}, headers=headers, expected=(200,400,500))
        if result is not None: fails(result, 'enforcing AppArmor')
        assert not any(r['name'] == saved['prefix'] + '-missing-profile' for r in w.summaries())
    finally:
        subprocess.run(['apparmor_parser', '-r', profile], check=True)
    task(api('create', '/nodes/pve1/docker/' + source['id'] + '/action', action='start')); check_kernel(source)
    task(api('create', '/nodes/pve1/docker/' + source['id'] + '/action', action='force-stop'))
    print('PASS missing enforcing AppArmor refuses Start and delegated creation, reload permits Start', flush=True)
    saved['failures_complete'] = True; STATE.write_text(json.dumps(saved))


if __name__ == '__main__': main()
