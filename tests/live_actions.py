#!/usr/bin/env python3
'Check action state and guest identity through PVE HTTP on disposable DCTs.\n\nRun on <node> after deployment. Leaves one stopped guest for the final GUI\nchecks. cleanup removes only its recorded guests, backup and pool.\n'
import live_environment as env
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from urllib.parse import quote

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from transfer import ssh_command
import live_pools_hooks as live
from live_permissions import http

STATE = Path('/root/ux-actions-fixture.json')
NAME = 'ux-actions-check'
POOL = 'ux-actions-check'


def resources():
    return live.api('get', '/cluster/resources', type='vm')


def base(row):
    return '/nodes/' + row['node'] + '/docker/' + row['id'].split('/')[-1]


def guest(name):
    rows = [row for row in resources() if row.get('name') == name]
    assert len(rows) == 1, rows
    return rows[0]


def cleanup():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    for row in resources():
        if row.get('name') not in (NAME, NAME + '-clone', NAME + '-restore'):
            continue
        identifier = row['id'].split('/')[-1]
        assert row['vmid'] in state['vmids'], 'Unrecorded guest with fixture name'
        endpoint = base(row)
        current = live.api('get', endpoint + '/management')
        if current.get('protection'):
            live.api('set', endpoint + '/config', protection=0, digest=current['digest'])
        live.task(live.api('create', endpoint + '/action', action='force-stop'))
        for snapshot in live.api('get', endpoint + '/snapshots'):
            if snapshot['name'] != 'current':
                live.task(live.api('create', endpoint + '/snapshot', action='delete', snapname=snapshot['name']))
        live.task(live.api('delete', endpoint))
        path = '/var/lib/betterdocker/projects/' + identifier
        # Confirm the retained directory identity before removing test data.
        command = ['python3', '-c', 'import json,shutil; from pathlib import Path; p=Path(' + repr(path) + '); assert json.loads((p/"request.json").read_text())["name"] == ' + repr(row['name']) + '; shutil.rmtree(p)']
        subprocess.run(command if row['node'] == env.node('primary') else ssh_command(row['node']) + [shlex.join(command)], check=True)
    retained = worker.ROOT / state.get('id', 'missing')
    if retained.exists():
        assert json.loads((retained / 'request.json').read_text())['name'] == NAME
        assert not (worker.METADATA / (state['id'] + '.json')).exists()
        shutil.rmtree(retained)
    if state.get('archive'):
        live.api('delete', f"/nodes/{env.node('primary')}/storage/local/content/" + quote(state['archive'], safe=''))
    if any(row['poolid'] == POOL for row in live.api('get', '/pools')):
        live.api('delete', '/pools/' + POOL)
    STATE.unlink()
    print('PASS removed disposable action guests, backup, pool and retained files', flush=True)


