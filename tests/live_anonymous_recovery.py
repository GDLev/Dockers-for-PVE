#!/usr/bin/env python3
"""Check anonymous-volume cleanup using real Docker and a recovery journal.

Run as root on an empty, approved test runtime. The test uses a temporary control
directory and its own volumes. It does not exercise PVE API authorization.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, '/usr/local/lib/betterdocker')
import runtime
from dctlib import projects as worker, service_recreate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0, 'Run on an approved test node as root'
    assert not args.report.exists() and not args.report.is_symlink()
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def docker(*command, check=True):
        return runtime.run(['docker', *command], text=True, capture_output=True, check=check)

    def inspect(name):
        return json.loads(docker('inspect', name).stdout)[0]

    assert not docker('ps', '-aq').stdout.strip(), 'Selected runtime must have no containers'
    token, identifier = uuid.uuid4().hex, uuid.uuid4().hex
    original_name = 'bd-anon-kept-' + token[:12]
    replacement_name = 'bd-anon-recovery-' + token[:12]
    containers, volumes = [], []
    report = dict(status='failed', revision=args.revision, run=token,
                  identifier=identifier, containers=containers, volumes=volumes,
                  names=[original_name, replacement_name])

    def save():
        worker.write_json(args.report, report)

    def create(name, mounts, command, transaction=False):
        options = ['create', '--name', name, '--network', 'none', '--memory', '32m',
                   '--memory-swap', '32m', '--cpus', '.1', '--pids-limit', '16',
                   '--label', 'betterdocker.id=' + identifier,
                   '--label', 'com.docker.compose.service=app',
                   '--label', 'dev.gdlev.acceptance-run=' + token]
        if transaction:
            options += ['--label', 'betterdocker.settings-transaction=' + token]
        for mount in mounts:
            options += ['--mount', mount]
        cid = docker(*options, 'busybox:1.37', *command).stdout.strip()
        containers.append(cid)
        save()
        row = inspect(cid)
        for mount in row['Mounts']:
            if mount['Type'] == 'volume' and mount['Name'] not in volumes:
                volumes.append(mount['Name'])
                save()
        return row

    save()
    try:
        original = create(original_name, ['type=volume,target=/kept'],
                          ['sh', '-c', 'printf own-proof >/kept/marker'])
        docker('start', '-a', original['Id'])
        old = next(m['Name'] for m in original['Mounts'] if m['Destination'] == '/kept')
        replacement = create(replacement_name,
                             ['type=volume,source=' + old + ',target=/kept',
                              'type=volume,target=/new'], ['true'], transaction=True)
        new = next(m['Name'] for m in replacement['Mounts'] if m['Destination'] == '/new')
        report.update(existing_volume=old, new_volume=new)
        save()
        # Reused data must survive even without another container holding it.
        docker('rm', original['Id'])
        with tempfile.TemporaryDirectory(prefix='bd-anon-recovery-', dir='/var/lib/betterdocker') as folder:
            previous = worker.ROOT
            worker.ROOT = Path(folder)
            try:
                directory = worker.ROOT / identifier
                directory.mkdir(mode=0o700)
                journal = dict(id=identifier, service='*', token=token, phase='preparing',
                               entries=[dict(name=replacement_name, created=replacement['Id'],
                                             source=None, body={},
                                             backup='bd-settings-' + identifier + '-' + token + '-0',
                                             tag='betterdocker-settings/' + identifier + ':' + token + '-0')],
                               files={name: None for name in service_recreate.FILES})
                worker.write_json(directory / service_recreate.JOURNAL, journal)
                assert service_recreate.recover(identifier)
            finally:
                worker.ROOT = previous
        assert docker('inspect', replacement['Id'], check=False).returncode != 0
        kept = json.loads(docker('volume', 'inspect', old).stdout)[0]
        assert (Path(kept['Mountpoint']) / 'marker').read_text() == 'own-proof'
        orphaned = docker('volume', 'inspect', new, check=False).returncode == 0
        report.update(existing_data_preserved=True, new_anonymous_volume_orphaned=orphaned,
                      status='failed' if orphaned else 'passed')
        save()
        print('Existing data preserved; new anonymous volume orphaned:', orphaned, flush=True)
    finally:
        for cid in reversed(containers):
            result = docker('inspect', cid, check=False)
            if result.returncode == 0:
                assert json.loads(result.stdout)[0]['Config']['Labels']['dev.gdlev.acceptance-run'] == token
                docker('rm', '-v', cid)
        for name in volumes:
            if docker('volume', 'inspect', name, check=False).returncode == 0:
                assert not docker('ps', '-aq', '--filter', 'volume=' + name).stdout.strip()
                docker('volume', 'rm', name)
        assert not docker('ps', '-aq').stdout.strip()
        report['cleanup'] = 'Only registered containers and exact volume IDs removed'
        save()
        print('Own inactive-container and volume fixtures removed', flush=True)
    assert report['status'] == 'passed'


if __name__ == '__main__':
    main()
