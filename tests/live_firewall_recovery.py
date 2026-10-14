#!/usr/bin/env python3
"""Verify DCT firewall through HA start, Docker restart and a test-node reboot."""
import live_environment as env
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from live_pools_hooks import api, task, wait_for
from live_firewall import remote

path = Path('/root/dct-firewall-acceptance.json')
state = json.loads(path.read_text())
node = state['node']
guests = [i for i in worker.summaries() if i['name'].startswith(state['prefix'])]
a = next(i for i in guests if i['id'] == state['guests'][0])
base = f'/nodes/{node}/docker/{a["id"]}'
fw = base + '/firewall'

def power(info, action):
    task(api('create', f'/nodes/{info["node"]}/docker/{info["id"]}/action', action=action, timeout=10))

def probe(allowed):
    result = subprocess.run(['curl', '-s', '--max-time', '2', f'http://{a["address"]}:18080/'], capture_output=True, text=True)
    assert ('firewall-ok' in result.stdout) == allowed, (allowed, result.stdout)

def rules(allow):
    for rule in api('get', fw + '/rules')[::-1]: api('delete', fw + '/rules/' + str(rule['pos']))
    if allow: api('create', fw + '/rules', type='in', action='ACCEPT', proto='tcp', dport='18080', enable=1)

def check_boot():
    def ready():
        try:
            return api('get', base + '/management')['status'] == 'running'
        except (RuntimeError, OSError):
            return False
    wait_for(ready, timeout=180)
    restored = int(remote(node, ['systemctl', 'show', 'betterdocker-firewall-restore', '-p', 'ExecMainExitTimestampMonotonic', '--value'], check=True).stdout)
    docker = int(remote(node, ['systemctl', 'show', 'docker', '-p', 'ExecMainStartTimestampMonotonic', '--value'], check=True).stdout)
    assert 0 < restored <= docker, (restored, docker)
    probe(False); rules(True); probe(True)
    api('set', base + '/config', onboot=0)
    print('PASS node reboot restores protection before Docker and workload startup', flush=True)

if '--after-reboot' in sys.argv:
    check_boot(); sys.exit()

running = remote(node, ['docker', 'ps', '-q'], check=True).stdout.split()
if running:
    containers = json.loads(remote(node, ['docker', 'inspect', *running], check=True).stdout)
    owned = {'bd-' + i['id'] for i in guests}
    assert all((c['Config'].get('Labels') or {}).get('com.docker.compose.project') in owned for c in containers), 'Other workloads are running on the reboot target'
for info in guests:
    if info['id'] != a['id'] and info['node'] == node: power(info, 'shutdown')
# HA requires a completed native ZFS replica. Use the existing test storage on
# Reboot targets must provide the storage required by the fixture.
ha_name = state['prefix'] + '-ha'
ha_node = env.node('primary')
ha = next((i for i in worker.summaries() if i['name'] == ha_name), None)
if not ha:
    source = 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo firewall-ok > /www/index.html; exec httpd -f -p 80 -h /www"]\n    ports: ["18080:80"]\n    volumes: [data:/www]\nvolumes:\n  data: {}\n'
    task(api('create', '/nodes/' + ha_node + '/docker', deployment=dict(name=ha_name, mode='compose', compose=source, bridge='vmbr0', cidr=f'{env.address(217)}/{env.prefix()}', firewall=1)))
    ha = next(i for i in worker.summaries() if i['name'] == ha_name)
ha_base = f'/nodes/{ha_node}/docker/{ha["id"]}'
power(ha, 'shutdown')
api('set', ha_base + '/firewall/options', enable=1)
task(api('set', ha_base + '/storage-limit', size_mib=512, storage='bdstage7-zfs'))
job = str(ha['vmid']) + '-0'
state['ha_job'] = job; path.write_text(json.dumps(state))
api('create', '/cluster/replication', id=job, target=env.node('secondary'), type='local', schedule='00:59')
result = remote(ha_node, ['pvesr', 'run', '--id', job], timeout=300)
assert result.returncode == 0, result.stderr
wait_for(lambda: env.node('secondary') in api('get', ha_base + '/management').get('replication', {}).get('targets', {}))
sid = 'docker:' + str(ha['vmid'])
state['ha_sid'] = sid; path.write_text(json.dumps(state))
api('create', '/cluster/ha/resources', sid=sid, state='started')
wait_for(lambda: api('get', ha_base + '/management')['status'] == 'running', timeout=180)
command = ['curl', '-s', '--max-time', '2', f'http://{ha["address"]}:18080/']
assert 'firewall-ok' not in subprocess.run(command, text=True, capture_output=True).stdout
api('create', ha_base + '/firewall/rules', type='in', action='ACCEPT', proto='tcp', dport='18080', enable=1)
assert 'firewall-ok' in subprocess.run(command, text=True, capture_output=True).stdout
api('set', '/cluster/ha/resources/' + sid, state='stopped')
wait_for(lambda: api('get', ha_base + '/management')['status'] == 'stopped', timeout=180)
api('delete', '/cluster/ha/resources/' + sid)
state.pop('ha_sid'); path.write_text(json.dumps(state))
print('PASS HA-controlled startup applies native firewall policy', flush=True)
rules(False)
power(a, 'start'); probe(False)
# Check for any successful connection while the Docker service is restarting.
with ThreadPoolExecutor(max_workers=1) as executor:
    restart = executor.submit(remote, node, ['systemctl', 'restart', 'docker'], timeout=150)
    while not restart.done():
        result = subprocess.run(['curl', '-s', '--max-time', '.3', f'http://{a["address"]}:18080/'], capture_output=True, text=True)
        assert 'firewall-ok' not in result.stdout, 'Firewall bypass during Docker restart'
        time.sleep(.1)
    result = restart.result()
    assert result.returncode == 0, result.stderr
wait_for(lambda: api('get', base + '/management')['status'] == 'running', timeout=120)
probe(False); rules(True); probe(True)
print('PASS Docker restart retains protection and resumes the workload', flush=True)
api('set', base + '/config', onboot=1)
rules(False)
old_boot = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], check=True).stdout.strip()
remote(node, ['systemctl', 'reboot'], timeout=15)
time.sleep(8)
def booted():
    try:
        result = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], timeout=8)
        return result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != old_boot
    except subprocess.TimeoutExpired:
        return False
wait_for(booted, timeout=180)
check_boot()
