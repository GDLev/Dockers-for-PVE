#!/usr/bin/env python3
"""Offline conversion, resume and rollback on the explicitly borrowed pve2.

Run after the cluster/data fixtures are removed and the empty conversion has
been rolled back. Interruptions happen in child processes, leaving real journals.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from live_firewall import remote
from live_pools_hooks import api, task

STATE = Path('/root/dct-rootless-conversion.json')
NODE = 'pve2'
SCRIPT = '/usr/local/lib/betterdocker/migrate-runtime.py'


def run(cleanup=False):
    assert worker.current_node() == 'pve1'
    state = json.loads(STATE.read_text()) if STATE.exists() else dict(name='rootless-conversion-' + uuid.uuid4().hex[:8])
    def save():
        STATE.write_text(json.dumps(state, indent=2)); STATE.chmod(0o600)
    def info():
        return next(i for i in worker.summaries() if i['id'] == state['id'])
    def base():
        return '/nodes/' + NODE + '/docker/' + state['id']
    def power(action):
        task(api('create', base() + '/action', action=action, timeout=10))
    def check(mode):
        identity = json.loads(remote(NODE, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)
        assert identity['mode'] == mode
        assert info()['vmid'] == state['vmid'] and info()['node'] == NODE
        reservation = json.loads(remote(NODE, ['cat', '/etc/pve/betterdocker/ids/' + str(state['vmid']) + '.json'], check=True).stdout)
        assert reservation == {'id': state['id'], 'node': NODE}, reservation
        assert api('get', base() + '/config')['description'] == 'Offline conversion acceptance'
        power('start')
        cid = remote(NODE, ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=bd-' + state['id']], check=True).stdout.strip()
        value = remote(NODE, ['docker', 'exec', cid, 'sh', '-c', 'cat /data/proof; stat -c %u:%g /data/proof'], check=True).stdout.strip()
        assert value == 'conversion-proof\n1234:2345', value
        power('shutdown')
        if mode == 'rootless':
            blocked = remote(NODE, ['python3', SCRIPT, '--runtime=rootful', '--apply'])
            assert blocked.returncode != 0 and 'Destination runtime is not empty' in blocked.stderr, blocked.stderr
            assert json.loads(remote(NODE, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)['mode'] == mode
    def interrupt(partial):
        code = '''import sys,importlib.util,os
sys.path.insert(0,"/usr/local/lib/betterdocker")
s=importlib.util.spec_from_file_location("conversion",''' + repr(SCRIPT) + ''');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
'''
        if partial:
            code += '''original=m.guest_state.worker.ensure_address
def stop(info):
 original(info)
 os._exit(93)
m.guest_state.worker.ensure_address=stop
'''
        else:
            code += '''original=m.guest_state.restore_state
def stop(*args,**kwargs):
 original(*args,**kwargs)
 os._exit(93)
m.guest_state.restore_state=stop
'''
        code += 'm.convert("rootless")\n'
        result = remote(NODE, ['python3', '-c', code], timeout=300)
        assert result.returncode == 93, (result.returncode, result.stderr)
        journal = json.loads(remote(NODE, ['python3', '-c', 'import json;from pathlib import Path;p=max(Path("/var/lib/betterdocker/runtime-migrations").glob("*/journal.json"),key=lambda p:p.stat().st_mtime);print(json.dumps({"token":p.parent.name,**json.loads(p.read_text())}))'], check=True).stdout)
        assert journal['phase'] == 'restore' and journal['guests'][0]['id'] == state['id']
        state['journal'] = journal['token']; save()
        return journal['token']
    save()
    if cleanup:
        identity = json.loads(remote(NODE, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)
        assert identity['mode'] == 'rootful'
        if any(i['id'] == state.get('id') for i in worker.summaries()):
            assert info()['name'] == state['name']
            power('force-stop'); task(api('delete', base()))
        identifier = state['id']
        remote(NODE, ['python3', '-c', 'import sys,json,shutil;sys.path.insert(0,"/usr/local/lib/betterdocker");from dctlib import projects as w;p=w.ROOT/' + repr(identifier) + ';assert json.loads((p/"request.json").read_text())["name"]==' + repr(state['name']) + ';shutil.rmtree(p)'], check=True)
        volumes = remote(NODE, ['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier], check=True).stdout.split()
        if volumes:
            remote(NODE, ['docker', 'volume', 'rm', *volumes], check=True)
        STATE.unlink(); print('PASS conversion fixture removed; rootful runtime restored', flush=True); return
    if not state.get('id'):
        assert json.loads(remote(NODE, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)['mode'] == 'rootful'
        compose = 'services:\n  app:\n    image: busybox:stable\n    init: true\n    command: [sh, -c, "mkdir -p /data; test -f /data/proof || { echo conversion-proof > /data/proof; chown 1234:2345 /data/proof; }; exec sleep infinity"]\n    volumes: [data:/data]\nvolumes:\n  data: {}\n'
        task(api('create', '/nodes/' + NODE + '/docker', deployment=dict(name=state['name'], mode='compose', compose=compose, bridge='vmbr0', cidr='192.0.2.225/20')))
        current = next(i for i in worker.summaries() if i['name'] == state['name'])
        state.update(id=current['id'], vmid=current['vmid']); save()
    api('set', base() + '/config', description='Offline conversion acceptance')
    if not state.get('blockers_checked'):
        power('start')
        assert 'stop all service instances' in remote(NODE, ['python3', SCRIPT, '--runtime=rootless'], check=True).stdout
        power('shutdown')
        task(api('create', base() + '/snapshot', action='create', snapname='conversion-blocker'))
        report = json.loads(remote(NODE, ['python3', SCRIPT, '--runtime=rootless'], check=True).stdout)
        assert any('snapshot history' in i for i in report['blockers']), report
        blocked = remote(NODE, ['python3', SCRIPT, '--runtime=rootless', '--apply'])
        assert blocked.returncode != 0 and 'snapshot history' in blocked.stderr
        task(api('create', base() + '/snapshot', action='delete', snapname='conversion-blocker'))
        check('rootful'); state['blockers_checked'] = True; save()
        print('PASS running guest and snapshot blockers retain source data and runtime', flush=True)
    if not state.get('resume_checked'):
        token = state.get('journal') or interrupt(False)
        remote(NODE, ['python3', SCRIPT, '--runtime=rootless', '--resume', token, '--apply'], check=True, timeout=300)
        check('rootless')
        remote(NODE, ['python3', SCRIPT, '--rollback', token, '--apply'], check=True, timeout=300)
        check('rootful'); state['resume_checked'] = True; state.pop('journal'); save()
        print('PASS interrupted conversion resumes with the same VMID, notes and data; rollback restores source', flush=True)
    if not state.get('rollback_checked'):
        token = state.get('journal') or interrupt(True)
        remote(NODE, ['python3', SCRIPT, '--rollback', token, '--apply'], check=True, timeout=300)
        check('rootful'); state['rollback_checked'] = True; save()
        print('PASS interrupted partial import rolls back and leaves no import journal', flush=True)
        assert remote(NODE, ['test', '-e', '/var/lib/betterdocker/imports/' + state['id'] + '.json']).returncode != 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true')
    run(parser.parse_args().cleanup)
