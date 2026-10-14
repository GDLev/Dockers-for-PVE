#!/usr/bin/env python3
"""Test daemon loss, quorum refusal and reboot on an otherwise empty test node.

Run from another cluster node. Only --reboot requests a node reboot.
The fixture manifest stays on the controller until cleanup completes.
"""
import live_environment as env
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_firewall import remote
from live_pools_hooks import api, task, wait_for

STATE = Path('/root/bd-runtime-failures.json')


def cleanup():
    saved = json.loads(STATE.read_text())
    node, identifier = saved['node'], saved.get('id')
    remote(node, ['systemctl', 'start', 'corosync'], check=True)
    remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'service', 'start'], check=True)
    if identifier:
        info = next((row for row in w.summaries() if row['id'] == identifier), None)
        if info:
            assert info['node'] == node and info['name'] == saved['name']
            code = 'import sys,json;sys.path.insert(0,"/usr/local/lib/betterdocker");from dctlib import projects as w;print(json.dumps(w.operation_state(sys.argv[1])))'
            wait_for(lambda: not json.loads(remote(node, ['python3', '-c', code, identifier], check=True).stdout).get('active'), timeout=120)
            task(api('create', '/nodes/' + node + '/docker/' + identifier + '/action', action='force-stop'))
            task(api('delete', '/nodes/' + node + '/docker/' + identifier, **{'destroy-owned-data': 1}))
    STATE.unlink()
    print('PASS fault-test fixture removed and services restored', flush=True)


def run(node, reboot):
    assert node != w.current_node()
    assert not STATE.exists(), 'Inspect the previous fixture before retrying'
    assert not any(row['node'] == node for row in w.summaries()), 'Target must have no DCTs'
    assert not remote(node, ['docker', 'ps', '-aq'], check=True).stdout.strip(), 'Target Docker must be empty'
    name = 'runtime-failure-' + uuid.uuid4().hex[:8]
    saved = {'node': node, 'name': name}
    STATE.write_text(json.dumps(saved))
    identifier = None
    try:
        source = 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    restart: unless-stopped\n    command: [sleep, "86400"]\n'
        deployment = {'name': name, 'mode': 'compose', 'bridge': 'vmbr0', 'ipv4mode': 'none', 'compose': source}
        task(api('create', '/nodes/' + node + '/docker', deployment=json.dumps(deployment), start=1))
        info = next(row for row in w.summaries() if row['name'] == name)
        identifier = info['id']; saved.update(id=identifier, vmid=info['vmid']); STATE.write_text(json.dumps(saved))
        cid = remote(node, ['docker', 'ps', '-q', '--no-trunc', '--filter', 'label=betterdocker.id=' + identifier], check=True).stdout.strip()
        remote(node, ['docker', 'exec', cid, 'sh', '-c', 'echo kept >/marker'], check=True)
        remote(node, ['dct', 'set', str(info['vmid']), '--onboot', '1'], check=True)
        remote(node, ['/usr/local/lib/betterdocker/provision.py', 'daemon-prepare'], check=True)
        remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'service', 'stop'], check=True)
        probe = '''import sys,subprocess
sys.path.insert(0,"/usr/local/lib/betterdocker")
from dctlib import projects as w
try: w.container_records(sys.argv[1])
except (ValueError, OSError, subprocess.SubprocessError): print("unavailable")
else: raise AssertionError("Unavailable daemon was interpreted as empty")
'''
        assert remote(node, ['python3', '-c', probe, identifier], check=True).stdout.strip() == 'unavailable'
        remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'service', 'start'], check=True)
        remote(node, ['/usr/local/lib/betterdocker/provision.py', 'daemon-resume'], check=True)
        wait_for(lambda: remote(node, ['docker', 'exec', cid, 'cat', '/marker']).stdout.strip() == 'kept', timeout=120)
        print('PASS unavailable daemon is rejected; original instance and files return', flush=True)
        remote(node, ['systemctl', 'stop', 'corosync'], check=True)
        try:
            wait_for(lambda: remote(node, ['pvecm', 'status']).returncode != 0, timeout=30)
            request = remote(node, ['pvesh', 'create', '/nodes/' + node + '/docker/' + identifier + '/migrate', '--target', w.current_node()])
            assert request.returncode and 'quorum' in request.stderr.lower(), request.stdout + request.stderr
            print('PASS migration refuses lost quorum before ownership changes', flush=True)
        finally:
            remote(node, ['systemctl', 'start', 'corosync'], check=True)
            wait_for(lambda: remote(node, ['pvecm', 'status']).returncode == 0, timeout=90)
        if reboot:
            boot = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], check=True).stdout.strip()
            try:
                remote(node, ['reboot'], timeout=10)
            except subprocess.TimeoutExpired:
                pass
            def resumed():
                try:
                    current = remote(node, ['cat', '/proc/sys/kernel/random/boot_id'], timeout=5)
                    return current.returncode == 0 and current.stdout.strip() != boot and remote(node, ['docker', 'exec', cid, 'cat', '/marker'], timeout=5).stdout.strip() == 'kept'
                except subprocess.TimeoutExpired:
                    return False
            wait_for(resumed, timeout=300)
            assert next(row for row in w.summaries() if row['id'] == identifier)['node'] == node
            print('PASS reboot restores the same instance, writable files and guest ownership', flush=True)
    finally:
        cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default=env.node('rootless'))
    parser.add_argument('--reboot', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    cleanup() if args.cleanup else run(args.node, args.reboot)
