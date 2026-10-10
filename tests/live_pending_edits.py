#!/usr/bin/env python3
"""Run on a disposable PVE node. Keep only the named fixture for final UI checks.

Uses native PVE HTTP routes and the installed worker. --cleanup removes its own
DCT, ACL, token and user. It does not touch existing guests or PBS configuration.
"""
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
import live_pools_hooks as live
from live_permissions import http
NODE = worker.current_node()

STATE = Path('/root/ux-pending-edits-fixture.json')


def task_result(upid, expected='OK'):
    node = upid.split(':')[1]
    for _ in range(120):
        result = live.api('get', f'/nodes/{node}/tasks/{upid}/status')
        if result['status'] == 'stopped':
            assert (result.get('exitstatus') == expected) if expected is not None else result.get('exitstatus') != 'OK', result
            return
        time.sleep(1)
    raise AssertionError('Task did not finish')


def cleanup():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    guests = live.api('get', '/cluster/resources', type='vm')
    row = next((row for row in guests if row.get('id') == 'docker/' + state['id']), None)
    if row:
        assert row['name'] == state['name'], 'Fixture identity changed'
        live.task(live.api('create', state['base'] + '/action', action='force-stop'))
        for snapshot in live.api('get', state['base'] + '/snapshots'):
            if snapshot['name'] != 'current':
                live.task(live.api('create', state['base'] + '/snapshot', action='delete', snapname=snapshot['name']))
        live.task(live.api('delete', state['base']))
        folder = worker.ROOT / state['id']
        assert json.loads((folder / 'request.json').read_text())['name'] == state['name']
        assert not (worker.METADATA / (state['id'] + '.json')).exists()
        shutil.rmtree(folder)
    if state.get('user'):
        live.api('set', '/access/acl', path='/vms/' + str(state['vmid']), users=state['user'], roles=state['role'], delete=1)
        live.api('delete', '/access/users/' + state['user'] + '/token/network')
        live.api('delete', '/access/users/' + state['user'])
        live.api('delete', '/access/roles/' + state['role'])
    STATE.unlink()
    print('Removed pending-edits fixture and its access grants')


def configuration(base):
    return live.api('get', base + '/network')


