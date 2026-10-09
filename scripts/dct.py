#!/usr/bin/env python3
"""Manage DCT guests through the installed PVE API. Run as root on a PVE node."""
import argparse
import json
import re
import subprocess
import sys
import time


def api(method, path, **params):
    command = ['pvesh', method, path, '--output-format', 'json']
    for key, value in params.items():
        if value is not None:
            command += ['--' + key, json.dumps(value) if isinstance(value, (dict, list)) else str(int(value)) if isinstance(value, bool) else str(value)]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('PVE API request failed')
    return json.loads(result.stdout) if result.stdout.strip() else None


def guest(vmid):
    matches = [item for item in api('get', '/cluster/resources', type='vm')
               if item['type'] == 'docker' and int(item['vmid']) == vmid]
    if len(matches) != 1:
        raise ValueError(f'DCT {vmid} not found')
    item = matches[0]
    return '/nodes/' + item['node'] + '/docker/' + item['id'].split('/')[-1]


def task_path(upid):
    parts = upid.split(':')
    if len(parts) < 3 or parts[0] != 'UPID' or not parts[1]:
        raise ValueError('Expected a PVE task UPID')
    return '/nodes/' + parts[1] + '/tasks/' + upid


def wait_task(upid):
    path = task_path(upid)
    while True:
        result = api('get', path + '/status')
        if result['status'] != 'running':
            status = result.get('exitstatus', 'Task failed')
            if status != 'OK' and not re.fullmatch(r'WARNINGS: \d+', status):
                raise RuntimeError(status)
            if status != 'OK':
                print(status, file=sys.stderr)
            return result
        time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-wait', action='store_true', help='print the task UPID without waiting')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='list DCTs across the cluster')
    create = commands.add_parser('create', help='create from a deployment JSON file')
    create.add_argument('vmid', type=int); create.add_argument('--node', required=True)
    create.add_argument('--file', required=True); create.add_argument('--pool'); create.add_argument('--start', action='store_true')
    for name in ('status', 'config', 'pending', 'network', 'snapshots', 'start', 'shutdown', 'stop', 'reboot', 'destroy', 'migrate', 'clone', 'snapshot', 'rollback', 'set'):
        cmd = commands.add_parser(name)
        cmd.add_argument('vmid', type=int)
        if name in ('shutdown', 'reboot'): cmd.add_argument('--timeout', type=int, default=60)
        if name == 'destroy': cmd.add_argument('--confirm', type=int, required=True, help='repeat the guest ID')
        if name == 'migrate': cmd.add_argument('target'); cmd.add_argument('--storage')
        if name == 'clone':
            cmd.add_argument('newid', type=int); cmd.add_argument('--file', required=True, help='destination deployment JSON'); cmd.add_argument('--pool')
        if name in ('snapshot', 'rollback'): cmd.add_argument('name')
        if name == 'set': cmd.add_argument('--file', required=True, help='configuration changes as JSON')
    task = commands.add_parser('task', help='inspect or stop a PVE task')
    task.add_argument('action', choices=['status', 'log', 'wait', 'stop']); task.add_argument('upid')
    args = parser.parse_args(argv)
    def read_json(path):
        with open(path) as stream: value = json.load(stream)
        if not isinstance(value, dict): raise ValueError('The JSON file must contain an object')
        return value
    if args.command == 'list':
        result = [i for i in api('get', '/cluster/resources', type='vm') if i['type'] == 'docker']
    elif args.command == 'task':
        base = task_path(args.upid)
        if args.action == 'wait': result = wait_task(args.upid)
        elif args.action == 'stop': result = api('delete', base)
        else: result = api('get', base + '/' + args.action)
    elif args.command == 'create':
        data = read_json(args.file); data['vmid'] = args.vmid
        result = api('create', '/nodes/' + args.node + '/docker', deployment=data, pool=args.pool, start=int(args.start))
    else:
        base = guest(args.vmid)
        reads = {'status': 'management', 'config': 'config', 'pending': 'pending', 'network': 'network', 'snapshots': 'snapshots'}
        if args.command in reads:
            result = api('get', base + '/' + reads[args.command])
        elif args.command in ('start', 'shutdown', 'stop', 'reboot'):
            result = api('create', base + '/action', action='force-stop' if args.command == 'stop' else args.command,
                         timeout=getattr(args, 'timeout', None))
        else:
            digest = api('get', base + '/management')['digest']
            if args.command == 'destroy':
                if args.confirm != args.vmid: raise ValueError('--confirm must match the guest ID')
                result = api('delete', base)
            elif args.command == 'migrate':
                result = api('create', base + '/migrate', target=args.target, storage=args.storage, digest=digest)
            elif args.command == 'clone':
                result = api('create', base + '/clone', newid=args.newid, deployment=read_json(args.file), pool=args.pool, digest=digest)
            elif args.command in ('snapshot', 'rollback'):
                result = api('create', base + '/snapshot', action='create' if args.command == 'snapshot' else 'restore', snapname=args.name)
            else:
                result = api('set', base + '/config', **dict(read_json(args.file), digest=digest))
    print(json.dumps(result, indent=2))
    if isinstance(result, str) and result.startswith('UPID:') and not args.no_wait:
        wait_task(result)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped waiting. The PVE task continues; use dct task stop UPID to cancel it.', file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
