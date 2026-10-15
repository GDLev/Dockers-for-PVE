#!/usr/bin/env python3
"""Create, edit and roll back snapshots of one registered disposable DCT."""
import argparse
import hashlib
import json
from pathlib import Path

from acceptance_report import Report
import live_guest_lifecycle as lifecycle
import live_security_acceptance as live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError('Journal already exists; inspect registered fixtures before retrying')
    report = Report(args.report, args.revision, {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    nodes = [row['node'] for row in live.api('get', '/nodes') if row['status'] == 'online']
    if args.node not in nodes:
        raise ValueError('Target node is not online')
    report.data['baseline'] = dict(resources=live.resources(), cluster_firewall=live.api('get', '/cluster/firewall/options'),
        nodes={node: live.state(node) for node in nodes}, node_firewall={node: live.api('get', '/nodes/' + node + '/firewall/options') for node in nodes})
    item = report.plan_guest(args.node, int(live.api('get', '/cluster/nextid')), 'snapshot-check-' + report.data['run'][:8])
    compose = {'services': {'app': {'image': 'busybox:1.37', 'pull_policy': 'never', 'init': True,
        'command': ['sh', '-c', "trap 'exit 0' TERM; while true; do sleep 1; done"],
        'cpus': .5, 'mem_limit': '128m', 'pids_limit': 64, 'volumes': ['./data:/data', 'records:/records'],
        'labels': {'dev.gdlev.acceptance-run': report.data['run']}}}, 'volumes': {'records': {}}}
    try:
        live.task(live.api('create', '/nodes/' + args.node + '/docker', deployment=dict(vmid=item['vmid'], name=item['name'],
            mode='compose', compose=json.dumps(compose), bridge='vmbr0', ipv4mode='none'), start=0))
        guest = lifecycle.own(report, item); assert guest
        root = live.base(guest)
        live.task(live.api('set', root + '/security-policy', mode='hardened', digest=live.api('get', root + '/management')['digest']))
        live.power(guest, 'start')
        cid = live.container(guest, 'app')['Id']
        live.remote(args.node, ['docker', 'exec', cid, 'sh', '-c',
            'for p in /data/value /records/value /layer-value; do printf %s "$1" > "$p"; chown 12345:12345 "$p"; chmod 640 "$p"; done',
            'sh', report.data['run']], check=True)
        expected = lifecycle.files(guest)
        live.power(guest, 'shutdown')
        live.task(live.api('create', root + '/snapshot', action='create', snapname='checkpoint', description='Original data'))
        metadata = live.api('get', root + '/snapshot/checkpoint/config')
        live.api('set', root + '/snapshot/checkpoint/config', description='Rollback checkpoint', digest=metadata['digest'])
        assert live.api('get', root + '/snapshot/checkpoint/config')['description'] == 'Rollback checkpoint'
        report.check('snapshot creation and metadata editing', 'pass', 'Stopped DCT snapshot and digest-based description update')
        live.power(guest, 'start')
        cid = live.container(guest, 'app')['Id']
        live.remote(args.node, ['docker', 'exec', cid, 'sh', '-c',
            'for p in /data/value /records/value /layer-value; do printf changed > "$p"; chmod 600 "$p"; done'], check=True)
        live.power(guest, 'shutdown')
        before = {row['name'] for row in live.api('get', root + '/snapshots')}
        live.task(live.api('create', root + '/snapshot', action='restore', snapname='checkpoint'))
        after = live.api('get', root + '/snapshots')
        assert {row['name'] for row in after} == before, 'Rollback created an extra snapshot'
        assert next(row for row in after if row['name'] == 'current')['parent'] == 'checkpoint'
        live.power(guest, 'start')
        assert lifecycle.files(guest) == expected, 'Rollback did not preserve data or file ownership/mode'
        lifecycle.inspection(guest)
        live.power(guest, 'shutdown')
        report.check('snapshot rollback', 'pass', 'Writable layer, bind and named-volume data plus UID/GID/mode restored; no automatic extra snapshot')
        live.task(live.api('create', root + '/snapshot', action='delete', snapname='checkpoint'))
        assert [row['name'] for row in live.api('get', root + '/snapshots')] == ['current']
        report.check('snapshot deletion', 'pass', 'Only the current state remains in the snapshot tree')
    except BaseException as error:
        report.check('snapshot lifecycle', 'fail', str(error)[-2000:])
        raise
    finally:
        try:
            lifecycle.cleanup(report)
            live.verify_baseline(report)
            report.check('owned fixture cleanup', 'pass', 'Registered DCT removed and original lab state preserved')
        except BaseException as error:
            report.check('owned fixture cleanup', 'fail', str(error)[-2000:])
        report.finish()
    if report.data['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
