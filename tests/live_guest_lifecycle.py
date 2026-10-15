#!/usr/bin/env python3
"""Image/clone/PBS acceptance in the selected runtime on exactly registered disposable DCTs."""
import live_environment as env
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import live_security_acceptance as live
from acceptance_report import Report


def request(token, method, route, **data):
    encoded = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in data.items()}).encode() if data else None
    req = urllib.request.Request(f"https://{env.server()}:8006/api2/json" + route, data=encoded, method=method,
        headers={'Authorization': 'PVEAPIToken=' + token})
    context = env.tls()
    with urllib.request.urlopen(req, context=context, timeout=300) as response:
        return json.load(response)['data']


def inspection(info):
    c = live.container(info, 'app')
    status = live.remote(info['node'], ['docker', 'exec', c['Id'], 'cat', '/proc/1/status'], check=True).stdout
    fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    assert fields['NoNewPrivs'].strip() == '1' and fields['Seccomp'].strip() == '2'
    assert 'ALL' in c['HostConfig']['CapDrop']
    mode = live.state(info['node'])['mode']
    assert c['AppArmorProfile'] in ('', 'unconfined') if mode == 'rootless' else c['AppArmorProfile'] == 'betterdocker-default'
    assert c['HostConfig']['NanoCpus'] == 500000000 and c['HostConfig']['Memory'] == 134217728 and c['HostConfig']['PidsLimit'] == 64
    assert live.api('get', live.base(info) + '/management')['security_policy'] == 'hardened'
    return c


def files(info):
    c = live.container(info, 'app')
    command = ['sh', '-c', 'for p in /data/value /records/value /layer-value; do cat "$p"; stat -c "%u:%g:%a" "$p"; done']
    return live.remote(info['node'], ['docker', 'exec', c['Id'], *command], check=True).stdout


def own(report, item):
    # Deployment reserves betterdocker.* labels, so fixture labels use our domain.
    info = next((row for row in live.worker.summaries() if row.get('vmid') == item['vmid']), None)
    if info is None: return None
    if any(info.get(key) != item[key] for key in ('node', 'vmid', 'name')):
        raise ValueError('Unregistered fixture identity')
    code = """import sys,json
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as w
r=w.read_json(w.ROOT/w.project_id(sys.argv[1])/'request.json')
print(all(s.get('labels',{}).get('dev.gdlev.acceptance-run')==sys.argv[2] for s in json.loads(r['compose'])['services'].values()))
"""
    assert live.remote(info['node'], ['python3', '-c', code, info['id'], report.data['run']], check=True).stdout.strip() == 'True'
    report.confirm_guest(item, info)
    return info


