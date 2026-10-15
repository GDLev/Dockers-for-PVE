#!/usr/bin/env python3
"""Verify DCT addresses hide node services while published HTTP remains reachable.

Run on an empty, explicitly approved lab cluster. Uses only its registered DCTs;
cluster/node firewall options and existing host services are not changed.
"""
import argparse
import hashlib
import ipaddress
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path
import live_environment as env
import live_security_acceptance as live
import live_guest_lifecycle as lifecycle
from acceptance_report import Report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--address-offset', type=int, default=214)
    args = parser.parse_args()
    path = args.report
    assert not path.exists()
    r = Report(path, args.revision, {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    before = live.resources()
    assert not before
    network = ipaddress.IPv4Interface(env.config()['addresses']).network

    def reachable(address, port):
        try:
            with socket.create_connection((address, port), timeout=3):
                return True
        except OSError:
            return False
    try:
        for index, node in enumerate((env.node('secondary'), env.node('rootless'))):
            env.require_node(node)
            ip = env.address(args.address_offset + index)
            vmid = int(live.api('get', '/cluster/nextid'))
            while any((item['vmid'] == vmid for item in r.data['fixtures'])):
                vmid += 1
            item = r.plan_guest(node, vmid, 'endpoint-guard-' + r.data['run'][:8] + '-' + str(index))
            compose = {'services': {'app': {
                'image': 'busybox:1.37', 'pull_policy': 'never', 'init': True,
                'command': ['sh', '-c', 'mkdir -p /www; printf %s ' + r.data['run'] +
                            ' >/www/index.html; exec httpd -f -p 80 -h /www'],
                'cpus': 0.25, 'mem_limit': '64m', 'pids_limit': 64,
                'read_only': True, 'tmpfs': ['/www:rw,size=1m'], 'ports': ['18087:80'],
                'labels': {'dev.gdlev.acceptance-run': r.data['run']},
            }}}
            data = dict(vmid=item['vmid'], name=item['name'], mode='compose', compose=json.dumps(compose),
                        interfaces={'net0': dict(bridge='vmbr0', ipv4mode='static',
                                                cidr=ip + '/' + str(env.prefix()), ipv6mode='none', firewall=0)})
            live.task(live.api('create', '/nodes/' + node + '/docker', deployment=data, start=0))
            guest = lifecycle.own(r, item)
            assert guest
            live.task(live.api('set', live.base(guest) + '/security-policy', mode='hardened', digest=live.api('get', live.base(guest) + '/management')['digest']))
            live.power(guest, 'start')
            for attempt in range(30):
                try:
                    with urllib.request.urlopen('http://' + ip + ':18087/', timeout=3) as response:
                        assert response.read().decode() == r.data['run']
                    break
                except OSError:
                    if attempt == 29:
                        raise
                    time.sleep(1)
            hosts = live.remote(node, ['hostname', '-I'], check=True).stdout.split()
            host = next(x for x in hosts if ':' not in x and x != ip and ipaddress.IPv4Address(x) in network)
            assert reachable(host, 8006), 'Management positive control is not reachable'
            exposed = reachable(ip, 8006)
            r.data.setdefault('observations', []).append(dict(node=node, endpoint=ip, management_positive_control=host, host_management_exposed=exposed))
            r.save()
            r.check(node + ' unpublished host management port while running', 'fail' if exposed else 'pass', 'Published HTTP works; ordinary TCP connection to PVE port on the dedicated DCT address ' + ('succeeded unexpectedly' if exposed else 'was refused/timed out'))
            live.power(guest, 'shutdown')
            exposed = reachable(ip, 8006)
            r.check(node + ' unpublished host management port while stopped', 'fail' if exposed else 'pass', 'Guest stopped; dedicated address ' + ('still accepts the host management connection' if exposed else 'does not expose host management'))
            lifecycle.cleanup(r)
            assert live.resources() == before
            assert reachable(host, 8006), 'Node management changed after fixture cleanup'
    except BaseException as error:
        r.check('endpoint host access verification', 'fail', str(error)[-1800:])
        raise
    finally:
        lifecycle.cleanup(r)
        assert live.resources() == before
        r.check('owned fixture cleanup', 'pass', 'Only registered guests removed; management service remains reachable')
        r.finish()
    assert r.data['status'] == 'passed'


if __name__ == '__main__':
    main()
