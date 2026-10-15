#!/usr/bin/env python3
"""Check native pool backup selection using an owned management-test manifest."""
import argparse
import json
from pathlib import Path

import live_environment as env
from live_pools_hooks import api, task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    state = json.loads(args.manifest.read_text())
    guest = next(row for row in api('get', '/cluster/resources', type='vm')
                 if row['vmid'] == state['vmid'])
    assert guest['id'] == 'docker/' + state['id'] and guest['name'] == state['name']
    node, storage = guest['node'], env.backup_storage()
    job = state['name'] + '-backup'
    assert not any(row['id'] == job for row in api('get', '/cluster/backup'))
    path = f'/nodes/{node}/storage/{storage}/content'
    before = {row['volid'] for row in api('get', path, content='backup', vmid=state['vmid'])}
    created_job = False
    try:
        api('create', '/cluster/backup', id=job, schedule='sat 03:00', enabled=0,
            pool=state['pool'], storage=storage, mode='snapshot')
        created_job = True
        config = api('get', '/cluster/backup/' + job)
        assert config['pool'] == state['pool'] and config['enabled'] == 0
        task(api('create', f'/nodes/{node}/vzdump', pool=state['pool'], storage=storage, mode='snapshot'))
        created = [row for row in api('get', path, content='backup', vmid=state['vmid'])
                   if row['volid'] not in before]
        assert len(created) == 1, 'Cannot identify the backup created by this fixture'
        archive = created[0]
        state.setdefault('archives', []).append(dict(node=node, storage=storage, volid=archive['volid']))
        args.manifest.write_text(json.dumps(state))
        args.manifest.chmod(0o600)
        assert archive['subtype'] == 'docker' and archive['vmid'] == state['vmid']
        print('PASS native pool backup selection produced the owned DCT PBS archive', flush=True)
    finally:
        if created_job:
            api('delete', '/cluster/backup/' + job)


if __name__ == '__main__':
    main()
