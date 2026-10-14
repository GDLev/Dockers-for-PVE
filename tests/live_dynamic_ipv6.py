#!/usr/bin/env python3
"""Verify SLAAC/DHCPv6 DCT endpoints and HTTP on isolated links."""
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from live_service_security import api, call, w


def run(*args, check=True):
    return subprocess.run(list(args), check=check, text=True, capture_output=True).stdout


def main():
    suffix = uuid.uuid4().hex[:6]
    bridge, namespace, parent = ['bd' + name + suffix for name in ('br', 'ns', 'v')]
    before = api('get', '/cluster/resources', type='vm')
    default = run('ip', '-6', '-j', 'route', 'show', 'default')
    identifier = None
    router = None
    with tempfile.TemporaryDirectory(prefix='bd-dynamic6-') as folder:
        source = Path(folder) / 'compose.json'
        source.write_text(json.dumps({'services': {'app': {'image': 'busybox:1.37', 'init': True,
            'command': ['sh', '-c', 'mkdir /www; echo dynamic-ipv6 >/www/index.html; exec httpd -f -p 8080 -h /www'], 'ports': ['8080:8080']}}}))
        vmid = next(i for i in range(940, 980) if i not in {int(row['vmid']) for row in before})
        try:
            run('ip', 'netns', 'add', namespace)
            run('ip', 'link', 'add', bridge, 'type', 'bridge')
            for setting in ('accept_ra', 'autoconf'):
                run('sysctl', '-qw', 'net.ipv6.conf.' + bridge + '.' + setting + '=0')
            run('ip', 'link', 'set', bridge, 'up')
            run('ip', 'link', 'add', parent, 'type', 'veth', 'peer', 'name', 'router', 'netns', namespace)
            run('ip', 'link', 'set', parent, 'master', bridge)
            for setting in ('accept_ra', 'autoconf'):
                run('sysctl', '-qw', 'net.ipv6.conf.' + parent + '.' + setting + '=0')
            run('ip', 'link', 'set', parent, 'up')
            run('ip', '-n', namespace, 'link', 'set', 'lo', 'up')
            run('ip', '-n', namespace, 'link', 'set', 'router', 'up')
            run('ip', '-n', namespace, '-6', 'address', 'add', 'fd42:bd20:2::1/64', 'dev', 'router')
            router_path = str(Path(__file__).with_name('ipv6_test_router.py'))
            router = subprocess.Popen(['ip', 'netns', 'exec', namespace, sys.executable, router_path, 'router', 'auto'])
            call('dct', 'create', vmid, source, '--hostname', 'dynamic-ipv6-test', '--net0', 'bridge=' + bridge + ',ip=none,ip6=auto', '--start', 1)
            row = next(row for row in api('get', '/cluster/resources', type='vm') if int(row['vmid']) == vmid)
            identifier, node = row['id'].split('/')[-1], row['node']
            base = '/nodes/' + node + '/docker/' + identifier
            def fetch():
                info = w.detail(identifier, node)
                address = str(ipaddress.IPv6Interface(info['endpoint_ipv6']['net0']['cidr6']).ip)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    result = subprocess.run(['ip', 'netns', 'exec', namespace, 'curl', '--noproxy', '*', '-fsS', '--max-time', '2', 'http://[' + address + ']:8080'], capture_output=True, text=True)
                    if result.returncode == 0:
                        assert result.stdout.strip() == 'dynamic-ipv6'
                        return address
                    time.sleep(.2)
                raise AssertionError('IPv6 endpoint HTTP failed: ' + result.stderr)
            slaac = fetch()
            assert run('ip', '-6', '-j', 'route', 'show', 'default') == default
            print('PASS: IPv6-only SLAAC DCT serves HTTP without altering host default routes', flush=True)
            call('dct', 'stop', vmid)
            router.terminate(); router.wait(timeout=5)
            router = subprocess.Popen(['ip', 'netns', 'exec', namespace, sys.executable, router_path, 'router', 'dhcp'])
            api('set', base + '/network', net='net0', ipv6mode='dhcp', digest=w.detail(identifier, node)['digest'])
            call('dct', 'start', vmid)
            assert fetch() == 'fd42:bd20:2::123'
            assert run('ip', '-6', '-j', 'route', 'show', 'default') == default
            print('PASS: DHCPv6 lease, RA gateway, address change and published HTTP preserve host routing', flush=True)
        finally:
            if not identifier:
                fixture = next((guest for guest in w.summaries() if guest.get('vmid') == vmid and guest.get('name') == 'dynamic-ipv6-test'), None)
                if fixture:
                    identifier = fixture['id']
            if identifier:
                call('dct', 'stop', vmid)
                call('dct', 'destroy', vmid, '--destroy-owned-data', 1)
            if router:
                router.terminate(); router.wait(timeout=5)
            for name in (parent, bridge):
                run('ip', 'link', 'del', name, check=False)
            run('ip', 'netns', 'del', namespace, check=False)
            assert run('ip', '-6', '-j', 'route', 'show', 'default') == default
            after = api('get', '/cluster/resources', type='vm')
            assert {row['vmid'] for row in before} == {row['vmid'] for row in after}
            print('PASS: disposable IPv6 guest and isolated topology removed', flush=True)


if __name__ == '__main__':
    main()