def reachable(ip):
    for _ in range(30):
        result = subprocess.run(['curl', '--noproxy', '*', '-fsS', '--max-time', '2', f'http://{ip}:18080'], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == 'pending-edits':
            return
        time.sleep(1)
    raise AssertionError('HTTP endpoint did not respond: ' + ip)


def run():
    assert not STATE.exists(), 'Clean up the previous fixture first'
    name = 'ux-pending-edits-check'
    deployment = {'name':name, 'mode':'compose', 'bridge':'vmbr0', 'ipv4mode':'static', 'cidr':'192.0.2.216/20',
        'compose': 'services:\n  app:\n    image: busybox:1.37\n    command: sh -c "mkdir -p /www; echo pending-edits > /www/index.html; httpd -p 8080 -h /www; trap \'exit 0\' TERM; while sleep 1; do :; done"\n    ports:\n      - "18080:8080"\n  sidecar:\n    image: busybox:1.37\n    command: sh -c "trap \'exit 0\' TERM; while sleep 1; do :; done"\n'}
    live.task(live.api('create', '/nodes/' + NODE + '/docker', deployment=json.dumps(deployment), start=1))
    row = next(row for row in live.api('get', '/cluster/resources', type='vm') if row.get('name') == name)
    identifier = row['id'].split('/')[-1]
    base = '/nodes/' + NODE + '/docker/' + identifier
    state = {'id':identifier, 'base':base, 'vmid':row['vmid'], 'name':name}
    STATE.write_text(json.dumps(state))
    directory = worker.ROOT / identifier
    reachable('192.0.2.216')
    before = configuration(base)
    containers = {row['id'] for row in live.api('get', base + '/management')['containers']}
    live.task(live.api('set', base + '/network', mtu=1400, digest=before['digest']))
    queued = configuration(base)
    assert queued['mtu'] == 0 and queued['pending'] == {'mtu':1400}, queued
    assert {row['id'] for row in live.api('get', base + '/management')['containers']} == containers
    reachable('192.0.2.216')
    http('PUT', base + '/network', {'mtu':1401, 'digest':before['digest']}, live._ROOT_HEADERS, expected=500)
    assert configuration(base)['pending'] == {'mtu':1400}
    live.task(live.api('set', base + '/network', revert=1, digest=queued['digest']))
    assert configuration(base)['pending'] == {} and configuration(base)['digest'] == before['digest']
    print('PASS running save, effective/pending values, stale digest and Revert', flush=True)

    # A guest-scoped token can edit/revert the network but cannot edit CPU.
    suffix = secrets.token_hex(4)
    user, role = 'uxpending-' + suffix + '@pve', 'UxPending' + suffix
    live.api('create', '/access/users', userid=user, password=secrets.token_urlsafe(24), enable=1)
    live.api('create', '/access/roles', roleid=role, privs='VM.Audit VM.Config.Network')
    live.api('set', '/access/acl', path='/vms/' + str(row['vmid']), users=user, roles=role)
    token = live.api('create', '/access/users/' + user + '/token/network', privsep=0)
    state.update(user=user, role=role); STATE.write_text(json.dumps(state))
    headers = {'Authorization':'PVEAPIToken=' + token['full-tokenid'] + '=' + token['value']}
    pending = http('GET', base + '/network', headers=headers)
    live.task(http('PUT', base + '/network', {'mtu':1400, 'digest':pending['digest']}, headers))
    http('PUT', base + '/settings', {'service':'app', 'settings':json.dumps({'cpus':0.5}), 'digest':configuration(base)['digest']}, headers, expected=403)
    assert live.api('get', base + '/management')['services'][0]['settings']['cpus'] == 0
    live.task(http('PUT', base + '/network', {'revert':1, 'digest':configuration(base)['digest']}, headers))
    print('PASS guest-scoped network edit/Revert and CPU rejection', flush=True)

    live.task(live.api('set', base + '/network', cidr='192.0.2.217/20', mtu=1400, digest=configuration(base)['digest']))
    live.task(live.api('create', base + '/service-action', service='sidecar', action='reboot'))
    assert configuration(base)['pending'] == {'cidr':'192.0.2.217/20', 'mtu':1400}
    reachable('192.0.2.216')
    print('PASS service-only reboot leaves pending project network untouched', flush=True)

    live.task(live.api('create', base + '/action', action='force-stop'))
    original = (directory / 'request.json').read_text()
    with worker.locked(), patch.object(worker, 'rebind_runtime', side_effect=ValueError('Injected interrupted runtime write')):
        try:
            worker.apply_pending_network(identifier, NODE)
        except ValueError as error:
            assert 'Injected' in str(error), error
        else:
            raise AssertionError('Expected injected failure')
    assert (directory / 'request.json').read_text() == original
    assert worker.detail(identifier, NODE)['address'] == '192.0.2.216'
    assert worker.load_pending_network(directory) == {'cidr':'192.0.2.217/20', 'mtu':1400}
    assert not (directory / 'network-transaction.json').exists()
    assert all(row['state'] not in ('running','paused','restarting') for row in worker.container_records(identifier))
    live.task(live.api('create', base + '/action', action='start'))
    reachable('192.0.2.217')
    assert configuration(base)['pending'] == {} and configuration(base)['cidr'] == '192.0.2.217/20'
    print('PASS actual rollback after interruption and successful full-start retry', flush=True)

    live.task(live.api('set', base + '/network', mtu=1401, digest=configuration(base)['digest']))
    live.task(live.api('create', base + '/action', action='reboot', timeout=10))
    assert configuration(base)['pending'] == {} and configuration(base)['mtu'] == 1401
    assert len(live.api('get', base + '/management')['containers']) == 2
    reachable('192.0.2.217')
    # Leave a visible pending edit for the final browser check.
    live.task(live.api('set', base + '/network', mtu=1402, digest=configuration(base)['digest']))
    print('PASS full reboot applies pending network; fixture retained for UI', json.dumps({'vmid':state['vmid'], 'base':base}), flush=True)


if __name__ == '__main__':
    cleanup() if '--cleanup' in sys.argv else run()
