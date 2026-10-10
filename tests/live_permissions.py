#!/usr/bin/env python3
"""Disposable PVE acceptance tests for DCT delegation. Run on a test node only.

Creates test users/tokens and three Docker guests, then removes those resources.
Does not install anything on PBS or grant node console privileges.
"""
import argparse
import base64
import os
import socket
import struct
import json
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from live_acceptance import api, task

TLS = ssl._create_unverified_context()


def http(method, path, values=None, headers=None, expected=200, origin="127.0.0.1"):
    encoded = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in (values or {}).items()}).encode()
    url = 'https://' + origin + ':8006/api2/json' + path
    if method == 'GET' and encoded:
        url += '?' + encoded.decode()
    req = urllib.request.Request(url, data=encoded if method != 'GET' else None,
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, context=TLS, timeout=90) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    assert status in (expected if isinstance(expected, tuple) else (expected,)), (method, path, status, body.decode()[:800])
    return json.loads(body).get('data') if body else None



def expired_console_ticket(user, vmid):
    # Sign an expired native ticket in this child process, without changing the
    # node clock or any PVE code. The API must reject a valid but stale signature.
    code = "BEGIN { *CORE::GLOBAL::time = sub { CORE::time() - 600 }; } use PVE::AccessControl; print PVE::AccessControl::assemble_vnc_ticket($ARGV[0], $ARGV[1]);"
    return subprocess.check_output(['perl', '-e', code, user, '/vms/' + str(vmid)], text=True).strip()


