import live_environment as env
#!/usr/bin/env python3
f"Verify create errors and prepare a disposable DCT for final form checks.\n\nRun on {env.node('primary')} after deployment. cleanup removes only the recorded fixture.\nThe HTTP helpers are the same ones used by the other live PVE checks.\n"
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
import live_pools_hooks as live
from live_permissions import http

STATE = Path('/root/ux-forms-fixture.json')
NAME = 'ux-forms-check'


def resources():
    return live.api('get', '/cluster/resources', type='vm')


def cleanup():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    guests = {state['vmid']: NAME}
    if state.get('wizard_vmid'):
        guests[state['wizard_vmid']] = state['wizard_name']
    retained = {state['id']: NAME} if state.get('id') else {}
    for row in resources():
        if row.get('vmid') not in guests:
            continue
        name = guests[row['vmid']]
        assert row.get('name') == name and row['node'] == env.node('primary'), 'Fixture identity changed'
        identifier = row['id'].split('/')[-1]
        base = f"/nodes/{env.node('primary')}/docker/" + identifier
        live.task(live.api('create', base + '/action', action='force-stop'))
        live.task(live.api('delete', base))
        retained[identifier] = name
    for identifier, name in retained.items():
        folder = worker.ROOT / identifier
        if folder.exists():
            assert json.loads((folder / 'request.json').read_text())['name'] == name
            assert not (worker.METADATA / (identifier + '.json')).exists()
            shutil.rmtree(folder)
    STATE.unlink()
    print('PASS disposable forms guest and retained files removed', flush=True)


def prepare():
    assert not STATE.exists(), 'Clean up the previous fixture first'
    before = {row['id'] for row in resources()}
    assert not any(row.get('name') == NAME for row in resources())
    vmid = int(live.api('get', '/cluster/nextid'))
    state = {'vmid': vmid}
    STATE.write_text(json.dumps(state))
    payload = {'name': NAME, 'vmid': vmid, 'mode': 'compose', 'bridge': 'vmbr0',
               'ipv4mode': 'static', 'cidr': f'{env.address(219)}/{env.prefix()}', 'compose': 'services: [broken'}
    http('POST', f"/nodes/{env.node('primary')}/docker", {'deployment': json.dumps(payload), 'start': 0},
         headers=live._ROOT_HEADERS, expected=500)
    assert {row['id'] for row in resources()} == before
    assert not (worker.METADATA / 'ids' / (str(vmid) + '.json')).exists(), 'Validation reserved a guest ID'
    payload['compose'] = 'services:\n  app:\n    image: busybox:1.37\n    command: sleep 3600\n'
    live.task(live.api('create', f"/nodes/{env.node('primary')}/docker", deployment=json.dumps(payload), start=0))
    row = next(row for row in resources() if row.get('name') == NAME)
    state.update(id=row['id'].split('/')[-1], base=f"/nodes/{env.node('primary')}/docker/" + row['id'].split('/')[-1])
    STATE.write_text(json.dumps(state))
    assert row['vmid'] == vmid and row['status'] == 'stopped'
    configuration = live.api('get', state['base'] + '/management')
    assert not any(item['state'] in ('running', 'restarting', 'paused') for item in configuration['containers'])
    live.task(live.api('set', state['base'] + '/settings', service='app',
        settings=json.dumps({'memory': 128, 'swap': 0, 'cpus': 1, 'cpuunits': 512,
                             'dns': ['1.1.1.1', '2001:db8::1'], 'dns_search': ['example.test', 'internal.test']}),
        digest=configuration['digest']))
    configuration = live.api('get', state['base'] + '/management')
    settings = configuration['services'][0]['settings']
    assert settings['dns'] == ['1.1.1.1', '2001:db8::1'] and settings['cpuunits'] == 512
    assert not any(item['state'] in ('running', 'restarting', 'paused') for item in configuration['containers'])
    print('PASS direct create validation, no ID leak, stopped creation and editable resource/DNS values', flush=True)
    print(json.dumps(state), flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['cleanup']:
        cleanup()
    elif sys.argv[1:] == ['record-wizard']:
        state = json.loads(STATE.read_text())
        name = 'forms-layout-' + 'a' * 50
        assert not any(row.get('name') == name for row in resources())
        state.update(wizard_vmid=int(live.api('get', '/cluster/nextid')), wizard_name=name)
        STATE.write_text(json.dumps(state))
        print('Recorded disposable GUI creation ID', state['wizard_vmid'], flush=True)
    else:
        prepare()
