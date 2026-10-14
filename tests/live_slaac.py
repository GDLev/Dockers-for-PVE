#!/usr/bin/env python3
"""Exercise the RA learner on isolated Linux links without changing host routes."""
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import ipv6_dynamic, ipv6_controls


def run(*args):
    return subprocess.run(list(args), text=True, check=True, capture_output=True).stdout


def main():
    suffix = uuid.uuid4().hex[:6]
    namespace, bridge, parent, client = ['bd' + prefix + suffix for prefix in ('ns', 'br', 'v', 'c')]
    before = json.loads(run('ip', '-6', '-j', 'route', 'show', 'table', 'main'))
    router = None
    endpoint = {'interface': client}
    code = '''import socket,struct,time,ipaddress
index=socket.if_nametoindex('router')
packet=b'\\x86\\x00\\x00\\x00'+bytes([64,0])+struct.pack('!H',1800)+bytes(8)
packet+=bytes([3,4,64,0xc0])+struct.pack('!II',600,300)+bytes(4)+ipaddress.IPv6Address('fd42:bd20:1::').packed
with socket.socket(socket.AF_INET6,socket.SOCK_RAW,socket.IPPROTO_ICMPV6) as s:
 s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_MULTICAST_IF,index)
 s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_MULTICAST_HOPS,255)
 for _ in range(40):
  try: s.sendto(packet,('ff02::1',0,0,index))
  except OSError: pass
  time.sleep(.25)
'''
    try:
        run('ip', 'netns', 'add', namespace)
        run('ip', 'link', 'add', bridge, 'type', 'bridge')
        for option in ('accept_ra', 'autoconf'):
            run('sysctl', '-qw', 'net.ipv6.conf.' + bridge + '.' + option + '=0')
        run('ip', 'link', 'set', bridge, 'up')
        run('ip', 'link', 'add', parent, 'type', 'veth', 'peer', 'name', 'router', 'netns', namespace)
        run('ip', 'link', 'set', parent, 'master', bridge)
        run('ip', 'link', 'set', parent, 'up')
        for option in ('accept_ra', 'autoconf'):
            run('sysctl', '-qw', 'net.ipv6.conf.' + parent + '.' + option + '=0')
        run('ip', '-n', namespace, 'link', 'set', 'router', 'up')
        run('ip', 'link', 'add', 'link', bridge, 'name', client, 'address', '02:11:22:33:44:55', 'type', 'macvlan', 'mode', 'bridge')
        for option in ('accept_ra', 'autoconf'):
            run('sysctl', '-qw', 'net.ipv6.conf.' + client + '.' + option + '=0')
        run('ip', 'link', 'set', client, 'up')
        ipv6_controls.ensure(endpoint)
        router = subprocess.Popen(['ip', 'netns', 'exec', namespace, 'python3', '-c', code])
        assigned = ipv6_dynamic.discover({'interface': client, 'hwaddr': '02:11:22:33:44:55'})
        assert assigned, 'No valid router advertisement received'
        assert assigned['cidr6'] == 'fd42:bd20:1:0:11:22ff:fe33:4455/64', assigned
        assert ipaddress.IPv6Address(assigned['gw6']).is_link_local
        addresses = json.loads(run('ip', '-6', '-j', 'address', 'show', 'dev', client))
        assert not any(row.get('scope') == 'global' for link in addresses for row in link.get('addr_info', []))
        current = json.loads(run('ip', '-6', '-j', 'route', 'show', 'table', 'main'))
        assert [route for route in current if route.get('dev') not in (client, parent, bridge)] == before, 'Original host routes changed'
        assert not any(route.get('dst', '').startswith('fd42:bd20:1:') for route in current)
        print('PASS: real RA discovery, EUI-64 guest address and router lifetime without host autoconfiguration', flush=True)
    finally:
        if router:
            router.terminate(); router.wait(timeout=5)
        ipv6_controls.ensure(endpoint, remove=True)
        for name in (client, parent, bridge):
            subprocess.run(['ip', 'link', 'del', name], capture_output=True)
        subprocess.run(['ip', 'netns', 'del', namespace], capture_output=True)
        assert json.loads(run('ip', '-6', '-j', 'route', 'show', 'table', 'main')) == before
        print('PASS: isolated links removed, original host routes unchanged', flush=True)


if __name__ == '__main__':
    main()
