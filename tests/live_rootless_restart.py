#!/usr/bin/env python3
"""Verify protection and workload recovery during runtime restart and node reboot."""
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

state = json.loads(Path('/root/dct-rootless-firewall.json').read_text())
node = state['node']
assert node == 'pve3' and json.loads(remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)['mode'] == 'rootless'
assert remote(node, ['systemctl', 'is-active', '--quiet', 'docker.service', 'docker.socket']).returncode != 0
info = next(i for i in worker.summaries() if i['id'] == state['guests'][0])
base = '/nodes/' + node + '/docker/' + info['id']
for row in api('get', base + '/firewall/rules')[::-1]:
    api('delete', base + '/firewall/rules/' + str(row['pos']))
task(api('create', base + '/action', action='start'))
api('set', base + '/config', onboot=1)
def probe():
    return 'firewall-ok' in subprocess.run(['curl', '--noproxy', '*', '-s', '--max-time', '2', 'http://' + info['address'] + ':18080/'], text=True, capture_output=True).stdout
assert not probe()
with ThreadPoolExecutor(max_workers=1) as executor:
    result = executor.submit(remote, node, ['/usr/local/lib/betterdocker/runtime.py', 'service', 'restart'], timeout=180)
    while not result.done():
        assert not probe(), 'Firewall bypass during restart'
        time.sleep(.1)
    finished = result.result()
    assert finished.returncode == 0, finished.stderr
wait_for(lambda: api('get', base + '/management')['status'] == 'running')
assert not probe()
print('PASS controlled rootless runtime restart preserves workload and protection', flush=True)
old = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], check=True).stdout.strip()
try:
    remote(node, ['systemctl', 'reboot'], timeout=15)
except subprocess.TimeoutExpired:
    pass  # SSH may disappear before its reply; the changed boot ID proves reboot.
time.sleep(8)
def ready():
    try:
        boot = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], timeout=8)
        if boot.returncode != 0 or boot.stdout.strip() == old:
            return False
        result = api('get', base + '/management')
        return (result['status'] == 'running' and result['containers']
                and all(i['state'] == 'running' for i in result['containers'])
                and remote(node, ['systemctl', 'is-active', '--quiet', 'betterdocker-rootless', 'betterdocker-lifecycle', 'pve-guests'], timeout=8).returncode == 0)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
wait_for(ready, timeout=240)
identity = json.loads(remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)
restored = int(remote(node, ['systemctl', 'show', 'betterdocker-firewall-restore', '-p', 'ExecMainExitTimestampMonotonic', '--value'], check=True).stdout)
manager = int(remote(node, ['systemctl', 'show', 'user@' + str(identity['uid']), '-p', 'ExecMainStartTimestampMonotonic', '--value'], check=True).stdout)
assert 0 < restored <= manager, (restored, manager)
assert remote(node, ['systemctl', 'is-active', '--quiet', 'docker.service', 'docker.socket']).returncode != 0
assert not probe()
api('create', base + '/firewall/rules', type='in', action='ACCEPT', proto='tcp', dport='18080', enable=1)
wait_for(probe, timeout=90)
api('set', base + '/config', onboot=0)
print('PASS node reboot restores filtering before rootless user manager and onboot guest', flush=True)