def terminal_session(base, terminal, cookie, container, origin="127.0.0.1", hostname=None):
    path = '/api2/json' + base + '/vncwebsocket?' + urllib.parse.urlencode({'port': terminal['port'], 'vncticket': terminal['ticket']})
    connection = TLS.wrap_socket(socket.create_connection((origin, 8006), timeout=20), server_hostname='localhost')
    connection.settimeout(20)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (f'GET {path} HTTP/1.1\r\nHost: localhost:8006\r\nUpgrade: websocket\r\n'
               f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
               f'Cookie: {cookie["Cookie"]}\r\n\r\n')
    connection.sendall(request.encode())
    def exact(size):
        result = b''
        while len(result) < size:
            part = connection.recv(size - len(result))
            if not part:
                raise RuntimeError('Terminal connection closed')
            result += part
        return result
    response = b''
    while not response.endswith(b'\r\n\r\n'):
        response += exact(1)
    assert response.startswith(b'HTTP/1.1 101'), response.split(b'\r\n', 1)[0]
    def send(value):
        payload = value.encode()
        mask = os.urandom(4)
        length = bytes([0x80 | len(payload)]) if len(payload) < 126 else bytes([0x80 | 126]) + struct.pack('!H', len(payload))
        connection.sendall(b'\x81' + length + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
    def receive():
        opcode, size = exact(2)
        size &= 127
        if size == 126:
            size = struct.unpack('!H', exact(2))[0]
        elif size == 127:
            size = struct.unpack('!Q', exact(8))[0]
        return exact(size)
    try:
        send(terminal['user'] + ':' + terminal['ticket'] + '\n')
        assert receive().startswith(b'OK')
        command = 'printf "BD-CONSOLE-RESULT:%s\\n" "$(cat /etc/hostname)"\n'
        send('0:' + str(len(command.encode())) + ':' + command)
        output = b''
        while ('BD-CONSOLE-RESULT:' + (hostname or container[:12])).encode() not in output:
            output += receive()
        send('0:5:exit\n')
    finally:
        connection.close()



def console_acceptance(args):
    """Exercise local and remote termproxy versions without repeating disk/PBS tests."""
    import shlex
    import transfer
    nonce = uuid.uuid4().hex[:8]
    user, role = 'bdconsole-' + nonce + '@pve', 'BDConsole' + nonce
    password = secrets.token_urlsafe(24)
    identifier = None
    registered_user = registered_role = False
    name = 'permissions-console-' + nonce
    compose = json.dumps({'services': {'app': {'image': 'busybox:1.37', 'hostname': name,
        'command': ['sh', '-c', "trap 'exit 0' TERM; while true; do sleep 1; done"]}}})
    try:
        task(api('create', f'/nodes/{args.target}/docker', deployment={'name': name,
            'mode': 'compose', 'compose': compose, 'bridge': args.bridge,
            'address': args.address, 'cidr': args.address + '/' + str(args.prefix)}))
        row = next(r for r in worker.summaries() if r['name'] == name)
        identifier = row['id']
        base = f'/nodes/{args.target}/docker/{identifier}'
        api('create', '/access/roles', roleid=role, privs='VM.Audit VM.Console VM.PowerMgmt VM.Allocate')
        registered_role = True
        api('create', '/access/users', userid=user, password=password)
        registered_user = True
        api('set', '/access/acl', path='/vms/' + str(row['vmid']), roles=role, users=user)
        session = http('POST', '/access/ticket', {'username': user, 'password': password})
        cookie = {'Cookie': 'PVEAuthCookie=' + session['ticket'], 'CSRFPreventionToken': session['CSRFPreventionToken']}
        data = http('GET', base + '/management', headers=cookie)
        assert data['capabilities']['console'] and not data['capabilities']['trusted']
        assert not data['capabilities']['configure'] and 'deployment' not in data
        cid = data['containers'][0]['id']
        for origin in ('127.0.0.1', args.target_address):
            terminal = http('POST', base + '/termproxy', {'container': cid}, headers=cookie, origin=origin)
            terminal_session(base, terminal, cookie, cid, origin=origin, hostname=name)
            print('OK guest-only console through ' + origin, flush=True)
        task(http('POST', base + '/action', {'action': 'shutdown'}, headers=cookie))
        data = http('GET', base + '/management', headers=cookie)
        task(http('POST', base + '/template', {'digest': data['digest']}, headers=cookie))
        assert http('GET', base + '/management', headers=cookie)['template']
        task(http('DELETE', base, headers=cookie))
        print('PASS remote console, destination termproxy, delegated template and removal', flush=True)
    finally:
        if identifier is None:
            retained = next((r for r in worker.summaries() if r['name'] == name), None)
            if retained:
                identifier = retained['id']
                base = f'/nodes/{args.target}/docker/{identifier}'
        if identifier:
            if any(r['id'] == identifier for r in worker.summaries()):
                task(api('create', base + '/action', action='force-stop'))
                task(api('delete', base))
            cleanup = "import sys,shutil;sys.path.insert(0,'/usr/local/lib/betterdocker');from dctlib import projects as w;shutil.rmtree(w.ROOT / " + repr(identifier) + ",ignore_errors=True);w.release_id(" + repr(identifier) + ")"
            subprocess.run(transfer.ssh_command(args.target) + [shlex.join(['python3', '-c', cleanup])], check=True)
        if registered_user:
            api('delete', '/access/users/' + user)
        if registered_role:
            api('delete', '/access/roles/' + role)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', required=True)
    parser.add_argument('--other-address', required=True)
    parser.add_argument('--unsafe-address', required=True)
    parser.add_argument('--console-only', action='store_true')
    parser.add_argument('--bridge', default='vmbr0')
    parser.add_argument('--prefix', type=int, default=20)
    parser.add_argument('--target', default='pve3')
    parser.add_argument('--target-address', default='192.0.2.40')
    parser.add_argument('--backup-storage', default='pbs1')
    parser.add_argument('--clone-address', default='192.0.2.219')
    parser.add_argument('--restore-address', default='192.0.2.218')
    parser.add_argument('--create-address', default='192.0.2.217')
    args = parser.parse_args()
    if args.console_only:
        return console_acceptance(args)
    node = worker.current_node()
    nonce = uuid.uuid4().hex[:8]
    roles, users, guests = [], [], []
    created = set()
    source = 'services:\n  app:\n    image: busybox:1.37\n    command: ["sh", "-c", "trap \'exit 0\' TERM INT; echo delegation-ready; while true; do sleep 1; done"]\n    volumes:\n      - ./data:/data\n'

    def deploy(label, address, compose=source):
        name = 'permissions-' + nonce + '-' + label
        data = dict(name=name, mode='compose', compose=compose, bridge=args.bridge,
                    address=address, cidr=address + '/' + str(args.prefix), environment='DELEGATION_SECRET=sentinel')
        upid = api('create', f'/nodes/{node}/docker', deployment=data)
        # Register ownership before waiting, so a failed task is cleaned up too.
        rows = [r for r in worker.summaries() if r['name'] == name]
        task(upid)
        rows = [r for r in worker.summaries() if r['name'] == name]
        assert len(rows) == 1
        guests.append(rows[0]['id'])
        return rows[0]

    try:
        safe = deploy('safe', args.address)
        other = deploy('other', args.other_address)
        unsafe = deploy('unsafe', args.unsafe_address, source.replace('    image:', '    privileged: true\n    image:'))
        base = f"/nodes/{node}/docker/{safe['id']}"
        otherbase = f"/nodes/{node}/docker/{other['id']}"
        unsafebase = f"/nodes/{node}/docker/{unsafe['id']}"
        headers = {}
        passwords = {}
        definitions = {
            'audit': 'VM.Audit',
            'console': 'VM.Audit VM.Console',
            'operator': 'VM.Audit VM.PowerMgmt',
            'editor': 'VM.Audit VM.Console VM.PowerMgmt VM.Config.CPU VM.Config.Memory VM.Config.Network VM.Config.Disk VM.Config.Options VM.Clone VM.Migrate VM.Allocate VM.Snapshot VM.Snapshot.Rollback VM.Backup',
        }
        for label, privs in definitions.items():
            role = 'BDPerm' + nonce + label
            user = 'bdperm-' + nonce + '-' + label + '@pve'
            password = secrets.token_urlsafe(24)
            api('create', '/access/roles', roleid=role, privs=privs)
            roles.append(role)
            api('create', '/access/users', userid=user, password=password)
            users.append(user)
            passwords[label] = (user, password)
            token = api('create', f'/access/users/{user}/token/test', privsep=1)
            for guest in (safe, unsafe):
                for principal in ({'users': user}, {'tokens': user + '!test'}):
                    api('set', '/access/acl', path='/vms/' + str(guest['vmid']), roles=role, **principal)
            headers[label] = {'Authorization': 'PVEAPIToken=' + user + '!test=' + token['value']}
        for label in definitions:
            data = http('GET', base + '/management', headers=headers[label])
            assert not data['capabilities']['trusted']
            assert ('deployment' in data) == (label == 'editor')
            http('GET', otherbase + '/management', headers=headers[label], expected=403)
            http('POST', f'/nodes/{node}/termproxy', headers=headers[label], expected=403)
        api('set', '/access/acl', path='/vms/' + str(safe['vmid']), users=users[0], roles=roles[1])
        assert not http('GET', base + '/management', headers=headers['audit'])['capabilities']['console']
        print('OK roles, token ACL intersection, cross-DCT and node-console denial', flush=True)

        for label in ('audit', 'operator'):
            http('GET', base + '/logs', headers=headers[label], expected=403)
        assert 'delegation-ready' in http('GET', base + '/logs', headers=headers['console'])
        assert not http('GET', base + '/management', headers=headers['console'])['capabilities']['configure']
        cid = worker.container_records(safe['id'])[0]['id']
        othercid = worker.container_records(other['id'])[0]['id']
        http('GET', base + '/logs', {'container': othercid}, headers=headers['console'], expected=500)
        print('OK console-only logs and container ownership', flush=True)

        unsafe_caps = http('GET', unsafebase + '/management', headers=headers['editor'])['capabilities']
        assert not any(unsafe_caps[k] for k in ('console', 'configure', 'disk', 'clone', 'migrate', 'remove'))
        http('GET', unsafebase + '/logs', headers=headers['editor'], expected=403)
        print('OK existing privileged DCT remains administrator-only', flush=True)

        user, password = passwords['console']
        session = http('POST', '/access/ticket', {'username': user, 'password': password})
        cookie = {'Cookie': 'PVEAuthCookie=' + session['ticket'], 'CSRFPreventionToken': session['CSRFPreventionToken']}
        terminal = http('POST', base + '/termproxy', {'container': cid}, headers=cookie)
        # Native tickets are bound to guest and user. Reject ticket reuse on
        # a different guest even if that guest is later added to the same user's ACL.
        api('set', '/access/acl', path='/vms/' + str(other['vmid']), users=user, roles=roles[1])
        http('GET', otherbase + '/vncwebsocket', {'port': terminal['port'], 'vncticket': terminal['ticket']}, headers=cookie, expected=401)
        http('POST', base + '/termproxy', {'container': othercid}, headers=cookie, expected=500)
        http('GET', base + '/vncwebsocket', {'port': terminal['port'], 'vncticket': expired_console_ticket(user, safe['vmid'])}, headers=cookie, expected=401)
        # Denied ticket requests are deliberately delayed by PVE. Obtain a fresh
        # listener after those checks so termproxy's authentication timeout cannot
        # expire while the test is exercising unrelated denials.
        terminal = http('POST', base + '/termproxy', {'container': cid}, headers=cookie)
        terminal_session(base, terminal, cookie, cid)
        print('OK user-session shell, guest ticket binding and forged container denial', flush=True)

        http('POST', base + '/action', {'action': 'shutdown'}, headers=headers['audit'], expected=403)
        upid = http('POST', base + '/action', {'action': 'shutdown'}, headers=headers['operator'])
        task(upid)
        http('DELETE', base, headers=headers['operator'], expected=403)
        task(http('POST', base + '/action', {'action': 'start'}, headers=headers['operator']))
        print('OK operator power without configuration or removal grants', flush=True)

        task(http('POST', base + '/action', {'action': 'shutdown'}, headers=headers['operator']))
        details = http('GET', base, headers=headers['editor'])
        changed = dict(details['deployment'], environment='DELEGATION_SECRET=not-for-audit')
        changed['compose'] = source.replace('busybox:1.37', 'busybox:1.37\n    privileged: true')
        upid = http('PUT', base, {'deployment': changed, 'digest': details['digest']}, headers=headers['editor'])
        try:
            task(upid)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Delegated privileged edit succeeded')
        assert 'privileged' not in api('get', base)['deployment']['compose']
        task(http('PUT', base, {'deployment': details['deployment'], 'digest': details['digest']}, headers=headers['editor']))
        print('OK unsafe edit rejected, existing deployment preserved, safe edit accepted', flush=True)
        # Grant network/storage access separately; guest privileges alone are insufficient.
        http('PUT', base + '/storage-limit', {'size_mib': 256, 'storage': 'local-lvm'}, headers=headers['editor'], expected=403)
        editor = users[-1]
        store_role = 'BDPerm' + nonce + 'resources'
        api('create', '/access/roles', roleid=store_role, privs='Datastore.Audit Datastore.AllocateSpace SDN.Use')
        roles.append(store_role)
        def grant(path, role=store_role):
            for principal in ({'users': editor}, {'tokens': editor + '!test'}):
                api('set', '/access/acl', path=path, roles=role, **principal)
        grant('/storage/local-lvm')
        grant('/storage/' + args.backup_storage)
        grant('/sdn/zones/localnetwork/' + args.bridge)
        vmid = int(api('get', '/cluster/nextid'))
        grant('/vms/' + str(vmid), roles[3])
        task(http('POST', f'/nodes/{node}/docker', {'deployment': {
            'name': 'permissions-' + nonce + '-created', 'vmid': vmid,
            'mode': 'image', 'image': 'busybox:1.37', 'bridge': args.bridge,
            'address': args.create_address, 'cidr': args.create_address + '/' + str(args.prefix),
        }}, headers=headers['editor']))
        created_guest = next(r for r in worker.summaries() if r.get('vmid') == vmid)
        guests.append(created_guest['id'])
        print('OK delegated creation with scoped allocation and bridge access', flush=True)

        data = http('GET', base + '/management', headers=headers['editor'])
        task(http('PUT', base + '/storage-limit', {'size_mib': 256, 'storage': 'local-lvm', 'digest': data['digest']}, headers=headers['editor']))
        task(http('POST', base + '/snapshot', {'action': 'create', 'snapname': 'delegated'}, headers=headers['editor']))
        task(http('POST', base + '/snapshot', {'action': 'restore', 'snapname': 'delegated'}, headers=headers['editor']))
        task(http('POST', base + '/snapshot', {'action': 'delete', 'snapname': 'delegated'}, headers=headers['editor']))
        print('OK delegated disk and snapshot rollback', flush=True)

        vmid = int(api('get', '/cluster/nextid'))
        grant('/vms/' + str(vmid), roles[3])
        data = http('GET', base + '/management', headers=headers['editor'])
        task(http('POST', base + '/clone', {'newid': vmid, 'deployment': {'name': 'permissions-' + nonce + '-clone',
            'bridge': args.bridge, 'address': args.clone_address, 'cidr': args.clone_address + '/' + str(args.prefix)},
            'digest': data['digest'], 'storage': 'local-lvm'}, headers=headers['editor']))
        cloned = next(r for r in worker.summaries() if r.get('vmid') == vmid)
        guests.append(cloned['id'])
        print('OK delegated clone and destination allocation', flush=True)

        task(http('POST', f'/nodes/{node}/vzdump', {'vmid': safe['vmid'], 'storage': args.backup_storage, 'mode': 'stop'}, headers=headers['editor']))
        backups = api('get', f'/nodes/{node}/storage/{args.backup_storage}/content', content='backup', vmid=safe['vmid'])
        archive = sorted(backups, key=lambda r: r.get('ctime', 0))[-1]['volid']
        vmid = int(api('get', '/cluster/nextid'))
        grant('/vms/' + str(vmid), roles[3])
        task(http('POST', f'/nodes/{node}/docker', {'archive': archive, 'newid': vmid, 'storage': 'local-lvm',
            'deployment': {'name': 'permissions-' + nonce + '-restored', 'bridge': args.bridge,
                'address': args.restore_address, 'cidr': args.restore_address + '/' + str(args.prefix)}}, headers=headers['editor']))
        restored = next(r for r in worker.summaries() if r.get('vmid') == vmid)
        guests.append(restored['id'])
        assert http('GET', f'/nodes/{node}/docker/' + restored['id'] + '/management', headers=headers['editor'])['capabilities']['console']
        print('OK delegated PBS backup and restore', flush=True)

        data = http('GET', base + '/management', headers=headers['editor'])
        task(http('POST', base + '/migrate', {'target': args.target, 'storage': 'local-lvm', 'digest': data['digest']}, headers=headers['editor']))
        base = f"/nodes/{args.target}/docker/{safe['id']}"
        data = http('GET', base + '/management', headers=headers['editor'])
        assert data['node'] == args.target and data['vmid'] == safe['vmid'] and data['capabilities']['console']
        task(http('POST', base + '/action', {'action': 'start'}, headers=headers['editor']))
        import transfer, shlex
        remote_cid = subprocess.check_output(transfer.ssh_command(args.target) + [shlex.join(['docker', 'ps', '--no-trunc', '-q', '--filter', 'label=betterdocker.id=' + safe['id']])], text=True).strip().splitlines()[0]
        remote_terminal = http('POST', base + '/termproxy', {'container': remote_cid}, headers=cookie)
        remote_hostname = subprocess.check_output(transfer.ssh_command(args.target) + [shlex.join(['docker', 'inspect', '--format', '{{.Config.Hostname}}', remote_cid])], text=True).strip()
        terminal_session(base, remote_terminal, cookie, remote_cid, hostname=remote_hostname)
        direct_terminal = http('POST', base + '/termproxy', {'container': remote_cid}, headers=cookie, origin=args.target_address)
        terminal_session(base, direct_terminal, cookie, remote_cid, origin=args.target_address, hostname=remote_hostname)
        task(http('POST', base + '/action', {'action': 'shutdown'}, headers=headers['editor']))
        data = http('GET', base + '/management', headers=headers['editor'])
        print('OK delegated console after migration on destination PVE release', flush=True)
        task(http('POST', base + '/template', {'digest': data['digest']}, headers=headers['editor']))
        assert http('GET', base + '/management', headers=headers['editor'])['template']
        clonebase = f"/nodes/{node}/docker/{cloned['id']}"
        task(http('DELETE', clonebase, headers=headers['editor']))
        worker.purge_retained_storage(cloned['id'])
        import shutil
        shutil.rmtree(worker.ROOT / cloned['id'])
        print('OK delegated migration, template conversion and removal', flush=True)
        print('PASS delegated permissions acceptance', flush=True)
    except BaseException:
        import traceback
        traceback.print_exc()
        raise
    finally:
        # Use root for cleanup. ACL entries disappear with the temporary users.
        for identifier in reversed(guests):
            info = next((r for r in worker.summaries() if r['id'] == identifier), None)
            if info:
                path = f"/nodes/{info['node']}/docker/{identifier}"
                try:
                    task(api('create', path + '/action', action='force-stop'))
                    task(api('delete', path))
                    import transfer, shlex
                    command = ['/usr/local/lib/betterdocker/provision.py', 'purge-retained-storage', identifier]
                    if info['node'] != node:
                        command = transfer.ssh_command(info['node']) + [shlex.join(command)]
                    cleanup = subprocess.run(command, capture_output=True, text=True)
                    if cleanup.returncode and 'No retained PVE disks' not in cleanup.stderr:
                        raise RuntimeError(cleanup.stderr.strip())
                    project = worker.ROOT / identifier
                    if project.exists():
                        import shutil
                        shutil.rmtree(project)
                    worker.release_id(identifier)
                except Exception as error:
                    print('Cleanup requires attention:', identifier, str(error), file=sys.stderr)
        for user in reversed(users):
            api('delete', '/access/users/' + user)
        for role in reversed(roles):
            api('delete', '/access/roles/' + role)


if __name__ == '__main__':
    main()
