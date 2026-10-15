#!/usr/bin/env python3
"""Signed package, repair and uninstall/reinstall on one disposable PVE node.

Requires artifacts built from this checkout and a signing key already trusted
on the target. Creates one owned DCT and removes only that DCT after verification.
Existing containers must retain their identity, state and start timestamp.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import installer
sys.path.pop(0)
sys.path.insert(0, '/usr/local/lib/betterdocker')
from acceptance_report import Report
import live_environment as env
import live_guest_lifecycle as lifecycle
import live_security_acceptance as live
from live_firewall import remote


def containers(node):
    ids = remote(node, ['docker', 'ps', '-aq'], check=True).stdout.split()
    records = json.loads(remote(node, ['docker', 'inspect', *ids], check=True).stdout) if ids else []
    return {row['Id']: [row['State']['Status'], row['State']['StartedAt']] for row in records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--replace-test-release', action='store_true', help='Explicitly permit replacing an existing development version with different signed test bytes')
    args = parser.parse_args()
    env.require_node(args.node)
    if args.report.exists():
        raise ValueError('Journal already exists; inspect its owned fixtures before retrying')
    source = Path(env.source())
    package = Path(env.package())
    files = [package, package.parent / 'RELEASE.json', package.parent / 'RELEASE.sig']
    for path in files:
        if not path.is_file():
            raise ValueError('Missing signed artifact: ' + str(path))
    report = Report(args.report, args.revision, {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    report.data['working_tree'] = True
    cluster = installer.Cluster()
    original = live.resources()
    item = report.plan_guest(args.node, int(live.api('get', '/cluster/nextid')), 'package-check-' + report.data['run'][:8])
    compose = {'services': {'app': {'image': 'busybox:1.37', 'pull_policy': 'never', 'init': True,
        'command': ['sh', '-c', "trap 'exit 0' TERM; while true; do sleep 1; done"],
        'cpus': .5, 'mem_limit': '128m', 'pids_limit': 64, 'volumes': ['./data:/data'],
        'labels': {'dev.gdlev.acceptance-run': report.data['run']}}}}
    helper = '/usr/local/lib/betterdocker/runtime.py'
    def command(argv, timeout=600):
        result = cluster.run(args.node, argv, text=True, timeout=timeout)
        with args.report.with_suffix('.log').open('a') as handle:
            handle.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(shlex.join(argv[:3]) + ': ' + (result.stdout + result.stderr)[-2000:])
        return result
    def stable():
        assert containers(args.node) == before, 'Container identity/state/start time changed'
        identity = json.loads(command([helper, 'identity']).stdout)
        assert identity == runtime, 'Runtime identity changed'
        inspected = json.loads(command([helper, 'docker', 'inspect', cid]).stdout)[0]
        assert 'ALL' in inspected['HostConfig']['CapDrop']
        status = command([helper, 'docker', 'exec', cid, 'cat', '/proc/1/status']).stdout
        fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
        assert fields['NoNewPrivs'].strip() == '1' and fields['Seccomp'].strip() == '2'
        if runtime['mode'] == 'rootful':
            assert command([helper, 'docker', 'exec', cid, 'cat', '/proc/1/attr/current']).stdout.strip() == 'betterdocker-default (enforce)'
        assert inspected['AppArmorProfile'] in (('', 'unconfined') if runtime['mode'] == 'rootless' else ('betterdocker-default',))
        assert command([helper, 'docker', 'exec', cid, 'cat', '/data/marker']).stdout == report.data['run']
        assert command([helper, 'docker', 'exec', cid, 'cat', '/layer-marker']).stdout == report.data['run']
    signed = False
    try:
        live.task(live.api('create', '/nodes/' + args.node + '/docker', deployment=dict(vmid=item['vmid'], name=item['name'],
            mode='compose', compose=json.dumps(compose), bridge='vmbr0', ipv4mode='none'), start=0))
        guest = lifecycle.own(report, item); assert guest
        live.task(live.api('set', live.base(guest) + '/security-policy', mode='hardened', digest=live.api('get', live.base(guest) + '/management')['digest']))
        live.power(guest, 'start')
        cid = live.container(guest, 'app')['Id']
        command([helper, 'docker', 'exec', cid, 'sh', '-c', 'printf %s "$1" >/data/marker; printf %s "$1" >/layer-marker', 'sh', report.data['run']])
        before = containers(args.node)
        runtime = json.loads(command([helper, 'identity']).stdout)
        report.data['baseline'] = dict(containers=before, runtime=runtime, resources=original)
        report.save()
        directory = '/root/betterdocker-package-' + report.data['run']
        command(['install', '-d', '-m', '0700', directory])
        for path in files:
            cluster.run(args.node, ['python3', '-c', 'import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)', directory + '/' + path.name], input=path.read_bytes(), check=True)
        install = ['python3', str(source / 'scripts/release.py'), 'deb', directory + '/' + package.name,
            '--manifest', directory + '/RELEASE.json', '--signature', directory + '/RELEASE.sig']
        first_install = install + (['--allow-downgrade'] if args.replace_test_release else [])
        command(first_install); signed = True; stable()
        report.check('signed package installation with running DCT', 'pass', 'Existing containers, runtime mapping, writable layer and volume retained')
        command(install); stable()
        command(['dpkg-trigger', '--no-await', '/usr/share/perl5/PVE/API2/Nodes.pm'])
        command(['dpkg', '--triggers-only', 'proxmox-betterdockers']); stable()
        report.check('package reinstall and native repair trigger', 'pass', 'Repeated signed installation and real trigger retained all containers and data')
        command(['bash', '/opt/betterdocker/start.sh', 'repair', '--nodes=' + args.node, '--yes']); stable()
        alternate = 'rootful' if runtime['mode'] == 'rootless' else 'rootless'
        rejected = cluster.run(args.node, ['bash', '/opt/betterdocker/start.sh', 'repair', '--nodes=' + args.node, '--yes', '--runtime=' + alternate], text=True, timeout=120)
        assert rejected.returncode and 'fixed' in rejected.stderr, rejected.stderr
        stable()
        report.check('repair and runtime-change rejection', 'pass', 'Repair retained workloads; changing the fixed runtime was rejected')
        rejected = cluster.run(args.node, ['dpkg', '--remove', 'proxmox-betterdockers'], text=True, timeout=120)
        assert rejected.returncode and 'Guest ID reservations must be empty' in rejected.stderr, rejected.stderr
        stable()
        assert cluster.run(args.node, ['test', '-e', '/usr/sbin/dct']).returncode == 0
        assert lifecycle.own(report, item)
        report.check('package uninstall with running DCT', 'pass', 'Removal rejected before changing integration; workload identity, data and security unchanged')
        lifecycle.cleanup(report)
        assert live.resources() == original, 'Original guest inventory changed'
        if original:
            report.check('empty-cluster uninstall', 'not_tested', 'Other guests exist; no unrelated workload is removed')
        else:
            command(['dpkg', '--remove', 'proxmox-betterdockers'])
            assert cluster.run(args.node, ['test', '-e', '/usr/sbin/dct']).returncode == 1
            assert cluster.run(args.node, ['test', '-e', '/usr/share/perl5/PVE/API2/BetterDocker.pm']).returncode == 1
            report.check('empty-cluster package uninstall', 'pass', 'Integration removed only after the owned DCT and its reservations were deleted')
        command(install)
        assert cluster.run(args.node, ['test', '-e', '/usr/sbin/dct']).returncode == 0
        assert json.loads(command([helper, 'identity']).stdout) == runtime
        report.check('extension reinstall after removal', 'pass', 'Runtime identity retained; guest CLI restored')
    except BaseException as error:
        report.check('package lifecycle', 'fail', str(error)[-2000:])
        raise
    finally:
        try:
            if signed and cluster.run(args.node, ['test', '-e', '/usr/share/perl5/PVE/API2/BetterDocker.pm']).returncode:
                command(install)
            lifecycle.cleanup(report)
            assert live.resources() == original, 'Original guest inventory changed'
            report.check('owned fixture cleanup', 'pass', 'Only the registered DCT was removed; original inventory retained')
        except BaseException as error:
            report.check('owned fixture cleanup', 'fail', str(error)[-2000:])
        report.finish()
    if report.data['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
