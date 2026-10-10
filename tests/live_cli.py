#!/usr/bin/env python3
"""Check the installed dct CLI on a disposable PVE node. Creates two temporary DCTs."""
import argparse
import json
import subprocess


def run(*args):
    result = subprocess.run(['dct', *map(str, args)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(' '.join(map(str, args)) + '\n' + result.stdout + '\n' + result.stderr)
    return result.stdout


def status(vmid):
    return json.loads(run('status', vmid, '--output-format', 'json'))['status']


def guests():
    return json.loads(run('list', '--cluster', 1, '--output-format', 'json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vmid', type=int, default=150)
    parser.add_argument('--clone-id', type=int, default=151)
    parser.add_argument('--cidr', required=True, help='Unused address for the test guest')
    parser.add_argument('--clone-cidr', required=True, help='Unused address for the clone')
    parser.add_argument('--bridge', default='vmbr0')
    args = parser.parse_args()
    before = guests()
    ids = (args.vmid, args.clone_id)
    if len(set(ids)) != 2 or any(g['vmid'] in ids for g in before):
        raise ValueError('Choose two unused guest IDs')
    assert 'USAGE: dct set <vmid>' in run('help', 'set')
    assert 'dct task status <upid>' in run('help', '--verbose', 0)
    print('PASS native help and command syntax', flush=True)
    try:
        run('create', args.vmid, 'nginx:stable', '--hostname', 'cli-check',
            '--net0', f'bridge={args.bridge},ip={args.cidr}', '--ports', '8080:80')
        assert status(args.vmid) == 'stopped'
        print('PASS positional source and stopped default', flush=True)
        run('set', args.vmid, '--onboot', 0, '--memory', 128, '--cpulimit', 0.5, '--tags', 'cli-check')
        config = json.loads(run('config', args.vmid, '--output-format', 'json'))
        assert config['memory'] == 128 and config['cpulimit'] == 0.5 and config['onboot'] == 0
        print('PASS native options and numeric conversion', flush=True)
        run('start', args.vmid)
        assert run('status', args.vmid).strip() == 'status: running'
        run('shutdown', args.vmid, '--timeout', 15)
        assert status(args.vmid) == 'stopped'
        print('PASS power operations and status format', flush=True)
        run('snapshot', args.vmid, 'baseline', '--description', 'CLI acceptance')
        assert 'baseline' in run('listsnapshot', args.vmid)
        run('rollback', args.vmid, 'baseline')
        assert status(args.vmid) == 'stopped'
        run('delsnapshot', args.vmid, 'baseline')
        print('PASS snapshot commands', flush=True)
        run('template', args.vmid)
        run('clone', args.vmid, args.clone_id, '--hostname', 'cli-copy',
            '--net0', f'bridge={args.bridge},ip={args.clone_cidr}', '--full', 1)
        assert status(args.clone_id) == 'stopped'
        print('PASS template and full clone', flush=True)
    finally:
        for guest in guests():
            if guest['vmid'] in ids and guest['name'] in ('cli-check', 'cli-copy'):
                if guest['status'] != 'stopped':
                    run('stop', guest['vmid'])
                for snapshot in json.loads(run('listsnapshot', guest['vmid'], '--output-format', 'json')):
                    if snapshot['name'] != 'current':
                        run('delsnapshot', guest['vmid'], snapshot['name'])
                run('destroy', guest['vmid'])
    assert {(g['vmid'], g['node'], g['name'], g['status']) for g in before} == {
        (g['vmid'], g['node'], g['name'], g['status']) for g in guests()}
    print('PASS destroy; original guests unchanged', flush=True)


if __name__ == '__main__':
    main()
