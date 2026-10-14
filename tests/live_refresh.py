import live_environment as env
#!/usr/bin/env python3
f"Prepare, change and remove one disposable DCT for stable-refresh GUI checks.\n\nRun on {env.node('primary')} after deployment. No changes to existing guests or PBS.\n"
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
import live_pools_hooks as live

STATE = Path('/root/ux-refresh-fixture.json')


def run(action):
    if action == 'prepare':
        assert not STATE.exists(), 'Remove the previous fixture first'
        guests = live.api('get', f"/nodes/{env.node('primary')}/docker")
        assert not any(row.get('address') == env.address(216) for row in guests)
        ports = ''.join(f'      - "{18400 + index}:8080"\n' for index in range(24))
        deployment = {'name': 'ux-refresh-check', 'mode': 'compose', 'bridge': 'vmbr0',
            'ipv4mode': 'static', 'cidr': f'{env.address(216)}/{env.prefix()}',
            'compose': 'services:\n  app:\n    image: busybox:1.37\n    command: sh -c "trap \'exit 0\' TERM; while sleep 1; do :; done"\n    ports:\n' + ports +
            '  sidecar:\n    image: busybox:1.37\n    command: sh -c "trap \'exit 0\' TERM; while sleep 1; do :; done"\n'}
        live.task(live.api('create', f"/nodes/{env.node('primary')}/docker", deployment=json.dumps(deployment), start=1))
        row = next(row for row in live.api('get', '/cluster/resources', type='vm') if row.get('name') == deployment['name'])
        identifier = row['id'].split('/')[-1]
        state = {'id': identifier, 'base': f"/nodes/{env.node('primary')}/docker/" + identifier, 'name': row['name'], 'vmid': row['vmid']}
        STATE.write_text(json.dumps(state))
        live.task(live.api('create', state['base'] + '/snapshot', action='create', snapname='refresh', description='Before metadata edit'))
        print(json.dumps(state), flush=True)
        return
    state = json.loads(STATE.read_text())
    base = state['base']
    row = live.api('get', base + '/management')
    assert row['id'] == state['id'] and row['name'] == state['name'], 'Fixture identity changed'
    if action == 'change':
        live.task(live.api('set', base + '/network', mtu=1405, digest=row['digest']))
        row = live.api('get', base + '/management')
        assert row['pending_network'] == {'mtu': 1405}
        live.task(live.api('set', base + '/settings', service='app', settings=json.dumps({'memory': 128}), digest=row['digest']))
        print('PASS actual task completion, pending network and resource changes')
    elif action == 'memory':
        app = next(service for service in row['services'] if service['name'] == 'app')
        memory = 128 if app.get('pending', {}).get('memory') == 192 else 192
        live.task(live.api('set', base + '/settings', service='app', settings=json.dumps({'memory': memory}), digest=row['digest']))
        print('PASS resource changed while editor remains open')
    elif action == 'revert':
        live.task(live.api('set', base + '/network', revert=1, digest=row['digest']))
        print('PASS pending endpoint reverted')
    elif action == 'cleanup':
        live.task(live.api('create', base + '/action', action='force-stop'))
        for snapshot in live.api('get', base + '/snapshots'):
            if snapshot['name'] != 'current':
                live.task(live.api('create', base + '/snapshot', action='delete', snapname=snapshot['name']))
        live.task(live.api('delete', base))
        folder = worker.ROOT / state['id']
        assert json.loads((folder / 'request.json').read_text())['name'] == state['name']
        assert not (worker.METADATA / (state['id'] + '.json')).exists()
        assert not worker.container_records(state['id'])
        shutil.rmtree(folder)
        STATE.unlink()
        print('PASS disposable refresh guest and snapshots removed')
    else:
        raise ValueError('Expected prepare, change, memory, revert or cleanup')


if __name__ == '__main__':
    run(sys.argv[1])
