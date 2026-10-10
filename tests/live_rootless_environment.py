#!/usr/bin/env python3
"""Temporary placement changes explicitly approved for the rootless test cluster."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
from live_pools_hooks import api, task

STATE = Path('/root/betterdocker-rootless-baseline/placement.json')
VMIDS = {106, 111, 113, 114, 117, 118}


def run(restore=False):
    if worker.current_node() != 'pve1':
        raise ValueError('Run only on the approved test coordinator pve1')
    if not STATE.exists():
        if restore:
            raise ValueError('Placement baseline is missing')
        guests = [i for i in worker.summaries() if i['vmid'] in VMIDS]
        assert len(guests) == 6 and all(i['node'] == 'pve2' and i['status'] == 'stopped' for i in guests)
        jobs = api('get', '/cluster/replication')
        saved = next(i for i in jobs if i['id'] == '115-0')
        STATE.write_text(json.dumps({'guests': guests, 'replication': saved}, indent=2))
        STATE.chmod(0o600)
    value = json.loads(STATE.read_text())
    if not restore:
        api('set', '/cluster/replication/115-0', disable=1)
    for original in value['guests']:
        info = next(i for i in worker.summaries() if i['id'] == original['id'])
        destination = original['node'] if restore else 'pve1'
        if info['node'] == destination:
            continue
        assert info['node'] in ('pve1', 'pve2') and info['status'] == 'stopped'
        base = '/nodes/' + info['node'] + '/docker/' + info['id']
        task(api('create', base + '/migrate', target=destination, storage='local-lvm'))
        result = next(i for i in worker.summaries() if i['id'] == original['id'])
        assert result['vmid'] == original['vmid'] and result['node'] == destination and result['status'] == 'stopped'
        print('PASS preserved guest', original['vmid'], 'on', destination, flush=True)
    if restore:
        api('set', '/cluster/replication/115-0', disable=value['replication'].get('disable', 0))
        print('PASS original placement and replication enable state restored', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', action='store_true')
    run(parser.parse_args().restore)
