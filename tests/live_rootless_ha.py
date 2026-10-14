import live_environment as env
#!/usr/bin/env python3
f"Native HA relocation and fenced recovery of the rootless cluster fixture.\n\nThe fence test force-reboots the approved, evacuated {env.node('secondary')}. A temporary\ncorosync delay keeps it out of quorum long enough for native CRM recovery.\n--cleanup removes that owned delay and HA rule even after interruption.\n"
import argparse
import json
import subprocess
from pathlib import Path
import sys
import time
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from live_pools_hooks import api, wait_for
from live_firewall import remote

STATE = Path('/root/dct-rootless-cluster.json')
DROPIN = '/etc/systemd/system/corosync.service.d/betterdocker-rootless-test.conf'


def run(action):
    assert worker.current_node() == env.node('primary')
    state = json.loads(STATE.read_text())
    identifier, vmid = state['id'], state['vmid']
    sid = 'docker:' + str(vmid)
    rule = 'bd-rootless-' + str(vmid)
    def save():
        STATE.write_text(json.dumps(state, indent=2)); STATE.chmod(0o600)
    def current():
        return next(i for i in worker.summaries() if i['id'] == identifier)
    def check(node):
        wait_for(lambda: current()['node'] == node and current()['status'] == 'running', timeout=360)
        def container():
            return remote(node, ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier], check=True).stdout.strip()
        wait_for(container, timeout=180)
        cid = container()
        data = remote(node, ['docker', 'exec', cid, 'sh', '-c', 'cat /data/proof; stat -c %u:%g /data/proof'], check=True).stdout.strip()
        assert data == 'rootless-data-proof\n1234:2345', data
    if action == 'cleanup':
        if state.get('fence'):
            remote(env.node('secondary'), ['rm', '--', DROPIN], check=True)
            remote(env.node('secondary'), ['systemctl', 'daemon-reload'], check=True)
            remote(env.node('secondary'), ['systemctl', 'restart', '--no-block', 'corosync'], check=True)
            state.pop('fence'); save()
        if any(i['sid'] == sid for i in api('get', '/cluster/ha/resources')):
            api('set', '/cluster/ha/resources/' + sid, state='stopped')
            wait_for(lambda: current()['status'] == 'stopped', timeout=180)
            api('delete', '/cluster/ha/resources/' + sid)
        if any(i['rule'] == rule for i in api('get', '/cluster/ha/rules')):
            api('delete', '/cluster/ha/rules/' + rule)
        print('PASS temporary HA registration and boot delay removed', flush=True); return
    if action == 'prepare':
        assert state.get('replicated') and current()['node'] == env.node('secondary')
        if not any(i['sid'] == sid for i in api('get', '/cluster/ha/resources')):
            api('create', '/cluster/ha/resources', sid=sid, state='stopped')
        state['rule'] = rule; save()
        if not any(i['rule'] == rule for i in api('get', '/cluster/ha/rules')):
            api('create', '/cluster/ha/rules', rule=rule, type='node-affinity', resources=sid, nodes=f"{env.node('secondary')},{env.node('rootless')}", strict=1)
        api('set', '/cluster/ha/resources/' + sid, state='started')
        check(env.node('secondary'))
        for destination in (env.node('rootless'), env.node('secondary')):
            api('create', '/cluster/ha/resources/' + sid + '/relocate', node=destination)
            check(destination)
            remote(destination, ['pvesr', 'run', '--id', state['job']], check=True, timeout=300)
            print('PASS native HA relocation to', destination, 'preserves owners and workload', flush=True)
        state['ha_ready'] = True; save(); return
    if action == 'fence':
        assert state.get('ha_ready') and current()['node'] == env.node('secondary')
        assert json.loads(remote(env.node('secondary'), ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)['mode'] == 'rootless'
        running = remote(env.node('secondary'), ['docker', 'ps', '-q'], check=True).stdout.split()
        values = json.loads(remote(env.node('secondary'), ['docker', 'inspect', *running], check=True).stdout)
        assert all(c['Config']['Labels'].get('betterdocker.id') == identifier for c in values), 'Unrelated workloads are running on reboot target'
        assert remote(env.node('secondary'), ['test', '-e', DROPIN]).returncode != 0
        state['fence'] = dict(boot=remote(env.node('secondary'), ['cat', '/proc/sys/kernel/random/boot_id'], check=True).stdout.strip())
        save()
        remote(env.node('secondary'), ['mkdir', '-p', str(Path(DROPIN).parent)], check=True)
        remote(env.node('secondary'), ['tee', DROPIN], input='[Service]\nExecStartPre=/bin/sleep 300\n', check=True)
        remote(env.node('secondary'), ['systemctl', 'daemon-reload'], check=True)
        try:
            remote(env.node('secondary'), ['reboot', '-f'], timeout=15)
        except subprocess.TimeoutExpired:
            pass  # Reboot can leave the SSH channel open; the boot ID is verified below.
        print('pve2 force-reboot requested; waiting for native fenced recovery', flush=True)
        check(env.node('rootless'))
        state['recovered'] = True; save()
        print('PASS native CRM fenced recovery uses rootless runtime and remaps owners', flush=True)
        return
    assert action == 'check'
    check(env.node('rootless'))
    assert state.get('fence')
    boot = remote(env.node('secondary'), ['cat', '/proc/sys/kernel/random/boot_id'], check=True).stdout.strip()
    assert boot and boot != state['fence']['boot']
    assert not remote(env.node('secondary'), ['docker', 'ps', '-q', '--filter', 'label=betterdocker.id=' + identifier], check=True).stdout.strip(), 'Old owner restarted the guest'
    state['recovered'] = True; save()
    print('PASS native CRM fenced recovery uses rootless runtime and remaps owners', flush=True)
    wait_for(lambda: any(i['node'] == env.node('secondary') and i.get('status') == 'online' for i in api('get', '/nodes')), timeout=300)
    print('PASS rebooted source rejoins without a duplicate workload', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'fence', 'check', 'cleanup'))
    run(parser.parse_args().action)
