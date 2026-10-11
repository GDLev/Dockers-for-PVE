#!/usr/bin/env python3
"""Verify budgets and Docker caps with a disposable replicated DCT on a PVE node."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def call(*args, fail=False):
    if args[0] == 'docker':
        sys.path.insert(0, '/usr/local/lib/betterdocker')
        import runtime
        result = runtime.run(list(map(str, args)), text=True, capture_output=True)
    else:
        result = subprocess.run(list(map(str, args)), text=True, capture_output=True)
    if fail:
        assert result.returncode != 0, 'Unexpected success: ' + str(args)
        return result.stdout + result.stderr
    if result.returncode: raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def resources():
    return json.loads(call('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cidr', required=True)
    args = parser.parse_args()
    before = resources()
    used = {int(row['vmid']) for row in before}
    vmid = next(value for value in range(900, 1000) if value not in used)
    identifier = None
    config = {'services': {
        'web': {'image': 'busybox:1.37', 'command': ['sh', '-c', 'while :; do echo budget-test; sleep 1; done'],
                'cpus': .1, 'mem_limit': '16m', 'pids_limit': 16, 'deploy': {'replicas': 2}},
        'db': {'image': 'busybox:1.37', 'command': ['sh', '-c', 'while :; do sleep 1; done'],
               'cpus': .2, 'mem_limit': '32m', 'pids_limit': 32},
    }}
    with tempfile.TemporaryDirectory(prefix='bd-limit-test-') as folder:
        source = Path(folder) / 'compose.json'
        source.write_text(json.dumps(config))
        try:
            # Exercise the delegated policy after real Docker Compose normalization.
            for setting, message in (({'oom_kill_disable': True}, 'OOM protection'),
                                     ({'oom_score_adj': -1000}, 'OOM protection'),
                                     ({'logging': {'driver': 'json-file', 'options': {'max-size': '0'}}}, 'Delegated logs')):
                request = Path(folder) / 'unsafe-request.json'
                request.write_text(json.dumps({'name': 'policy-check', 'mode': 'compose', 'bridge': 'vmbr0',
                    'cidr': args.cidr, 'compose': json.dumps({'services': {'app': {'image': 'busybox:1.37', **setting}}})}))
                error = call('env', 'BETTERDOCKER_TRUSTED=0', 'BETTERDOCKER_TASK_USER=root@pam',
                    '/usr/local/lib/betterdocker/provision.py', 'preview', request, fail=True)
                assert message in error, error
            call('dct', 'create', vmid, source, '--hostname', 'resource-budget-test', '--net0', 'bridge=vmbr0,ip=' + args.cidr, '--start', 0)
            row = next(row for row in resources() if int(row['vmid']) == vmid)
            identifier = row['id'].split('/')[-1]
            project = Path('/var/lib/betterdocker/projects') / identifier
            node = row['node']
            path = f'/nodes/{node}/docker/{identifier}'
            call('dct', 'limits', vmid, '--cpus', .4, '--memory', 64, '--pids', 64)
            budget = json.loads(call('pvesh', 'get', path + '/limits', '--output-format', 'json'))
            assert {key: budget[key] for key in ('cpus', 'memory', 'pids')} == {'cpus': .4, 'memory': 64, 'pids': 64}
            old = (project / 'compose.runtime.json').read_bytes()
            error = call('dct', 'set', vmid, '--service', 'web', '--cpulimit', .3, fail=True)
            assert 'budget exceeded' in error, error
            assert old == (project / 'compose.runtime.json').read_bytes()
            call('dct', 'set', vmid, '--service', 'web', '--memory', 0, fail=True)
            request = Path(folder) / 'limits.json'; request.write_text(json.dumps({'cpus': 0, 'memory': 0, 'pids': 0}))
            error = call('env', 'BETTERDOCKER_TASK_SYS_MODIFY=0', '/usr/local/lib/betterdocker/provision.py',
                         'project-limits', identifier, node, request, fail=True)
            assert 'Permission denied' in error, error
            # Rollback keeps the current policy rather than restoring older administrative limits.
            call('dct', 'snapshot', vmid, 'within-budget')
            call('dct', 'rollback', vmid, 'within-budget')
            assert json.loads((project / 'limits.json').read_text())['cpus'] == .4
            call('dct', 'delsnapshot', vmid, 'within-budget')
            call('dct', 'start', vmid)
            ids = call('docker', 'ps', '-aq', '--filter', 'label=betterdocker.id=' + identifier).split()
            records = json.loads(call('docker', 'inspect', *ids))
            assert len(records) == 3
            assert all(record['State']['Running'] for record in records)
            assert sum(record['HostConfig']['NanoCpus'] for record in records) == 400000000
            assert sum(record['HostConfig']['Memory'] for record in records) == 64 * 1048576
            assert sum(record['HostConfig']['PidsLimit'] for record in records) == 64
            assert all(record['HostConfig']['LogConfig']['Config']['max-size'] == '10m' and
                       record['HostConfig']['LogConfig']['Config']['max-file'] == '3' for record in records)
            call('dct', 'limits', vmid, '--memory', 128, fail=True)
            print('PASS aggregate replica limits, real Docker caps, bounded logs, delegated policy checks and snapshot rollback', flush=True)
        finally:
            if identifier:
                call('dct', 'stop', vmid)
                call('dct', 'destroy', vmid)
                shutil.rmtree(Path('/var/lib/betterdocker/projects') / identifier)
    assert {int(row['vmid']) for row in resources()} == used
    print('PASS temporary guest removed; original inventory preserved', flush=True)


if __name__ == '__main__': main()
