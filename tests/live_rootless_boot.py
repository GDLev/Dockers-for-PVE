#!/usr/bin/env python3
"""Confirm the kept rootless fixture recovers without starting host Docker."""
import live_environment as env
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
from live_firewall import remote
from live_pools_hooks import api, task, wait_for


def main():
    assert worker.current_node() == env.node('primary')
    node = env.node('rootless')
    state = json.loads(remote(node, ['cat', '/root/betterdocker-rootless-fixture.json'], check=True).stdout)
    base = '/nodes/' + node + '/docker/' + state['id']
    assert remote(node, ['systemctl', 'is-active', '--quiet', 'docker.service', 'docker.socket']).returncode != 0
    deps = remote(node, ['systemctl', 'show', 'betterdocker-lifecycle', '-p', 'Requires', '-p', 'BindsTo', '-p', 'After'], check=True).stdout
    assert 'docker.service' not in deps and 'betterdocker-rootless.service' in deps, deps
    task(api('create', base + '/action', action='start'))
    api('set', base + '/config', onboot=1)
    def reachable():
        result = subprocess.run(['curl', '--noproxy', '*', '-fsS', '--max-time', '2', 'http://' + state['address'] + '/'], text=True, capture_output=True)
        return result.returncode == 0 and result.stdout.strip() == 'original'
    wait_for(reachable, timeout=60)
    code = '''import sys,json
from pathlib import Path
sys.path.insert(0,"/usr/local/lib/betterdocker")
import runtime,provision as w
r=runtime.load();cid=w.container_records(IDENTIFIER)[0]["id"]
full=json.loads(w.capture(["docker","inspect",cid]))[0]["Id"]
manager=Path("/sys/fs/cgroup/user.slice")/("user-"+str(r.uid)+".slice")/("user@"+str(r.uid)+".service")
cgroup=next(manager.rglob("docker-"+full+".scope"))
assert (cgroup/"memory.max").read_text().strip()=="67108864"
assert (cgroup/"pids.max").read_text().strip()=="64"
quota,period=(cgroup/"cpu.max").read_text().split();assert int(quota)/int(period)==0.5
print("PASS actual delegated cgroup CPU, memory and PID limits")
'''.replace('IDENTIFIER', repr(state['id']))
    print(remote(node, ['python3', '-c', code], check=True).stdout.strip(), flush=True)
    remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'service', 'restart'], check=True, timeout=180)
    wait_for(reachable, timeout=90)
    assert remote(node, ['systemctl', 'is-active', '--quiet', 'docker.service', 'docker.socket']).returncode != 0
    print('PASS runtime restart recovers actual traffic without host Docker', flush=True)
    old = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], check=True).stdout.strip()
    try:
        remote(node, ['systemctl', 'reboot'], timeout=15)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(8)
    def ready():
        try:
            boot = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], timeout=8)
            if boot.returncode or boot.stdout.strip() == old:
                return False
            info = api('get', base + '/management')
            return (info['containers'] and all(i['state'] == 'running' for i in info['containers'])
                    and remote(node, ['systemctl', 'is-active', '--quiet', 'pve-guests', 'betterdocker-lifecycle', 'betterdocker-rootless'], timeout=8).returncode == 0 and reachable())
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return False
    wait_for(ready, timeout=240)
    assert remote(node, ['systemctl', 'is-active', '--quiet', 'docker.service', 'docker.socket']).returncode != 0
    identity = json.loads(remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)
    assert identity['mode'] == 'rootless'
    api('set', base + '/config', onboot=0)
    print('PASS real node reboot and onboot recover rootless DCT without host Docker', flush=True)


if __name__ == '__main__':
    main()