def run():
    assert not STATE.exists(), 'Clean up the previous fixture first'
    assert not any(row.get('name', '').startswith(NAME) or row.get('address') in (env.address(216), env.address(217), env.address(218)) for row in resources())
    assert not any(row['poolid'] == POOL for row in live.api('get', '/pools'))
    state = {'vmids': []}
    STATE.write_text(json.dumps(state))
    live.api('create', '/pools', poolid=POOL)
    payload = {'name': NAME, 'mode': 'compose', 'bridge': 'vmbr0', 'ipv4mode': 'static', 'cidr': f'{env.address(216)}/{env.prefix()}',
               'compose': 'services:\n  app:\n    image: busybox:1.37\n    command: sh -c "trap \'exit 0\' TERM; while sleep 1; do :; done"\n'}
    # Reserve explicit VMIDs before mutations so interrupted runs can clean up.
    vmid = int(live.api('get', '/cluster/nextid'))
    state['vmids'].append(vmid); STATE.write_text(json.dumps(state))
    payload['vmid'] = vmid
    live.task(live.api('create', f"/nodes/{env.node('primary')}/docker", deployment=json.dumps(payload), pool=POOL, start=1))
    row = guest(NAME); endpoint = base(row)
    state.update(id=row['id'].split('/')[-1], base=endpoint, name=NAME, vmid=vmid)
    STATE.write_text(json.dumps(state))
    caps = live.api('get', endpoint + '/capabilities')
    assert caps['active_containers'] and not caps['template'] and not caps['ha']['managed']
    assert caps['capabilities']['clone'] and caps['capabilities']['console']
    config = live.api('get', endpoint + '/management')
    live.task(live.api('set', endpoint + '/network', mtu=1405, digest=config['digest']))
    assert live.api('get', endpoint + '/capabilities')['pending_network'] == {'mtu': 1405}
    live.task(live.api('set', endpoint + '/network', revert=1, digest=live.api('get', endpoint + '/management')['digest']))
    live.task(live.api('create', endpoint + '/action', action='reboot'))
    live.task(live.api('create', endpoint + '/action', action='shutdown'))
    assert live.api('get', endpoint + '/capabilities')['active_containers'] == 0
    live.api('set', endpoint + '/config', protection=1, digest=live.api('get', endpoint + '/management')['digest'])
    assert live.api('get', endpoint + '/capabilities')['protection'] == 1
    http('DELETE', endpoint, headers=live._ROOT_HEADERS, expected=500)
    live.api('set', endpoint + '/config', protection=0, digest=live.api('get', endpoint + '/management')['digest'])
    for suffix, address in [('clone', env.address(217)), ('restore', env.address(218))]:
        destination = int(live.api('get', '/cluster/nextid'))
        state['vmids'].append(destination); STATE.write_text(json.dumps(state))
        deployment = {'name': NAME + '-' + suffix, 'bridge': 'vmbr0', 'address': address, 'cidr': address + '/20'}
        if suffix == 'clone':
            live.task(live.api('create', endpoint + '/clone', newid=destination, pool=POOL,
                deployment=json.dumps(deployment), digest=live.api('get', endpoint + '/management')['digest']))
        else:
            live.task(live.api('create', f"/nodes/{env.node('primary')}/vzdump", vmid=vmid, storage='local', mode='stop'))
            backups = live.api('get', f"/nodes/{env.node('primary')}/storage/local/content", content='backup', vmid=vmid)
            archive = max(backups, key=lambda item: item.get('ctime', 0))['volid']
            assert 'vzdump-docker-' in archive
            state['archive'] = archive; STATE.write_text(json.dumps(state))
            live.task(live.api('create', f"/nodes/{env.node('primary')}/docker", newid=destination, archive=archive, pool=POOL,
                deployment=json.dumps(deployment)))
        created = guest(NAME + '-' + suffix)
        assert created['vmid'] == destination and created['pool'] == POOL and created['type'] == 'docker'
    # Migration keeps the resource ID and VMID; native ResourceTree can reselect it.
    clone = guest(NAME + '-clone')
    live.task(live.api('create', base(clone) + '/migrate', target=env.node('secondary'), digest=live.api('get', base(clone) + '/management')['digest']))
    migrated = guest(NAME + '-clone')
    assert migrated['node'] == env.node('secondary') and migrated['id'] == clone['id'] and migrated['vmid'] == clone['vmid'] and migrated['pool'] == POOL
    live.task(live.api('create', base(migrated) + '/template', digest=live.api('get', base(migrated) + '/management')['digest']))
    assert live.api('get', base(migrated) + '/capabilities')['template']
    tasks = live.api('get', '/cluster/tasks')
    types = {task['type'] for task in tasks if str(task.get('id')) in {str(value) for value in state['vmids']}}
    assert {'betterdockercreate', 'betterdockerclone', 'betterdockerrestore', 'betterdockermigrate', 'betterdockertemplate'} <= types, types
    pool = live.api('get', '/pools/' + POOL)
    assert {row['vmid'] for row in pool['members'] if row['type'] == 'docker'} == set(state['vmids'])
    live.task(live.api('create', endpoint + '/snapshot', action='create', snapname='actions', description='Action confirmation check'))
    ha = live.api('get', '/cluster/ha/resources')
    for resource in ha:
        if resource['sid'] == 'docker:115':
            ha_row = next(row for row in resources() if row.get('type') == 'docker' and row['vmid'] == 115)
            assert live.api('get', base(ha_row) + '/capabilities')['ha']['managed']
    print('PASS HTTP power/pending/protection, clone/backup/restore, migration identity, templates, pools and native task VMIDs', flush=True)
    print(json.dumps(state), flush=True)


if __name__ == '__main__':
    cleanup() if sys.argv[1:] == ['cleanup'] else run()
