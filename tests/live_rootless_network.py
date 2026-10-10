#!/usr/bin/env python3
"""Check real rootless DHCP acquisition, published traffic and DNS on the test LAN."""
import json
import subprocess
import sys
import time
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
from live_firewall import remote
from live_pools_hooks import api, task, wait_for


def main():
    node = 'pve3'
    assert worker.current_node() == 'pve1'
    identity = json.loads(remote(node, ['/usr/local/lib/betterdocker/runtime.py', 'identity'], check=True).stdout)
    assert identity['mode'] == 'rootless'
    name = 'rootless-dhcp-' + uuid.uuid4().hex[:8]
    identifier = None
    try:
        deployment = dict(name=name, mode='compose', bridge='vmbr0', ipv4mode='dhcp',
            compose='services:\n  app:\n    image: busybox:stable\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo rootless-dhcp > /www/index.html; exec httpd -f -p 8080 -h /www"]\n    ports: ["80:8080"]\n')
        task(api('create', '/nodes/' + node + '/docker', deployment=json.dumps(deployment)))
        info = next(i for i in worker.summaries() if i['name'] == name)
        identifier = info['id']
        base = '/nodes/' + node + '/docker/' + identifier
        assert info['ipv4mode'] == 'dhcp' and info['address']
        def reachable():
            result = subprocess.run(['curl', '--noproxy', '*', '-fsS', '--max-time', '2', 'http://' + info['address'] + '/'], capture_output=True, text=True)
            return result.returncode == 0 and result.stdout.strip() == 'rootless-dhcp'
        wait_for(reachable, timeout=60)
        print('PASS real DHCP lease and published port 80', flush=True)
        cid = api('get', base + '/management')['containers'][0]['id']
        remote(node, ['docker', 'exec', cid, 'nslookup', 'example.org'], check=True, timeout=30)
        print('PASS DNS resolution through the rootless Docker resolver', flush=True)
        resolver = remote(node, ['python3', '-c', 'import ipaddress;from pathlib import Path;print(next(line.split()[1] for line in Path("/etc/resolv.conf").read_text().splitlines() if line.startswith("nameserver ") and ipaddress.ip_address(line.split()[1]).version==4))'], check=True).stdout.strip()
        config = api('get', base + '/management')
        task(api('set', base + '/settings', service='app', settings=json.dumps({'dns': [resolver]}), digest=config['digest']))
        task(api('create', base + '/action', action='reboot', timeout=10))
        cid = api('get', base + '/management')['containers'][0]['id']
        inspected = json.loads(remote(node, ['docker', 'inspect', cid], check=True).stdout)[0]
        assert inspected['HostConfig']['Dns'] == [resolver]
        remote(node, ['docker', 'exec', cid, 'nslookup', 'example.org'], check=True, timeout=30)
        wait_for(reachable, timeout=60)
        print('PASS configured DNS survives recreation and resolves real queries', flush=True)
    finally:
        info = next((i for i in worker.summaries() if i['name'] == name), None)
        if info:
            identifier = info['id']
            base = '/nodes/' + node + '/docker/' + identifier
            task(api('create', base + '/action', action='force-stop'))
            task(api('delete', base))
            remote(node, ['python3', '-c', 'import sys,json,shutil;sys.path.insert(0,"/usr/local/lib/betterdocker");import provision as w;p=w.ROOT/' + repr(identifier) + ';assert json.loads((p/"request.json").read_text())["name"]==' + repr(name) + ';shutil.rmtree(p)'], check=True)
            assert remote(node, ['ip', 'link', 'show', 'bd' + identifier[:12]]).returncode != 0
            print('PASS DHCP client and owned endpoint removed', flush=True)


if __name__ == '__main__':
    main()
