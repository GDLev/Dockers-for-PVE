#!/usr/bin/env python3
"""Final UX API checks on disposable DCTs. Run on a test PVE node after deployment.

run keeps two fixtures for the final GUI comparison. cleanup removes only the
recorded guests, users, roles and snippet. No PBS or existing guest is modified.
"""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
import live_pools_hooks as live
from live_permissions import http

STATE = Path('/root/ux-acceptance-fixture.json')


def save(state):
    STATE.write_text(json.dumps(state))
    STATE.chmod(0o600)


def task_result(upid, expected='OK', reason=None):
    endpoint = '/nodes/' + upid.split(':')[1] + '/tasks/' + upid
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = live.api('get', endpoint + '/status')
        if status['status'] == 'stopped':
            result = status.get('exitstatus', '')
            assert (result == expected if expected != 'failure' else result != 'OK' and not result.startswith('WARNINGS:')), status
            if reason:
                log = '\n'.join(row['t'] for row in live.api('get', endpoint + '/log', limit=1000))
                assert reason in log, log
            print('PASS task', upid.split(':')[5], expected, flush=True)
            return status
        time.sleep(.5)
    raise AssertionError('Task did not finish: ' + upid)


def configuration(guest):
    return live.api('get', guest['base'] + '/management')


def settings(guest, values, headers=None, expected='OK'):
    data = {'service': 'app', 'settings': json.dumps(values), 'digest': configuration(guest)['digest']}
    upid = live.api('set', guest['base'] + '/settings', **data) if headers is None else http('PUT', guest['base'] + '/settings', data, headers=headers)
    return task_result(upid, expected)


def cleanup():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    assert state['name'].startswith('ux-acceptance-')
    rows = live.api('get', '/cluster/resources', type='vm')
    for guest in state['guests']:
        row = next((row for row in rows if row.get('vmid') == guest['vmid']), None)
        if row:
            assert row.get('name') == guest['name'] and row['node'] == state['node'], 'Fixture identity changed'
            identifier = row['id'].split('/')[-1]
            assert not guest.get('id') or identifier == guest['id']
            guest.update(id=identifier, base='/nodes/' + state['node'] + '/docker/' + identifier)
            live.api('set', guest['base'] + '/config', delete='hookscript')
            task_result(live.api('create', guest['base'] + '/action', action='force-stop'))
            for snapshot in live.api('get', guest['base'] + '/snapshots'):
                if snapshot['name'] != 'current':
                    task_result(live.api('create', guest['base'] + '/snapshot', action='delete', snapname=snapshot['name']))
            task_result(live.api('delete', guest['base']))
        if guest.get('id'):
            folder = worker.ROOT / guest['id']
            if folder.exists():
                assert json.loads((folder / 'request.json').read_text())['name'] == guest['name']
                assert not (worker.METADATA / (guest['id'] + '.json')).exists()
                assert not worker.container_records(guest['id'])
                shutil.rmtree(folder)
    users = {item['userid'] for item in live.api('get', '/access/users')}
    for user in state['users']:
        if user in users:
            live.api('delete', '/access/users/' + user)
    roles = {item['roleid'] for item in live.api('get', '/access/roles')}
    for role in state['roles']:
        if role in roles:
            live.api('delete', '/access/roles/' + role)
    if state.get('snippet'):
        path = Path(state['snippet'])
        assert path.name == state['name'] + '.sh'
        path.unlink(missing_ok=True)
    if state.get('snippet_storage'):
        storage = state['snippet_storage']
        assert storage == 'uxsnip-' + state['name'].removeprefix('ux-acceptance-')
        if any(row['storage'] == storage for row in live.api('get', '/storage')):
            live.api('delete', '/storage/' + storage)
        folder = Path('/var/lib/vz') / state['name']
        assert not any(folder.glob('snippets/*'))
        shutil.rmtree(folder, ignore_errors=True)
    for suffix in ('fail', 'entered', 'warn'):
        Path('/run/' + state['name'] + '.' + suffix).unlink(missing_ok=True)
    assert not any(row.get('name', '').startswith(state['name']) for row in live.api('get', '/cluster/resources', type='vm'))
    assert not any(row.get('ugid') in state['users'] for row in live.api('get', '/access/acl'))
    STATE.unlink()
    print('PASS acceptance fixtures, accounts, ACLs, roles and retained data removed', flush=True)


def cross_node():
    state = json.loads(STATE.read_text())
    running, stopped = state['guests']
    nodes = [row for row in live.api('get', '/cluster/status') if row['type'] == 'node' and row.get('online')]
    for user in state['users']:
        # Test the existing guest-scoped ACLs without persisting passwords or
        # tickets. Local root can issue a native session for this test identity.
        perl = 'use PVE::AccessControl; use JSON; print encode_json({ticket=>PVE::AccessControl::assemble_ticket($ARGV[0]),csrf=>PVE::AccessControl::assemble_csrf_prevention_token($ARGV[0])});'
        session = json.loads(subprocess.check_output(['perl', '-e', perl, user], text=True))
        headers = {'Cookie': 'PVEAuthCookie=' + session['ticket'], 'CSRFPreventionToken': session['csrf']}
        for node in nodes:
            origin = node['ip']
            data = http('GET', running['base'] + '/management', headers=headers, origin=origin)
            assert data['capabilities']['audit'] and not data['capabilities']['trusted']
            assert 'deployment' not in data
            http('GET', stopped['base'] + '/management', headers=headers, origin=origin, expected=403)
            assert {row['vmid'] for row in http('GET', '/cluster/resources', {'type': 'vm'}, headers=headers, origin=origin)} == {running['vmid']}
            print('PASS guest-scoped proxy and filtered inventory', node['name'], user.split('-')[1], flush=True)


