#!/usr/bin/env python3
"""Verify Stage 1 authorization on disposable rootful and rootless DCTs.

Run on pve1 after deployment. A manifest supports explicit cleanup after interruption.
No extension code is installed on PBS.
"""
import argparse
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time
import urllib.parse
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_permissions import http
from live_pools_hooks import api, task
from live_firewall import remote

STATE = Path('/root/bd-security-stage1.json')


def cleanup():
    saved = json.loads(STATE.read_text())
    for identifier in reversed(saved['guests']):
        info = next((row for row in w.summaries() if row['id'] == identifier), None)
        if info:
            assert info['name'].startswith(saved['prefix'])
            base = '/nodes/' + info['node'] + '/docker/' + identifier
            task(api('create', base + '/action', action='force-stop'))
            task(api('delete', base, **{'destroy-owned-data': 1}))
    for user in saved['users']:
        api('delete', '/access/users/' + user)
    for role in saved['roles']:
        api('delete', '/access/roles/' + role)
    STATE.unlink()
    print('PASS owned fixtures, users, tokens and roles removed', flush=True)


def run():
    assert not STATE.exists(), 'Inspect or clean previous fixtures before retrying'
    before = {int(row['vmid']) for row in api('get', '/cluster/resources', type='vm')}
    suffix = uuid.uuid4().hex[:8]
    saved = {'prefix': 'security-s1-' + suffix, 'guests': [], 'users': [], 'roles': []}
    STATE.write_text(json.dumps(saved)); STATE.chmod(0o600)
    def save(): STATE.write_text(json.dumps(saved))
    role = 'BDS1' + suffix
    audit = role + 'Audit'
    allocate = role + 'Allocate'
    privileges = 'VM.Audit VM.Console VM.PowerMgmt VM.Config.CPU VM.Config.Memory VM.Config.Network VM.Config.Disk VM.Config.Options VM.Allocate VM.Clone VM.Migrate VM.Snapshot VM.Snapshot.Rollback VM.Backup'
    try:
        for name, rights in ((role, privileges), (audit, 'VM.Audit'), (allocate, 'VM.Allocate')):
            api('create', '/access/roles', roleid=name, privs=rights); saved['roles'].append(name); save()
        fixtures = []
        for index, node in enumerate(('pve1', 'pve3')):
            secret = 'private-environment-' + secrets.token_hex(12)
            compose = 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sleep, "86400"]\n    environment: {SENTINEL: "${SENTINEL}"}\n    volumes: ["./data:/data", "/anonymous"]\n'
            name = saved['prefix'] + '-' + str(index)
            create_task = api('create', '/nodes/' + node + '/docker', deployment={'name': name, 'mode': 'compose', 'compose': compose,
                'environment': 'SENTINEL=' + secret, 'bridge': 'vmbr0', 'ipv4mode': 'none'}, start=1)
            task(create_task)
            task_log = api('get', '/nodes/' + node + '/tasks/' + create_task + '/log')
            assert secret not in json.dumps(task_log)
            diagnostics = api('get', '/nodes/' + node + '/docker', diagnostics=1)
            assert secret not in json.dumps(diagnostics)
            info = next(row for row in w.summaries() if row['name'] == name)
            saved['guests'].append(info['id']); save()
            base = '/nodes/' + node + '/docker/' + info['id']
            user = 'bds1-' + suffix + '-' + str(index) + '@pve'
            password = secrets.token_urlsafe(24)
            api('create', '/access/users', userid=user, password=password); saved['users'].append(user); save()
            api('set', '/access/acl', path='/vms/' + str(info['vmid']), users=user, roles=role)
            token = api('create', '/access/users/' + user + '/token/editor', privsep=1)
            headers = {'Authorization': 'PVEAPIToken=' + user + '!editor=' + token['value']}
            http('GET', base + '/management', headers=headers, expected=403)
            api('set', '/access/acl', path='/vms/' + str(info['vmid']), tokens=user + '!editor', roles=role)
            expired = api('create', '/access/users/' + user + '/token/expired', privsep=1, expire=int(time.time()) - 60)
            api('set', '/access/acl', path='/vms/' + str(info['vmid']), tokens=user + '!expired', roles=role)
            http('GET', base + '/management', headers={'Authorization': 'PVEAPIToken=' + user + '!expired=' + expired['value']}, expected=(401,403))
            audit_token = api('create', '/access/users/' + user + '/token/audit', privsep=1)
            audit_headers = {'Authorization': 'PVEAPIToken=' + user + '!audit=' + audit_token['value']}
            api('set', '/access/acl', path='/vms/' + str(info['vmid']), tokens=user + '!audit', roles=audit)
            http('GET', '/nodes/' + node + '/docker', {'diagnostics': 1}, headers=audit_headers, expected=403)
            public = http('GET', base + '/management', headers=audit_headers)
            assert secret not in json.dumps(public) and 'deployment' not in public
            for endpoint in ('', '/logs', '/termproxy'):
                if endpoint == '/termproxy':
                    http('POST', base + endpoint, {'container': '0' * 64}, headers=audit_headers, expected=403)
                else:
                    http('GET', base + endpoint, headers=audit_headers, expected=403)
            management = http('GET', base + '/management', headers=headers)
            cid = management['containers'][0]['id']
            remote(node, ['docker', 'exec', cid, 'sh', '-c', 'echo retained >/marker; echo anonymous >/anonymous/value'], check=True)
            remote(node, ['dct', 'status', str(info['vmid'])], check=True)
            cookie = http('POST', '/access/ticket', {'username': user, 'password': password})
            cookie = {'Cookie': 'PVEAuthCookie=' + cookie['ticket'], 'CSRFPreventionToken': cookie['CSRFPreventionToken']}
            fixtures.append(dict(info, base=base, user=user, headers=headers, audit=audit_headers, cookie=cookie,
                cid=cid, secret=secret, source=compose))
            print('PASS token intersection, audit/task/diagnostic secrecy and native CLI on ' + node, flush=True)
        for fixture, other in ((fixtures[0], fixtures[1]), (fixtures[1], fixtures[0])):
            base, headers = fixture['base'], fixture['headers']
            http('GET', other['base'] + '/management', headers=headers, expected=403)
            http('POST', other['base'] + '/action', {'action': 'force-stop'}, headers=headers, expected=403)
            http('POST', base + '/termproxy', {'container': other['cid']}, headers=headers, expected=(403,500))
            # Try unsafe source before Compose has a chance to load host files.
            probe_vmid = next(value for value in range(970, 999) if value not in before and value not in {row['vmid'] for row in fixtures})
            for principal in ({'users': fixture['user']}, {'tokens': fixture['user'] + '!editor'}):
                api('set', '/access/acl', path='/vms/' + str(probe_vmid), roles=allocate, **principal)
            for option in ('privileged: true', 'pid: host', 'devices: [/dev/sda]',
                           'env_file: /etc/shadow', 'volumes: ["/var/run/docker.sock:/socket"]'):
                deployment = {'vmid': probe_vmid, 'name': 'rejected', 'mode': 'compose', 'bridge': 'vmbr0', 'ipv4mode': 'none',
                    'compose': 'services:\n  app:\n    image: busybox:1.37\n    ' + option + '\n', 'environment': 'SENTINEL=' + fixture['secret']}
                result = http('PUT', '/nodes/' + fixture['node'] + '/docker', {'deployment': deployment}, headers=headers, expected=(403,500))
                assert fixture['secret'] not in json.dumps(result)
            # Simulate a queued task whose authorization refers to an old guest ID.
            context = {'id': fixture['id'], 'vmid': fixture['vmid'] + 1000, 'node': fixture['node']}
            code = 'import os,sys,json;sys.path.insert(0,"/usr/local/lib/betterdocker");from dctlib import projects as w;os.environ["BETTERDOCKER_REQUEST_CONTEXT"]=sys.argv[2]\nwith w.operation(sys.argv[1],"update"): raise AssertionError("stale request accepted")'
            result = remote(fixture['node'], ['python3', '-c', code, fixture['id'], json.dumps(context)])
            assert result.returncode and 'authorization' in result.stderr
            assert remote(fixture['node'], ['docker', 'exec', fixture['cid'], 'cat', '/marker'], check=True).stdout.strip() == 'retained'
            # An issued ticket must not override a subsequently removed guest ACL.
            terminal = http('POST', base + '/termproxy', {'container': fixture['cid']}, headers=fixture['cookie'])
            api('set', '/access/acl', path='/vms/' + str(fixture['vmid']), users=fixture['user'], roles=role, delete=1)
            http('GET', base + '/vncwebsocket', {'port': terminal['port'], 'vncticket': terminal['ticket']}, headers=fixture['cookie'], expected=403)
            http('POST', base + '/action', {'action': 'force-stop'}, headers=headers, expected=403)
            api('set', '/access/acl', path='/vms/' + str(fixture['vmid']), users=fixture['user'], roles=role)
            api('delete', '/access/users/' + fixture['user'] + '/token/editor')
            http('GET', base + '/management', headers=headers, expected=(401,403))
            api('set', '/access/users/' + fixture['user'], enable=0)
            http('GET', base + '/management', headers=fixture['cookie'], expected=(401,403))
            api('delete', '/access/users/' + fixture['user'])
            saved['users'].remove(fixture['user']); save()
            http('GET', base + '/management', headers=fixture['cookie'], expected=(401,403))
            http('GET', base + '/management', headers=fixture['audit'], expected=(401,403))
            assert remote(fixture['node'], ['docker', 'exec', fixture['cid'], 'cat', '/anonymous/value'], check=True).stdout.strip() == 'anonymous'
            print('PASS cross-guest denial, unsafe Compose, stale worker, revoked ticket/token/user on ' + fixture['node'], flush=True)
    finally:
        cleanup()
    assert {int(row['vmid']) for row in api('get', '/cluster/resources', type='vm')} == before
    print('PASS Stage 1 acceptance; original guest inventory retained', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--cleanup', action='store_true'); args = parser.parse_args()
    cleanup() if args.cleanup else run()
