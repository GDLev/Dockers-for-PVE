"""Learn IPv6 endpoints without allowing RA or DHCP to change host routing."""
import ipaddress
import errno
import json
import os
from pathlib import Path
import socket
import struct
import time


def cache(info):
    from . import projects as w
    return w.ROOT / info['id'] / ('.ipv6-' + info['net'] + '.json')


def router_prefix(packet, mac):
    if len(packet) < 16 or packet[:2] != b'\x86\x00':
        return None
    index = 16
    while index + 2 <= len(packet):
        kind, units = packet[index:index + 2]
        length = units * 8
        if not length or index + length > len(packet):
            return None
        option = packet[index:index + length]
        if kind == 3 and length == 32 and option[2] == 64 and option[3] & 0x40:
            lifetime = struct.unpack('!I', option[4:8])[0]
            preferred = struct.unpack('!I', option[8:12])[0]
            if lifetime and preferred <= lifetime:
                prefix = ipaddress.IPv6Address(option[16:32])
                if prefix.is_multicast or prefix.is_link_local or prefix.is_unspecified:
                    return None
                link = bytearray.fromhex(mac.replace(':', ''))
                link[0] ^= 2
                iid = int.from_bytes(link[:3] + b'\xff\xfe' + link[3:])
                return {'cidr6': str(ipaddress.IPv6Interface(str(ipaddress.IPv6Address((int(prefix) >> 64 << 64) | iid)) + '/64')),
                        'expires': int(time.time()) + lifetime, 'preferred': int(time.time()) + preferred,
                        'renew': int(time.time()) + min(300, max(1, lifetime // 2))}
        index += length
    return None


def discover(info, address=True):
    index = socket.if_nametoindex(info['interface'])
    with socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6) as client:
        client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
        client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVHOPLIMIT, 1)
        client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVPKTINFO, 1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, info['interface'].encode() + b'\0')
        client.settimeout(.5)
        solicitation = b'\x85\x00\x00\x00\x00\x00\x00\x00\x01\x01' + bytes.fromhex(info['hwaddr'].replace(':', ''))
        deadline = time.monotonic() + 5
        sent = False
        while time.monotonic() < deadline:
            if not sent:
                try:
                    client.sendto(solicitation, ('ff02::2', 0, 0, index))
                    sent = True
                except OSError as error:
                    if error.errno not in (errno.EADDRNOTAVAIL, errno.ENETUNREACH):
                        raise
                    # A new link-local address may still be undergoing DAD.
                    time.sleep(.1)
                    continue
            try:
                packet, control, _, sender = client.recvmsg(4096, 128)
            except socket.timeout:
                continue
            hops = next((struct.unpack('i', data)[0] for level, kind, data in control
                         if level == socket.IPPROTO_IPV6 and kind == socket.IPV6_HOPLIMIT), None)
            device = next((struct.unpack('16sI', data)[1] for level, kind, data in control
                           if level == socket.IPPROTO_IPV6 and kind == socket.IPV6_PKTINFO), None)
            if hops != 255 or device != index or not ipaddress.IPv6Address(sender[0]).is_link_local:
                continue
            if len(packet) < 16 or packet[:2] != b'\x86\x00':
                continue
            router_lifetime = struct.unpack('!H', packet[6:8])[0]
            if not address:
                return {'gw6': sender[0], 'router_expires': int(time.time()) + router_lifetime} if router_lifetime else {}
            value = router_prefix(packet, info['hwaddr'])
            if value:
                if router_lifetime:
                    value['gw6'] = sender[0]
                    value['router_expires'] = int(time.time()) + router_lifetime
                return value
    return None