def run(args):
    assert not STATE.exists(), 'Clean up the previous fixture first'
    node = worker.current_node()
    assert len(set(args.address)) == 2
    before = live.api('get', '/cluster/resources', type='vm')
    assert not any(row.get('address') in args.address for row in live.api('get', '/nodes/' + node + '/docker'))
    nonce = uuid.uuid4().hex[:8]
    state = {'name': 'ux-acceptance-' + nonce, 'node': node, 'guests': [], 'users': [], 'roles': []}
    save(state)
    for index, address in enumerate(args.address):
        name = state['name'] + ('-running' if index == 0 else '-stopped')
        guest = {'name': name, 'vmid': int(live.api('get', '/cluster/nextid'))}
        state['guests'].append(guest)
        save(state)
        compose = {'services': {'app': {'image': 'busybox:1.37', 'restart': 'no',
            'mem_limit': '64m', 'memswap_limit': '128m', 'cpus': 1,
            'command': ['sh', '-c', "trap 'exit 0' TERM; while sleep 1; do :; done"],
            'ports': [str(18600 + index * 30 + port) + ':8080' for port in range(24)]}}}
        deployment = {'name': name, 'vmid': guest['vmid'], 'mode': 'compose', 'compose': json.dumps(compose),
            'environment': 'UX_TEST_SECRET=private-fixture-value\n', 'bridge': args.bridge,
            'ipv4mode': 'static', 'cidr': address + '/' + str(args.prefix)}
        task_result(live.api('create', '/nodes/' + node + '/docker', deployment=json.dumps(deployment), start=int(index == 0)))
        row = next(row for row in live.api('get', '/cluster/resources', type='vm') if row.get('name') == name)
        guest.update(id=row['id'].split('/')[-1], base='/nodes/' + node + '/docker/' + row['id'].split('/')[-1])
        save(state)
        assert configuration(guest)['status'] == ('running' if index == 0 else 'stopped')
    running, stopped = state['guests']
    passwords = {}
    for label, grants in [('reader', 'VM.Audit'), ('editor', 'VM.Audit VM.Config.Memory VM.Config.CPU VM.Config.Network VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback')]:
        user, role = 'ux-' + label + '-' + nonce + '@pve', 'UX' + label + nonce
        state['roles'].append(role); save(state)
        live.api('create', '/access/roles', roleid=role, privs=grants)
        state['users'].append(user); save(state)
        passwords[label] = secrets.token_urlsafe(24)
        live.api('create', '/access/users', userid=user, password=passwords[label])
        live.api('set', '/access/acl', path='/vms/' + str(running['vmid']), users=user, roles=role)
        session = http('POST', '/access/ticket', {'username': user, 'password': passwords[label]})
        headers = {'Cookie': 'PVEAuthCookie=' + session['ticket'], 'CSRFPreventionToken': session['CSRFPreventionToken']}
        data = http('GET', running['base'] + '/management', headers=headers)
        assert not data['capabilities']['trusted'] and 'deployment' not in data
        assert 'private-fixture-value' not in json.dumps(data)
        http('GET', stopped['base'] + '/management', headers=headers, expected=403)
        assert {row['vmid'] for row in http('GET', '/cluster/resources', {'type': 'vm'}, headers=headers)} == {running['vmid']}
        if label == 'reader':
            assert not data['capabilities']['memory'] and 'digest' not in data
            http('PUT', running['base'] + '/settings', {'service': 'app', 'settings': json.dumps({'memory': 128})}, headers=headers, expected=403)
            http('POST', running['base'] + '/action', {'action': 'shutdown'}, headers=headers, expected=403)
        else:
            assert data['capabilities']['memory'] and not data['capabilities']['configure']
            settings(running, {'memory': 128, 'swap': 0, 'restart': 'no'}, headers=headers)
            http('PUT', running['base'] + '/settings', {'service': 'app', 'settings': json.dumps({'restart': 'always'})}, headers=headers, expected=403)
    print('PASS guest-scoped reads, denied peer access, denied writes and delegated memory save', flush=True)
    host = json.loads(subprocess.check_output(['docker', 'inspect', configuration(running)['containers'][0]['id']], text=True))[0]['HostConfig']
    assert host['Memory'] == 128 * 1024 * 1024
    for guest in (running, stopped):
        initial = configuration(guest)
        settings(guest, {'memory': 160, 'swap': 0})
        http('PUT', guest['base'] + '/settings', {'service': 'app', 'settings': json.dumps({'memory': 192}), 'digest': initial['digest']}, headers=live._ROOT_HEADERS, expected=500)
        task_result(live.api('set', guest['base'] + '/settings', service='app', settings=json.dumps({'dns': ['1.1.1.1']}), digest=configuration(guest)['digest']))
        if guest is running:
            assert configuration(guest)['services'][0].get('pending', {}).get('dns') == ['1.1.1.1']
            task_result(live.api('set', guest['base'] + '/pending', service='app', revert='dns', digest=configuration(guest)['digest']))
        else:
            assert configuration(guest)['services'][0]['settings']['dns'] == ['1.1.1.1']
        assert configuration(guest)['services'][0]['settings']['memory'] == 160
    print('PASS running/stopped saves, stale digest rejection and pending revert', flush=True)
    # A real worker operation lock, held by a disposable child process. No PVE
    # implementation is mocked or patched to make a conflict happen.
    code = "import sys;sys.path.insert(0,'/usr/local/lib/betterdocker');from dctlib import projects as w\nwith w.operation(sys.argv[1],'settings'):\n print('READY',flush=True)\n sys.stdin.readline()\n"
    child = subprocess.Popen([sys.executable, '-c', code, running['id']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'READY'
        assert configuration(running)['operation']['active']
        task_result(live.api('create', running['base'] + '/action', action='start'), 'failure', 'Another operation is running')
    finally:
        child.communicate('\n', timeout=10)
    assert not configuration(running)['operation'].get('active')
    print('PASS real operation lock rejects a competing task and then releases', flush=True)
    # Native nonfatal hook warnings remain in task history, as for LXC.
    storage = next((row for row in live.api('get', '/nodes/' + node + '/storage', content='snippets') if row.get('active')), None)
    if not storage:
        identifier = 'uxsnip-' + nonce
        state['snippet_storage'] = identifier; save(state)
        live.api('create', '/storage', storage=identifier, type='dir', content='snippets',
                 path='/var/lib/vz/' + state['name'], nodes=node)
        storage = {'storage': identifier}
    volume = storage['storage'] + ':snippets/' + state['name'] + '.sh'
    snippet = Path(subprocess.check_output(['pvesm', 'path', volume], text=True).strip())
    snippet.parent.mkdir(parents=True, exist_ok=True)
    assert not snippet.exists()
    state['snippet'] = str(snippet); save(state)
    marker = '/run/' + state['name']
    snippet.write_text('#!/bin/sh\nif [ "$2" = pre-start ] && [ -f ' + marker + '.fail ]; then echo "UX acceptance start rejected" >&2; exit 1; fi\nif [ "$2" = post-stop ] && [ -f ' + marker + '.warn ]; then echo "UX acceptance stop warning" >&2; exit 1; fi\nexit 0\n')
    snippet.chmod(0o700)
    live.api('set', stopped['base'] + '/config', hookscript=volume, digest=configuration(stopped)['digest'])
    Path(marker + '.fail').touch()
    task_result(live.api('create', stopped['base'] + '/action', action='start'), 'failure', 'UX acceptance start rejected')
    assert configuration(stopped)['status'] == 'stopped'
    Path(marker + '.fail').unlink()
    task_result(live.api('create', stopped['base'] + '/action', action='start'))
    Path(marker + '.warn').touch()
    task_result(live.api('create', stopped['base'] + '/action', action='shutdown', timeout=15), 'OK', 'hookscript error')
    Path(marker + '.warn').unlink()
    live.api('set', stopped['base'] + '/config', delete='hookscript')
    print('PASS start failure/retry and native nonfatal hook warning log', flush=True)
    # Verify the native WARNINGS task status independently of plain hook stderr.
    perl = 'use PVE::RPCEnvironment; PVE::RPCEnvironment->setup_default_cli_env(); my $r=PVE::RPCEnvironment::get(); my $upid=$r->fork_worker(q(betterdockersettings),$ARGV[0],q(root@pam),sub{$r->warn(q(UX acceptance warning));},1); print $upid;'
    upid = subprocess.check_output(['perl', '-e', perl, str(stopped['vmid'])], text=True).strip()
    task_result(upid, 'WARNINGS: 1', 'UX acceptance warning')
    assert configuration(running)['status'] == 'running' and configuration(stopped)['status'] == 'stopped'
    current = {row['id']: row for row in live.api('get', '/cluster/resources', type='vm')}
    for row in before:
        assert row['id'] in current and current[row['id']]['node'] == row['node']
        if row.get('status') == 'running':
            assert current[row['id']]['status'] == 'running', row
    print('PASS existing guests preserved; fixtures ready for final GUI check', flush=True)
    print(json.dumps({'guests': state['guests'], 'users': state['users']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'cross-node', 'cleanup'))
    parser.add_argument('--address', nargs=2, default=['192.0.2.216', '192.0.2.217'])
    parser.add_argument('--bridge', default='vmbr0')
    parser.add_argument('--prefix', type=int, default=20)
    arguments = parser.parse_args()
    {'run': lambda: run(arguments), 'cross-node': cross_node, 'cleanup': cleanup}[arguments.action]()
