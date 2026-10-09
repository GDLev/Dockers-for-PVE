#!/usr/bin/env python3
"""Disposable PVE tests for pools, inherited ACLs and native guest hook phases.

Run on a test PVE node after installing BetterDockers on the target. No PBS code
is installed. --keep leaves only these named fixtures for final GUI inspection;
--cleanup removes them using the saved manifest.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
import urllib.parse

sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
import transfer
from live_permissions import http


_ROOT_HEADERS = None


def api(method, route, **values):
    global _ROOT_HEADERS
    if _ROOT_HEADERS is None:
        # A local root process may issue its own native PVE session. Keep the
        # ticket in memory; never print it or put it in a command argument.
        code = 'use PVE::AccessControl; use JSON; print encode_json({ticket=>PVE::AccessControl::assemble_ticket(q(root@pam)), csrf=>PVE::AccessControl::assemble_csrf_prevention_token(q(root@pam))});'
        session = json.loads(subprocess.check_output(['perl', '-e', code], text=True))
        _ROOT_HEADERS = {'Cookie': 'PVEAuthCookie=' + session['ticket'], 'CSRFPreventionToken': session['csrf']}
    if method == 'delete' and values:
        route += '?' + urllib.parse.urlencode(values)
        values = {}
    try:
        return http({'get': 'GET', 'create': 'POST', 'set': 'PUT', 'delete': 'DELETE'}[method],
                    route, values, headers=_ROOT_HEADERS)
    except AssertionError as error:
        raise RuntimeError(str(error)) from error


def task(upid):
    node = upid.split(':')[1]
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = api('get', f'/nodes/{node}/tasks/{upid}/status')
        if status['status'] == 'stopped':
            if status.get('exitstatus') != 'OK':
                raise RuntimeError(json.dumps(api('get', f'/nodes/{node}/tasks/{upid}/log')))
            print('OK', upid.split(':')[5], flush=True)
            return
        time.sleep(.5)
    raise RuntimeError('Task did not finish: ' + upid)


def wait_for(predicate, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1)
    raise AssertionError('Acceptance condition timed out')


def cleanup(path):
    state = json.loads(Path(path).read_text())
    stem = Path(path).stem
    assert stem.startswith('pools-hooks-') and len(stem) == 20, 'Invalid fixture manifest'
    for info in worker.summaries():
        if info['name'] == stem or info['name'].startswith(stem + '-'):
            if info['id'] not in state['guests']:
                state['guests'].append(info['id'])
    for identifier in state['guests']:
        metadata = worker.METADATA / (identifier + '.json')
        if not metadata.exists():
            continue
        info = json.loads(metadata.read_text())
        base = f'/nodes/{info["node"]}/docker/{identifier}'
        sid = 'docker:' + str(info['vmid'])
        if any(item['sid'] == sid for item in api('get', '/cluster/ha/resources')):
            api('set', '/cluster/ha/resources/' + sid, state='stopped')
            wait_for(lambda: api('get', base + '/management')['status'] == 'stopped')
            api('delete', '/cluster/ha/resources/' + sid)
        jobs = [item for item in api('get', '/cluster/replication') if str(item['guest']) == str(info['vmid'])]
        for job in jobs:
            api('delete', '/cluster/replication/' + job['id'])
            command = ['pvesr', 'run', '--id', job['id']]
            if info['node'] != worker.current_node():
                command = transfer.ssh_command(info['node']) + [shlex.join(command)]
            subprocess.run(command, check=True, capture_output=True, text=True)
        if info.get('replication'):
            command = ['/usr/local/lib/betterdocker/replica.py', 'cleanup', identifier, info['node']]
            if info['node'] != worker.current_node():
                command = transfer.ssh_command(info['node']) + [shlex.join(command)]
            subprocess.run(command, check=True, capture_output=True, text=True)
        # Clear a deliberately interrupted test hook before stopping/removing it.
        api('set', base + '/config', delete='hookscript')
        task(api('create', base + '/action', action='force-stop'))
        api('set', '/access/acl', path='/vms/' + str(info['vmid']), roles=state['roles'][0], users=state['users'][0]) if state['users'] and state['roles'] else None
        task(api('delete', base))
        acl_path = '/vms/' + str(info['vmid'])
        assert not any(item['path'] == acl_path or item['path'].startswith(acl_path + '/') for item in api('get', '/access/acl'))
        assert not any(member.get('vmid') == info['vmid'] for pool in state['pools'] for member in api('get', '/pools', poolid=pool)[0]['members'])
        command = ['/usr/local/lib/betterdocker/provision.py', 'purge-retained-storage', identifier]
        if info['node'] != worker.current_node():
            command = transfer.ssh_command(info['node']) + [shlex.join(command)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode and 'No retained PVE disks' not in result.stderr:
            raise RuntimeError(result.stderr.strip())
    for user in state['users']:
        api('delete', '/access/users/' + user)
    for role in state['roles']:
        api('delete', '/access/roles/' + role)
    for pool in state['pools']:
        api('delete', '/pools', poolid=pool)
    for node in state['nodes']:
        files = [state['script'], state['log'], state['fail'], state['hang'], state['entered']]
        if node == worker.current_node():
            for file in files:
                Path(file).unlink(missing_ok=True)
        else:
            subprocess.run(transfer.ssh_command(node) + [shlex.join(['rm', '-f', *files])], check=True)
    api('set', '/storage/local', content=state['local_content'])
    Path(path).unlink()
    print('PASS fixture cleanup', flush=True)


def check_missing_hook(path):
    state = json.loads(Path(path).read_text())
    info = next(item for item in worker.summaries() if item['name'] == Path(path).stem + '-clone')
    base = f'/nodes/{info["node"]}/docker/{info["id"]}'
    script = Path(state['script'])
    hidden = script.with_suffix('.missing')
    task(api('create', base + '/action', action='start'))
    script.rename(hidden)
    try:
        upid = api('create', base + '/action', action='shutdown', timeout=15)
        task(upid)
        log = json.dumps(api('get', f'/nodes/{info["node"]}/tasks/{upid}/log'))
        assert 'pre-stop' in log and 'post-stop' in log and 'hookscript error' in log
        upid = api('create', base + '/action', action='start')
        try:
            task(upid)
        except RuntimeError as error:
            assert 'pre-start' in str(error)
        else:
            raise AssertionError('Missing pre-start snippet did not block startup')
        assert api('get', base + '/management')['status'] == 'stopped'
    finally:
        hidden.rename(script)
    task(api('create', base + '/action', action='start'))
    task(api('create', base + '/action', action='shutdown', timeout=15))
    print('PASS native missing-snippet warnings, fatal pre-start and recovery', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup')
    parser.add_argument('--check-hooks')
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--target', default='pve2')
    parser.add_argument('--third', default='pve3')
    parser.add_argument('--third-address', default='192.0.2.218')
    parser.add_argument('--backup-storage', default='pbs1')
    parser.add_argument('--address', default='192.0.2.222')
    parser.add_argument('--other-address', default='192.0.2.221')
    parser.add_argument('--clone-address', default='192.0.2.220')
    parser.add_argument('--restore-address', default='192.0.2.219')
    parser.add_argument('--failure-address', default='192.0.2.217')
    parser.add_argument('--ha-storage', default='bdstage7-zfs')
    args = parser.parse_args()
    if args.check_hooks:
        check_missing_hook(args.check_hooks)
        return
    if args.cleanup:
        cleanup(args.cleanup)
        return
    node = worker.current_node()
    nonce = uuid.uuid4().hex[:8]
    stem = 'pools-hooks-' + nonce
    manifest = Path('/root/' + stem + '.json')
    script = '/var/lib/vz/snippets/' + stem + '.sh'
    state = dict(guests=[], users=[], roles=[], pools=[], nodes=[node, args.target, args.third], script=script,
                 log='/root/' + stem + '.log', fail='/root/' + stem + '.fail',
                 hang='/root/' + stem + '.hang', entered='/root/' + stem + '.entered',
                 local_content=api('get', '/storage/local')['content'])
    def save():
        manifest.write_text(json.dumps(state))
    def guest(name):
        info = next(item for item in worker.summaries() if item['name'] == name)
        if info['id'] not in state['guests']:
            state['guests'].append(info['id']); save()
        return info
    def base(info):
        owner = json.loads((worker.METADATA / (info['id'] + '.json')).read_text())['node']
        return f'/nodes/{owner}/docker/{info["id"]}'
    def power(info, action, headers=None):
        upid = http('POST', base(info) + '/action', {'action': action, 'timeout': 15}, headers=headers) if headers else api('create', base(info) + '/action', action=action, timeout=15)
        task(upid)
    def phases(info):
        owner = json.loads((worker.METADATA / (info['id'] + '.json')).read_text())['node']
        if owner != node:
            data = subprocess.check_output(transfer.ssh_command(owner) + [shlex.join(['cat', state['log']])], text=True)
        else:
            data = subprocess.check_output(['cat', state['log']], text=True) if Path(state['log']).exists() else ''
        return [line.split()[1] for line in data.splitlines() if line.split()[0] == str(info['vmid'])]
    def failed(upid, needle):
        try:
            task(upid)
        except RuntimeError as error:
            assert needle in str(error), str(error)[:800]
        else:
            raise AssertionError('Expected a failed task')
    source = 'services:\n  app:\n    image: busybox:1.37\n    command: ["sh", "-c", "trap \'exit 0\' TERM; while :; do sleep 1 & wait $$!; done"]\n    volumes: [data:/data]\nvolumes:\n  data: {}\n'
    def deployment(name, address):
        return dict(name=name, mode='compose', compose=source, bridge='vmbr0', address=address, cidr=address + '/20')
    save()
    try:
        for suffix in ('a', 'b'):
            pool = stem + '-' + suffix
            api('create', '/pools', poolid=pool)
            state['pools'].append(pool); save()
        a, b = state['pools']
        api('set', '/storage/local', content=state['local_content'] + ',snippets' if 'snippets' not in state['local_content'].split(',') else state['local_content'])
        body = '#!/bin/sh\nset -eu\nprintf "%s %s\\n" "$1" "$2" >> ' + shlex.quote(state['log']) + '\n'
        pool_check = 'my $c=PVE::Cluster::cfs_read_file("user.cfg"); exit(!$c->{vms}->{$ARGV[0]});'
        body += 'if test "$2" = pre-start; then perl -MPVE::AccessControl -MPVE::Cluster -e ' + shlex.quote(pool_check) + ' "$1"; fi\n'
        body += 'if test -f ' + shlex.quote(state['fail']) + ' && { test ! -s ' + shlex.quote(state['fail']) + ' || test "$(cat ' + shlex.quote(state['fail']) + ')" = "$2"; }; then echo "test hook failure $2" >&2; exit 42; fi\n'
        body += 'if test "$2" = pre-start && test -f ' + shlex.quote(state['hang']) + '; then touch ' + shlex.quote(state['entered']) + '; sleep 120; fi\n'
        Path(script).parent.mkdir(parents=True, exist_ok=True)
        Path(script).write_text(body); os.chmod(script, 0o700)
        for destination in state['nodes'][1:]:
            subprocess.run(transfer.ssh_command(destination) + [shlex.join(['mkdir', '-p', '/var/lib/vz/snippets'])], check=True)
            subprocess.run(transfer.ssh_command(destination) + ['cat > ' + shlex.quote(script) + ' && chmod 700 ' + shlex.quote(script)], input=body, text=True, check=True)
        hook = 'local:snippets/' + Path(script).name
        task(api('create', f'/nodes/{node}/docker', deployment=deployment(stem, args.address), pool=a, hookscript=hook))
        primary = guest(stem)
        assert phases(primary) == ['pre-start', 'post-start'], phases(primary)
        role, user = 'PoolHooks' + nonce, stem + '@pve'
        api('create', '/access/roles', roleid=role, privs='Pool.Audit Pool.Allocate VM.Audit VM.Allocate VM.PowerMgmt VM.Clone VM.Migrate VM.Config.Options')
        state['roles'].append(role); save()
        api('create', '/access/users', userid=user, password=secrets.token_urlsafe(24))
        state['users'].append(user); save()
        host_role = 'PoolHost' + nonce
        api('create', '/access/roles', roleid=host_role, privs='Datastore.Audit Datastore.AllocateSpace SDN.Use')
        state['roles'].append(host_role); save()
        token = api('create', '/access/users/' + user + '/token/test', privsep=1)
        headers = {'Authorization': 'PVEAPIToken=' + user + '!test=' + token['value']}
        for principal in ({'users': user}, {'tokens': user + '!test'}):
            api('set', '/access/acl', path='/pool/' + a, roles=role, **principal)
            api('set', '/access/acl', path='/storage/local-lvm', roles=host_role, **principal)
            api('set', '/access/acl', path='/sdn/zones/localnetwork/vmbr0', roles=host_role, **principal)
        assert http('GET', base(primary) + '/management', headers=headers)['capabilities']['power']
        http('GET', f'/nodes/{node}/docker/' + next(item['id'] for item in worker.summaries() if item['vmid'] == 100) + '/management', headers=headers, expected=403)
        try:
            api('delete', '/pools', poolid=a)
        except RuntimeError as error:
            assert 'not empty' in str(error)
        else:
            raise AssertionError('Native pool deletion ignored a DCT member')
        members = http('GET', '/pools', {'poolid': a, 'type': 'docker'}, headers=headers)[0]['members']
        assert any(item['vmid'] == primary['vmid'] and item['type'] == 'docker' for item in members)
        resource = next(item for item in http('GET', '/cluster/resources', headers=headers) if item.get('vmid') == primary['vmid'])
        assert resource['pool'] == a
        http('PUT', base(primary) + '/config', {'hookscript': hook}, headers=headers, expected=403)
        http('PUT', base(primary) + '/config', {'delete': 'hookscript'}, headers=headers, expected=403)
        task(http('POST', f'/nodes/{node}/docker', {'deployment': deployment(stem + '-allocated', args.other_address), 'pool': a}, headers=headers))
        secondary = guest(stem + '-allocated')
        limited_role = 'PoolOnly' + nonce
        api('create', '/access/roles', roleid=limited_role, privs='Pool.Audit Pool.Allocate')
        state['roles'].append(limited_role); save()
        limited = api('create', '/access/users/' + user + '/token/limited', privsep=1)
        api('set', '/access/acl', path='/pool/' + a, roles=limited_role, tokens=user + '!limited')
        limited_headers = {'Authorization': 'PVEAPIToken=' + user + '!limited=' + limited['value']}
        http('PUT', '/pools', {'poolid': a, 'vms': primary['vmid'], 'delete': 1}, headers=limited_headers, expected=(403, 500))
        print('PASS pool-only allocation, inventory, token inheritance and hook ACLs', flush=True)
        # A token cannot move a member out of a pool it cannot allocate in.
        api('set', '/pools', poolid=b, vms=primary['vmid'], **{'allow-move': 1})
        api('set', '/access/acl', path='/pool/' + b, roles=role, users=user)
        http('GET', base(primary) + '/management', headers=headers, expected=403)
        http('PUT', '/pools', {'poolid': a, 'vms': primary['vmid'], 'allow-move': 1}, headers=headers, expected=(403, 500))
        api('set', '/pools', poolid=a, vms=primary['vmid'], **{'allow-move': 1})
        http('PUT', '/pools', {'poolid': a, 'vms': secondary['vmid'], 'delete': 1}, headers=headers)
        http('GET', base(secondary) + '/management', headers=headers, expected=403)
        api('set', '/pools', poolid=a, vms=secondary['vmid'])
        print('PASS native pool move/remove and immediate ACL changes', flush=True)
        power(primary, 'start', headers)
        assert phases(primary) == ['pre-start', 'post-start']
        power(primary, 'reboot', headers)
        assert phases(primary)[-4:] == ['pre-stop', 'post-stop', 'pre-start', 'post-start']
        power(primary, 'shutdown', headers)
        Path(state['fail']).touch()
        failed(api('create', base(primary) + '/action', action='start'), 'test hook failure pre-start')
        assert api('get', base(primary) + '/management')['status'] == 'stopped'
        Path(state['fail']).write_text('post-start')
        power(primary, 'start')
        assert api('get', base(primary) + '/management')['status'] == 'running'
        Path(state['fail']).unlink()
        Path(state['fail']).touch()
        power(primary, 'force-stop')
        Path(state['fail']).unlink()
        assert phases(primary)[-2:] == ['pre-stop', 'post-stop']
        Path(state['fail']).touch()
        failed(api('create', f'/nodes/{node}/docker', deployment=deployment(stem + '-failed', args.failure_address),
                   pool=a, hookscript=hook), 'test hook failure pre-start')
        failed_guest = guest(stem + '-failed')
        assert http('GET', base(failed_guest) + '/management', headers=headers)['capabilities']['remove']
        Path(state['fail']).unlink()
        task(http('DELETE', base(failed_guest), headers=headers))
        assert not any(item['vmid'] == failed_guest['vmid'] for item in api('get', '/pools', poolid=a)[0]['members'])
        print('PASS failed first Start retains pool ownership and permits delegated cleanup', flush=True)
        # Simulate a killed worker and script, without killing PVE services.
        Path(state['hang']).touch()
        command = ['/usr/local/lib/betterdocker/provision.py', 'action', primary['id'], node, 'start']
        with open('/root/' + stem + '-interruption.log', 'w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: Path(state['entered']).exists(), timeout=20)
            os.killpg(process.pid, signal.SIGKILL); process.wait()
        Path(state['hang']).unlink()
        before = len(phases(primary))
        failed(api('create', base(primary) + '/action', action='start'), 'interrupted')
        assert len(phases(primary)) == before
        api('set', base(primary) + '/config', hookscript=hook)
        power(primary, 'start'); power(primary, 'shutdown')
        print('PASS hook order, fatal pre-start, warning stop hooks and killed-worker recovery', flush=True)
        task(api('create', base(primary) + '/clone', deployment={'name': stem + '-clone', 'bridge': 'vmbr0', 'cidr': args.clone_address + '/20'}, pool=b))
        cloned = guest(stem + '-clone')
        assert api('get', base(cloned) + '/config')['hookscript'] == hook
        assert any(item['vmid'] == cloned['vmid'] for item in api('get', '/pools', poolid=b)[0]['members'])
        task(api('create', f'/nodes/{node}/vzdump', vmid=primary['vmid'], storage=args.backup_storage, mode='stop'))
        backups = api('get', f'/nodes/{node}/storage/{args.backup_storage}/content', content='backup', vmid=primary['vmid'])
        archive = max(backups, key=lambda item: item.get('ctime', 0))['volid']
        task(api('create', f'/nodes/{node}/docker', archive=archive, storage='local-lvm', pool=b,
                 deployment={'name': stem + '-restore', 'bridge': 'vmbr0', 'cidr': args.restore_address + '/20'}))
        restored = guest(stem + '-restore')
        assert api('get', base(restored) + '/config')['hookscript'] == hook
        assert any(item['vmid'] == restored['vmid'] for item in api('get', '/pools', poolid=b)[0]['members'])
        task(http('POST', base(primary) + '/migrate', {'target': args.target, 'storage': 'local-lvm'}, headers=headers))
        assert http('GET', base(primary) + '/management', headers=headers)['hookscript'] == hook
        assert any(item['vmid'] == primary['vmid'] for item in api('get', '/pools', poolid=a)[0]['members'])
        power(primary, 'start', headers); power(primary, 'shutdown', headers)
        assert phases(primary) == ['pre-start', 'post-start', 'pre-stop', 'post-stop']
        task(api('create', base(primary) + '/migrate', target=node, storage='local-lvm'))
        print('PASS clone, PBS restore and cross-node migration retain native pool/configuration semantics', flush=True)
        task(api('create', f'/nodes/{args.third}/docker', deployment=deployment(stem + '-third', args.third_address), pool=a, hookscript=hook))
        third = guest(stem + '-third')
        assert phases(third) == ['pre-start', 'post-start']
        assert http('GET', base(third) + '/management', headers=headers)['capabilities']['power']
        power(third, 'shutdown', headers)
        assert phases(third)[-2:] == ['pre-stop', 'post-stop']
        print('PASS PVE 9.2 node pool permissions and hook phases', flush=True)
        # Native node Start All uses the same hooks and skips duplicate starts.
        api('set', base(primary) + '/config', onboot=1)
        before = len(phases(primary))
        task(api('create', f'/nodes/{node}/startall', vms=str(primary['vmid'])))
        assert phases(primary)[before:] == ['pre-start', 'post-start']
        power(primary, 'shutdown')
        task(api('set', base(primary) + '/storage-limit', size_mib=512, storage=args.ha_storage))
        job = str(primary['vmid']) + '-0'
        api('create', '/cluster/replication', id=job, target=args.target, type='local', schedule='00:59')
        subprocess.run(['pvesr', 'run', '--id', job], check=True, capture_output=True, text=True)
        wait_for(lambda: args.target in api('get', base(primary) + '/management').get('replication', {}).get('targets', {}))
        sid = 'docker:' + str(primary['vmid'])
        api('create', '/cluster/ha/resources', sid=sid, state='stopped')
        before = len(phases(primary))
        power(primary, 'start')
        wait_for(lambda: api('get', base(primary) + '/management')['status'] == 'running')
        assert phases(primary)[before:] == ['pre-start', 'post-start']
        power(primary, 'shutdown')
        wait_for(lambda: api('get', base(primary) + '/management')['status'] == 'stopped')
        assert phases(primary)[-2:] == ['pre-stop', 'post-stop']
        print('PASS native node lifecycle and HA-controlled hook execution', flush=True)
        print('PASS all Stage 2 live checks; fixtures: ' + str(manifest), flush=True)
    finally:
        if not args.keep:
            cleanup(manifest)
        else:
            print('Fixtures retained for GUI inspection: ' + str(manifest), flush=True)


if __name__ == '__main__':
    main()
