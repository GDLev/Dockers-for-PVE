#!/usr/bin/env python3
"""Package updates and PVE repair triggers must retain mode and live containers."""
import live_environment as env
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
import transfer
from live_firewall import remote

PACKAGE = Path('/root/betterdocker-rootless-package/proxmox-betterdockers_0.19.0_all.deb')


def containers(node):
    ids = remote(node, ['docker', 'ps', '-aq'], check=True).stdout.split()
    records = json.loads(remote(node, ['docker', 'inspect', *ids], check=True).stdout) if ids else []
    return {i['Id']: [i['State']['Status'], i['State']['StartedAt']] for i in records}


def run():
    for node in (env.node('rootless'), env.node('secondary'), env.node('primary')):
        selected = remote(node, ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout
        before = containers(node)
        package = str(PACKAGE)
        if node != env.node('primary'):
            package = '/root/betterdocker-rootless-package.deb'
            subprocess.run(transfer.ssh_command(node) + ['cat > ' + package], input=PACKAGE.read_bytes(), check=True)
        remote(node, ['bash', '-c', 'DEBIAN_FRONTEND=noninteractive dpkg -i ' + package + ' > /root/dct-rootless-package.log 2>&1'], check=True, timeout=300)
        assert remote(node, ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout == selected
        assert containers(node) == before, node + ': workloads changed during update'
        remote(node, ['dpkg-trigger', '--no-await', '/usr/share/perl5/PVE/API2/Nodes.pm'], check=True)
        remote(node, ['bash', '-c', 'dpkg --triggers-only proxmox-betterdockers > /root/dct-rootless-trigger.log 2>&1'], check=True, timeout=300)
        assert remote(node, ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout == selected
        assert containers(node) == before, node + ': workloads changed during trigger'
        alternate = 'rootful' if json.loads(selected)['mode'] == 'rootless' else 'rootless'
        rejected = remote(node, ['bash', '/opt/betterdocker/start.sh', 'repair', '--runtime=' + alternate], timeout=30)
        assert rejected.returncode != 0 and 'fixed' in rejected.stderr, rejected.stderr
        assert containers(node) == before and remote(node, ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout == selected
        print('PASS update, native trigger and mode-change rejection retain mode and workloads', node, flush=True)


if __name__ == "__main__":
    run()
