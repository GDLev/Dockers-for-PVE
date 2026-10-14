#!/usr/bin/env python3
"""Actual rootless disk, clone, template and PBS ownership checks.

Run after live_rootless_cluster.py on pve1. Disposable guest IDs and backup
volumes are recorded for --cleanup; PBS receives backups only.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from live_pools_hooks import api, task
from live_firewall import remote

STATE = Path('/root/dct-rootless-data.json')
PBS = 'bdrootless-pbs'


def run(cleanup=False):
    assert worker.current_node() == 'pve1'
    state = json.loads(STATE.read_text()) if STATE.exists() else dict(name='rootless-data-' + uuid.uuid4().hex[:8], guests=[], backups=[])
    def save():
        STATE.write_text(json.dumps(state, indent=2)); STATE.chmod(0o600)
    def info(identifier):
        return next(i for i in worker.summaries() if i['id'] == identifier)
    def base(item):
        return '/nodes/' + info(item['id'])['node'] + '/docker/' + item['id']
    def power(item, action):
        task(api('create', base(item) + '/action', action=action, timeout=10))
    def container(item):
        return remote(info(item['id'])['node'], ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=bd-' + item['id']], check=True).stdout.strip()
    def check(item):
        power(item, 'start')
        value = remote(info(item['id'])['node'], ['docker', 'exec', container(item), 'sh', '-c', 'cat /data/proof; stat -c %u:%g /data/proof'], check=True).stdout.strip()
        assert value == 'rootless-data-proof\n1234:2345', value
        power(item, 'shutdown')
    def remember(item):
        if item['id'] not in state['guests']:
            state['guests'].append(item['id']); save()
        return item
    save()
    if cleanup:
        for identifier in state['guests']:
            current = next((i for i in worker.summaries() if i['id'] == identifier), None)
            if not current:
                continue
            if not current.get('template'):
                power(current, 'force-stop')
            task(api('delete', base(current)))
            for node in ('pve1', 'pve2', 'pve3'):
                script = 'import json,shutil; from pathlib import Path; from dctlib import projects as w; p=w.ROOT/' + repr(identifier) + ';\nif p.exists():\n assert json.loads((p/"request.json").read_text())["name"].startswith(' + repr(state['name']) + ');\n if w.storage_config(' + repr(identifier) + ').get("volid"): w.purge_retained_storage(' + repr(identifier) + ');\n shutil.rmtree(p)\n'
                remote(node, ['python3', '-c', 'import sys; sys.path.insert(0,"/usr/local/lib/betterdocker");\n' + script], check=True)
                volumes = remote(node, ['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier], check=True).stdout.split()
                if volumes:
                    remote(node, ['docker', 'volume', 'rm', *volumes], check=True)
        for archive in state['backups']:
            api('delete', '/nodes/pve1/storage/' + PBS + '/content/' + archive)
        STATE.unlink(); print('PASS data fixtures and PBS backups removed', flush=True); return
    cluster = json.loads(Path('/root/dct-rootless-cluster.json').read_text())
    source = info(cluster['id'])
    if not state.get('disk_checked'):
        power(source, 'start')
        result = remote(source['node'], ['docker', 'exec', container(source), 'dd', 'if=/dev/zero', 'of=/data/full', 'bs=1M', 'count=600'], timeout=60)
        assert result.returncode != 0 and 'No space left' in result.stderr, result.stderr
        remote(source['node'], ['docker', 'exec', container(source), 'rm', '/data/full'], check=True)
        power(source, 'shutdown'); state['disk_checked'] = True; save()
        print('PASS managed rootless data disk enforces its capacity', flush=True)
    name = state['name'] + '-clone'
    clone = next((i for i in worker.summaries() if i['name'] == name), None)
    if not clone:
        task(api('create', base(source) + '/clone', storage='bdrootless-zfs', deployment=dict(name=name, bridge='vmbr0', cidr='192.0.2.215/20')))
        clone = next(i for i in worker.summaries() if i['name'] == name)
    remember(clone)
    if not clone.get('template'):
        check(clone)
        task(api('create', base(clone) + '/template'))
    name = state['name'] + '-template-clone'
    child = next((i for i in worker.summaries() if i['name'] == name), None)
    if not child:
        task(api('create', base(clone) + '/clone', storage='bdrootless-zfs', deployment=dict(name=name, bridge='vmbr0', cidr='192.0.2.218/20')))
        child = next(i for i in worker.summaries() if i['name'] == name)
    remember(child); check(child)
    print('PASS full clone and template clone preserve data and logical owners', flush=True)
    for kind, master in [('rootless', source), ('legacy', None)]:
        if master is None:
            name = state['name'] + '-legacy'
            master = next((i for i in worker.summaries() if i['name'] == name), None)
            if not master:
                compose = 'services:\n  app:\n    image: busybox:stable\n    init: true\n    command: [sh, -c, "mkdir -p /data; test -f /data/proof || { echo rootless-data-proof > /data/proof; chown 1234:2345 /data/proof; }; exec sleep infinity"]\n    volumes: [data:/data]\nvolumes:\n  data: {}\n'
                task(api('create', '/nodes/pve1/docker', deployment=dict(name=name, mode='compose', compose=compose, bridge='vmbr0', cidr='192.0.2.216/20')))
                master = next(i for i in worker.summaries() if i['name'] == name)
            remember(master); check(master)
        if kind not in state:
            task(api('create', '/nodes/' + master['node'] + '/vzdump', vmid=master['vmid'], storage=PBS, mode='stop'))
            backups = api('get', '/nodes/' + master['node'] + '/storage/' + PBS + '/content', content='backup', vmid=master['vmid'])
            state[kind] = max(backups, key=lambda i: i.get('ctime', 0))['volid']
            state['backups'].append(state[kind]); save()
        name = state['name'] + '-' + kind + '-restore'
        restored = next((i for i in worker.summaries() if i['name'] == name), None)
        if not restored:
            task(api('create', '/nodes/pve3/docker', archive=state[kind], storage='local-lvm', deployment=dict(name=name, bridge='vmbr0', cidr=('192.0.2.223' if kind == 'rootless' else '192.0.2.224') + '/20')))
            restored = next(i for i in worker.summaries() if i['name'] == name)
        remember(restored); check(restored)
        print('PASS', kind, 'PBS backup and restore preserve data and logical owners', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true')
    run(parser.parse_args().cleanup)