def cleanup(report):
    for item in reversed(report.data['fixtures']):
        if item['removed']: continue
        info = own(report, item)
        if info:
            live.wait_idle(info)
            live.power(info, 'shutdown')
            for snap in live.api('get', live.base(info) + '/snapshots'):
                if snap['name'] != 'current':
                    live.task(live.api('create', live.base(info) + '/snapshot', action='delete', snapname=snap['name']))
            live.task(live.api('delete', live.base(info), **{'destroy-owned-data': 1}))
        assert own(report, item) is None
        item['removed'] = True; report.save()
    for backup in report.data.get('backups', []):
        if not backup.get('removed'):
            live.api('delete', '/nodes/' + backup['node'] + '/storage/' + backup['storage'] + '/content/' + backup['archive'])
            backup['removed'] = True; report.save()
    for image in report.data.get('images', []):
        if image['removed']: continue
        result = live.remote(image['node'], ['docker', 'image', 'inspect', image['tag']])
        if result.returncode == 0:
            actual = json.loads(result.stdout)[0]
            assert actual['Id'] == image['id'] and actual['Config']['Labels']['betterdocker.id'] == image['owner']
            live.remote(image['node'], ['docker', 'image', 'rm', image['tag']], check=True)
        else:
            assert 'no such image' in result.stderr.lower()
        image['removed'] = True; report.save()
    actor = report.data.get('actor')
    if actor and not actor.get('removed'):
        for path in actor['acl_paths']:
            live.api('set', '/access/acl', path=path, users=actor['user'], roles=actor['role'], delete=1)
            live.api('set', '/access/acl', path=path, tokens=actor['user'] + '!acceptance', roles=actor['role'], delete=1)
        if actor.get('created'): live.api('delete', '/access/users/' + actor['user'])
        if actor.get('role_created'): live.api('delete', '/access/roles/' + actor['role'])
        actor['removed'] = True; report.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default=env.node('rootless'))
    parser.add_argument('--storage', default=env.backup_storage())
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    env.require_node(args.node)
    if args.cleanup:
        report = Report.__new__(Report); report.path = args.report; report.data = json.loads(args.report.read_text())
        cleanup(report); live.verify_baseline(report); report.check('recovery cleanup', 'pass', 'Owned guests/backups/actor removed and guest baseline preserved'); report.finish(); return
    if args.report.exists(): raise SystemExit('Journal exists; inspect it or use --cleanup')
    report = Report(args.report, args.revision, {str(Path(__file__).name): hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    nodes = [r['node'] for r in live.api('get', '/nodes') if r['status'] == 'online']
    report.data['working_tree'] = True
    report.data['baseline'] = dict(resources=live.resources(), cluster_firewall=live.api('get', '/cluster/firewall/options'),
        nodes={node: live.state(node) for node in nodes}, node_firewall={node: live.api('get', '/nodes/' + node + '/firewall/options') for node in nodes})
    mode = report.data['baseline']['nodes'][args.node]['mode']
    assert mode in ('rootful', 'rootless')
    report.save()
    try:
        item = report.plan_guest(args.node, int(live.api('get', '/cluster/nextid')), 'release-' + mode + '-' + report.data['run'][:8])
        compose = {'services': {'app': {'image': 'busybox:1.37', 'pull_policy': 'never', 'init': True,
            'command': ['sh', '-c', "trap 'exit 0' TERM; while true; do sleep 1; done"],
            'cpus': .5, 'mem_limit': '128m', 'pids_limit': 64, 'volumes': ['./data:/data', 'records:/records'],
            'labels': {'dev.gdlev.acceptance-run': report.data['run']}}}, 'volumes': {'records': {}}}
        item['create_task'] = live.api('create', '/nodes/' + args.node + '/docker', deployment=dict(vmid=item['vmid'], name=item['name'], mode='compose', compose=json.dumps(compose), bridge='vmbr0', ipv4mode='none'), start=0)
        report.save(); live.task(item['create_task'])
        source = own(report, item); assert source
        root = live.base(source)
        live.task(live.api('set', root + '/security-policy', mode='hardened', digest=live.api('get', root + '/management')['digest']))
        live.api('set', root + '/limits', cpus=2, memory=512, pids=256, services=2, replicas=2, digest=live.api('get', root + '/management')['digest'])
        live.power(source, 'start'); inspection(source)
        marker = report.data['run']
        c = live.container(source, 'app')
        live.remote(args.node, ['docker', 'exec', c['Id'], 'sh', '-c', 'printf %s ' + marker + ' >/data/value; printf %s ' + marker + ' >/records/value'], check=True)
        live.power(source, 'shutdown')
        live.task(live.api('create', root + '/images/preview', images={'app': 'busybox:1.36'}, digest=live.api('get', root + '/management')['digest']))
        plan = live.api('get', root + '/images')
        assert any(row['changed'] for row in plan['changes'])
        live.task(live.api('create', root + '/images', token=plan['token'], recovery='release-image-check', digest=live.api('get', root + '/management')['digest']))
        live.power(source, 'start'); inspection(source)
        c = live.container(source, 'app')
        for path in ('/data/value', '/records/value'):
            assert live.remote(args.node, ['docker', 'exec', c['Id'], 'cat', path], check=True).stdout == marker
        live.remote(args.node, ['docker', 'exec', c['Id'], 'sh', '-c', 'printf %s ' + marker + ' >/layer-value; chown 12345:12345 /data/value /records/value /layer-value; chmod 640 /data/value /records/value /layer-value'], check=True)
        expected = files(source); live.power(source, 'shutdown')
        report.check(mode + ' registry pull and immutable image update', 'pass', 'busybox 1.37 to 1.36; volumes, Hardened and enforced caps retained')
        nonce = report.data['run'][:8]
        actor = dict(user='bdrelease-' + nonce + '@pve', role='BDRelease' + nonce, acl_paths=[], created=False, role_created=False, removed=False)
        report.data['actor'] = actor; report.save()
        assert not any(r['userid'] == actor['user'] for r in live.api('get', '/access/users'))
        assert not any(r['roleid'] == actor['role'] for r in live.api('get', '/access/roles'))
        live.api('create', '/access/roles', roleid=actor['role'], privs='VM.Audit VM.Console VM.PowerMgmt VM.Backup VM.Allocate VM.Clone VM.Config.CPU VM.Config.Memory VM.Config.Network VM.Config.Disk VM.Config.Options VM.Snapshot VM.Snapshot.Rollback Datastore.Audit Datastore.AllocateSpace SDN.Use')
        actor['role_created'] = True; report.save()
        live.api('create', '/access/users', userid=actor['user'], expire=int(time.time()) + 3600)
        actor['created'] = True; report.save()
        secret = live.api('create', '/access/users/' + actor['user'] + '/token/acceptance', privsep=1)['value']
        token = actor['user'] + '!acceptance=' + secret
        def grant(path):
            actor['acl_paths'].append(path); report.save()
            live.api('set', '/access/acl', path=path, users=actor['user'], roles=actor['role'])
            live.api('set', '/access/acl', path=path, tokens=actor['user'] + '!acceptance', roles=actor['role'])
        grant('/vms/' + str(source['vmid'])); grant('/storage/' + args.storage); grant('/sdn/zones/localnetwork/vmbr0')
        targets = []
        for operation in ('clone', 'restore'):
            destination = report.plan_guest(args.node, int(live.api('get', '/cluster/nextid')), item['name'] + '-' + operation)
            grant('/vms/' + str(destination['vmid']))
            deployment = dict(name=destination['name'], bridge='vmbr0', ipv4mode='none')
            if operation == 'clone':
                upid = request(token, 'POST', root + '/clone', newid=destination['vmid'], deployment=deployment, digest=live.api('get', root + '/management')['digest'])
            else:
                before = {r['volid'] for r in live.api('get', '/nodes/' + args.node + '/storage/' + args.storage + '/content', content='backup', vmid=source['vmid'])}
                report.data['backup_intent'] = dict(node=args.node, storage=args.storage, vmid=source['vmid'], source_id=source['id']); report.save()
                live.task(live.api('create', '/nodes/' + args.node + '/vzdump', vmid=source['vmid'], storage=args.storage, mode='stop'))
                after = live.api('get', '/nodes/' + args.node + '/storage/' + args.storage + '/content', content='backup', vmid=source['vmid'])
                created = [r['volid'] for r in after if r['volid'] not in before]; assert len(created) == 1, created
                backup = dict(node=args.node, storage=args.storage, archive=created[0], source_id=source['id'], removed=False)
                report.data.setdefault('backups', []).append(backup); report.save()
                upid = request(token, 'POST', '/nodes/' + args.node + '/docker', newid=destination['vmid'], archive=backup['archive'], deployment=deployment)
            destination['create_task'] = upid; report.save(); live.task(upid)
            target = own(report, destination); assert target
            live.task(request(token, 'POST', live.base(target) + '/action', action='start', timeout=30))
            inspection(target); assert files(target) == expected
            live.task(request(token, 'POST', live.base(target) + '/action', action='shutdown', timeout=30))
            targets.append(target)
            report.check(mode + ' delegated ' + operation, 'pass', 'Actual HTTP user/token without node Sys.Modify; data, UID/GID/mode, private image provenance, Hardened and caps verified')
        tag = 'betterdocker-acceptance-' + report.data['run'] + '-foreign'
        owned_image = dict(node=args.node, owner=targets[0]['id'], tag=tag, removed=False)
        report.data.setdefault('images', []).append(owned_image); report.save()
        image = live.remote(args.node, ['docker', 'commit', live.container(targets[0], 'app')['Id'], tag], check=True).stdout.strip()
        owned_image['id'] = image; report.save()
        upid = request(token, 'POST', root + '/images/preview', images={'app': image}, digest=live.api('get', root + '/management')['digest'])
        try: live.task(upid)
        except RuntimeError as error:
            assert 'Private images require access' in str(error), str(error)
        else: raise AssertionError('Unrelated private image preview was accepted')
        report.check('cross-DCT private image admission', 'pass', 'Normal image-preview request rejects a different DCT private image before pull')
    except BaseException as error:
        report.check(mode + ' release acceptance', 'fail', str(error)[-2000:]); raise
    finally:
        try:
            cleanup(report); live.verify_baseline(report)
            report.check('cleanup and original guest preservation', 'pass', 'Exact owned guests, PBS archive, temporary actor/ACLs removed; original baseline preserved')
        except BaseException as error:
            report.check('cleanup and original guest preservation', 'fail', str(error)[-2000:])
        report.finish()
    if report.data['status'] == 'failed': raise SystemExit(1)


if __name__ == '__main__': main()
