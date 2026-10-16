#!/usr/bin/env python3
"""Verify endpoint isolation and complete TCP/UDP exchanges in private namespaces."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading


def command(*args):
    return subprocess.run(args, text=True, capture_output=True, check=True).stdout


def exercise(policies):
    command('ip', 'link', 'set', 'lo', 'up')
    directory = Path('/run/netns')
    created = not directory.exists()
    directory.mkdir(exist_ok=True)
    command('mount', '-t', 'tmpfs', '-o', 'size=1m', 'tmpfs', str(directory))
    servers = []
    try:
        command('ip', 'netns', 'add', 'bd-guard-client')
        command('ip', 'link', 'add', 'bd-guard-host', 'type', 'veth', 'peer', 'name', 'bd-guard-peer')
        command('ip', 'link', 'set', 'bd-guard-peer', 'netns', 'bd-guard-client')
        command('ip', 'link', 'set', 'bd-guard-host', 'up')
        command('ip', '-n', 'bd-guard-client', 'link', 'set', 'lo', 'up')
        command('ip', '-n', 'bd-guard-client', 'link', 'set', 'bd-guard-peer', 'up')
        for address in ('198.18.0.1/24', '198.18.0.20/24'):
            command('ip', 'address', 'add', address, 'dev', 'bd-guard-host')
        for address in ('fd00::1/64', 'fd00::20/64'):
            command('ip', '-6', 'address', 'add', address, 'dev', 'bd-guard-host', 'nodad')
        command('ip', '-n', 'bd-guard-client', 'address', 'add', '198.18.0.2/24', 'dev', 'bd-guard-peer')
        command('ip', '-n', 'bd-guard-client', '-6', 'address', 'add', 'fd00::2/64', 'dev', 'bd-guard-peer', 'nodad')

        def listen(address, port, udp=False):
            server = socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET,
                                   socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
            if ':' in address:
                server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            server.bind((address, port))
            if not udp:
                server.listen(8)
            server.settimeout(.5)
            servers.append(server)

            def echo():
                while server.fileno() >= 0:
                    try:
                        if udp:
                            data, peer = server.recvfrom(32)
                            server.sendto(data, peer)
                        else:
                            client, _ = server.accept()
                            with client:
                                client.settimeout(2)
                                client.sendall(client.recv(32))
                    except OSError:
                        pass
            threading.Thread(target=echo, daemon=True).start()

        for address, port in [('0.0.0.0', 8006), ('::', 8006), ('198.18.0.20', 8080), ('fd00::20', 8080)]:
            listen(address, port)
        for address, port in [('0.0.0.0', 5354), ('::', 5354), ('198.18.0.20', 5353), ('fd00::20', 5353)]:
            listen(address, port, udp=True)
        probe = '''import socket,sys
address,port,protocol=sys.argv[1:]
s=socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET,socket.SOCK_DGRAM if protocol=='udp' else socket.SOCK_STREAM)
s.settimeout(1)
try:
 if protocol=='udp':
  s.sendto(b'endpoint-test',(address,int(port)));data=s.recvfrom(32)[0]
 else:
  s.connect((address,int(port)));s.sendall(b'endpoint-test');data=s.recv(32)
 print('open' if data==b'endpoint-test' else 'closed')
except OSError:print('closed')
finally:s.close()
'''
        def exchanged(address, port, protocol='tcp'):
            return command('ip', 'netns', 'exec', 'bd-guard-client', sys.executable, '-c', probe,
                           address, str(port), protocol).strip() == 'open'
        for endpoint in ('198.18.0.20', 'fd00::20'):
            assert exchanged(endpoint, 8006) and exchanged(endpoint, 8080), 'Fixture transport failed before policy'
        subprocess.run(['nft', '-f', '-'], input=policies['active'], text=True, check=True, capture_output=True)
        for endpoint, node in (('198.18.0.20', '198.18.0.1'), ('fd00::20', 'fd00::1')):
            for _ in range(3):
                assert exchanged(endpoint, 8080), 'Published TCP data blocked: ' + endpoint
            assert not exchanged(endpoint, 8006), 'Wildcard TCP host service admitted: ' + endpoint
            assert exchanged(node, 8006), 'Node TCP address blocked'
            assert exchanged(endpoint, 5353, 'udp'), 'Published UDP data blocked'
            assert not exchanged(endpoint, 5354, 'udp'), 'Wildcard UDP host service admitted'
            assert exchanged(node, 5354, 'udp'), 'Node UDP address blocked'
        command('nft', 'delete', 'table', 'inet', 'betterdocker_endpoints')
        subprocess.run(['nft', '-f', '-'], input=policies['stopped'], text=True, check=True, capture_output=True)
        for endpoint in ('198.18.0.20', 'fd00::20'):
            assert not exchanged(endpoint, 8080), 'Stopped TCP publication admitted'
            assert not exchanged(endpoint, 5353, 'udp'), 'Stopped UDP publication admitted'
        print('PASS complete IPv4/IPv6 TCP and UDP exchanges, wildcard host denial and stopped endpoint isolation')
    finally:
        for server in servers:
            server.close()
        subprocess.run(['ip', 'netns', 'del', 'bd-guard-client'], capture_output=True)
        command('umount', str(directory))
        if created:
            directory.rmdir()


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run on an isolated test node as root')
    if sys.argv[1:] == ['--namespace']:
        exercise(json.load(sys.stdin))
        return
    sys.path.insert(0, '/usr/local/lib/betterdocker')
    from dctlib.endpoint_controls import render
    identifier = 'a' * 32
    info = {'id': identifier, 'bridge': 'bd-guard-host', 'interfaces': {'net0': {
        'bridge': 'bd-guard-host', 'ipv4mode': 'static', 'cidr': '198.18.0.20/24',
        'ipv6mode': 'static', 'cidr6': 'fd00::20/64', 'firewall': 0}}}
    bindings = [(protocol, port, '10.240.1.2', port, address)
                for address in ('198.18.0.20', 'fd00::20')
                for protocol, port in (('tcp', 8080), ('tcp', 8006), ('udp', 5353), ('udp', 5354))]
    policies = {'active': render([info], {identifier: bindings}), 'stopped': render([info], {})}
    subprocess.run(['unshare', '--mount', '--net', '--propagation', 'private', sys.executable,
                    str(Path(__file__).resolve()), '--namespace'], input=json.dumps(policies), text=True, check=True)


if __name__ == '__main__':
    main()