def lease(info, wait=False):
    from . import projects as w
    path = cache(info)
    saved = json.loads(path.read_text()) if path.exists() else {}
    boot = w.network_boot_id()
    if saved.get('boot') != boot or saved.get('hwaddr') != info['hwaddr'] or saved.get('mode') != info['ipv6mode']:
        saved = {}
    if info['ipv6mode'] == 'auto' and saved.get('renew', 0) <= time.time():
        value = discover(info)
        if value:
            saved = dict(value, boot=boot, hwaddr=info['hwaddr'], mode='auto')
            w.write_json(path, saved)
    elif info['ipv6mode'] == 'dhcp':
        w.run(['systemctl', 'start', 'betterdocker-dhcp6@' + info['id'] + '-' + info['net'] + '.service'], capture_output=True)
        deadline = time.monotonic() + (30 if wait else 0)
        while not saved.get('cidr6') or saved.get('expires', 0) <= time.time():
            if time.monotonic() >= deadline:
                break
            time.sleep(.2)
            saved = json.loads(path.read_text()) if path.exists() else {}
            if saved.get('boot') != boot or saved.get('hwaddr') != info['hwaddr'] or saved.get('mode') != info['ipv6mode']:
                saved = {}
    if saved.get('expires', 0) > time.time():
        if info['ipv6mode'] == 'dhcp' and saved.get('router_checked', 0) + 300 <= time.time():
            router = discover(info, address=False)
            saved.pop('gw6', None)
            saved.pop('router_expires', None)
            saved.update(router or {}, router_checked=int(time.time()))
            w.write_json(path, saved)
        if saved.get('router_expires', saved['expires']) <= time.time():
            saved.pop('gw6', None)
        return {key: saved[key] for key in ('cidr6', 'gw6', 'expires', 'preferred') if key in saved}
    if wait:
        raise ValueError('No IPv6 address received for ' + info['net'])
    return None


def client(identifier, release=False):
    from . import projects as w
    from .endpoints import resolved
    identifier, _, name = identifier.partition('-')
    w.project_id(identifier)
    info = resolved(w.detail(identifier, w.current_node())).get(name)
    if not info or info.get('ipv6mode') != 'dhcp':
        raise ValueError('Unknown DHCPv6 endpoint')
    directory = w.ROOT / identifier
    config = directory / ('dhclient-' + name + '-v6.conf')
    config.write_text('timeout 10; retry 5;\n')
    config.chmod(0o600)
    command = ['dhclient', '-6', '-cf', str(config), '-lf', str(directory / ('dhclient-' + name + '-v6.leases')),
               '-pf', '/run/betterdocker-dhcp-' + identifier + '-' + name + '-v6.pid',
               '-sf', str(w.HELPERS / 'dhcp-hook.py'), '-r' if release else '-d', info['interface']]
    if release:
        w.run(command)
    else:
        os.execvpe(command[0], command, {'PATH': os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin')})


def event(environment):
    from . import projects as w
    from .endpoints import resolved
    endpoint = next((endpoint for info in w.summaries() if info['node'] == w.current_node()
                     for endpoint in resolved(info).values() if endpoint['interface'] == environment.get('interface')
                     and endpoint.get('ipv6mode') == 'dhcp'), None)
    if not endpoint:
        raise ValueError('DHCPv6 hook received an unowned interface')
    reason = environment.get('reason')
    if reason in ('BOUND6', 'RENEW6', 'REBIND6'):
        address = ipaddress.IPv6Address(environment['new_ip6_address'])
        if address.is_multicast or address.is_link_local or address.is_unspecified or address.is_loopback:
            raise ValueError('Invalid DHCPv6 address')
        lifetime = int(environment.get('new_max_life', '0'))
        if not 0 < lifetime <= 4294967295:
            raise ValueError('Invalid DHCPv6 lifetime')
        prefix = int(environment.get('new_ip6_prefixlen', '64'))
        if not 0 <= prefix <= 128:
            raise ValueError('Invalid DHCPv6 prefix length')
        path = cache(endpoint)
        previous = json.loads(path.read_text()) if path.exists() else {}
        router = {key: previous[key] for key in ('gw6', 'router_expires', 'router_checked') if key in previous} if (
            previous.get('boot') == w.network_boot_id() and previous.get('hwaddr') == endpoint['hwaddr']) else {}
        w.write_json(path, dict(router, **{'cidr6': str(address) + '/' + str(prefix), 'mode': 'dhcp',
            'expires': int(time.time()) + lifetime, 'preferred': int(time.time()) + min(lifetime, max(0, int(environment.get('new_preferred_life', '0')))),
            'hwaddr': endpoint['hwaddr'], 'boot': w.network_boot_id()}))
    elif reason in ('EXPIRE6', 'STOP6', 'RELEASE6'):
        cache(endpoint).unlink(missing_ok=True)
