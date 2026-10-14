"""Per-interface published endpoints. Docker's private networks stay separate."""
import copy
import ipaddress
import json
import math
import re
import hashlib

KEYS = ('bridge', 'ipv4mode', 'cidr', 'hwaddr', 'mtu', 'firewall', 'gw', 'ipv6mode', 'cidr6', 'gw6', 'tag', 'link_down', 'rate', 'ports')


def interfaces(info):
    if info.get('interfaces') is not None:
        return copy.deepcopy(info['interfaces'])
    return {'net0': {key: info.get(key, '') for key in KEYS if key in info} | {
        'bridge': info['bridge'], 'ipv4mode': info.get('ipv4mode', 'static'),
        'cidr': info.get('cidr') or (info.get('address', '') if info.get('ipv4mode', 'static') == 'static' else ''),
        'hwaddr': info.get('hwaddr', ''), 'mtu': info.get('mtu', 0), 'firewall': int(info.get('firewall', 0))}}


def integer(value, minimum, maximum, label):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or not minimum <= int(value) <= maximum:
        raise ValueError('Invalid ' + label)
    return int(value)


def validate(values):
    if not isinstance(values, dict) or not values or len(values) > 16 or 'net0' not in values:
        raise ValueError('Configure net0 and at most 16 interfaces')
    result, addresses, macs, assignments = {}, set(), set(), set()
    for name, raw in values.items():
        if not re.fullmatch(r'net(?:[0-9]|1[0-5])', name) or not isinstance(raw, dict) or set(raw) - set(KEYS):
            raise ValueError('Invalid network interface configuration')
        value = dict(raw)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', str(value.get('bridge', ''))):
            raise ValueError('Select a Linux bridge')
        mode = value.get('ipv4mode', 'static')
        if mode not in ('static', 'dhcp', 'none') :
            raise ValueError('Choose static IPv4, DHCP or no IPv4 address')
        value['ipv4mode'] = mode
        value['cidr'] = str(ipaddress.IPv4Interface(value.get('cidr', ''))) if mode == 'static' else ''
        mode6 = value.get('ipv6mode', 'none')
        if mode6 not in ('static', 'auto', 'dhcp', 'none'):
            raise ValueError('Choose static IPv6, SLAAC, DHCPv6 or no IPv6 address')
        value['ipv6mode'] = mode6
        value['cidr6'] = str(ipaddress.IPv6Interface(value.get('cidr6', ''))) if mode6 == 'static' else ''
        for cidr in (value['cidr'], value['cidr6']):
            if not cidr:
                continue
            address = ipaddress.ip_interface(cidr).ip
            if address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local:
                raise ValueError('Choose a usable endpoint address')
            if str(address) in addresses:
                raise ValueError('Interfaces cannot share an address')
            addresses.add(str(address))
        for key, cidr, family in (('gw', 'cidr', 4), ('gw6', 'cidr6', 6)):
            gateway = value.get(key, '') or ''
            if gateway:
                address = ipaddress.ip_address(gateway)
                link_local_router = family == 6 and address.is_link_local
                if address.version != family or not value[cidr] or (address not in ipaddress.ip_interface(value[cidr]).network and not link_local_router) or address == ipaddress.ip_interface(value[cidr]).ip or address.is_multicast or address.is_unspecified or address.is_loopback:
                    raise ValueError('Gateway must belong to the interface subnet')
                gateway = str(address)
            value[key] = gateway
        mac = str(value.get('hwaddr', '')).lower().strip()
        if mac and (not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac) or int(mac[:2], 16) & 1 or mac == '00:00:00:00:00:00' or mac in macs):
            raise ValueError('Choose a unique unicast MAC address')
        if mac:
            macs.add(mac)
        value['hwaddr'] = mac
        value['mtu'] = integer(value.get('mtu', 0) or 0, 0, 9216, 'MTU')
        if value['mtu'] and value['mtu'] < (1280 if mode6 != 'none' else 576):
            raise ValueError('MTU is too small for the configured IP family')
        value['tag'] = integer(value.get('tag', 0) or 0, 0, 4094, 'VLAN tag')
        for key in ('firewall', 'link_down'):
            if value.get(key, 0) not in (0, 1, False, True):
                raise ValueError('Invalid ' + key)
            value[key] = int(value.get(key, 0))
        rate = float(value.get('rate', 0) or 0)
        if not math.isfinite(rate) or not 0 <= rate <= 100000:
            raise ValueError('Invalid rate limit')
        value['rate'] = rate
        ports = value.get('ports', '') or ''
        if not isinstance(ports, str) or len(ports) > 8192:
            raise ValueError('Port assignments must be text')
        names = [item.strip() for item in re.split(r'[,\n]', ports) if item.strip()]
        for selector in names:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+:[0-9]+(?:-[0-9]+)?/(tcp|udp|sctp)', selector) or selector in assignments:
                raise ValueError('Use unique service:published-port/protocol assignments')
            assignments.add(selector)
        value['ports'] = ','.join(names)
        result[name] = value
    return dict(sorted(result.items(), key=lambda entry: int(entry[0][3:])))


def link_name(identifier, name):
    return 'bd' + identifier[:12] if name == 'net0' else 'b' + identifier[:10] + 'n' + name[3:]


def assign_macs(values, identifier):
    """Persist generated addresses so repairs and migrations keep their identity."""
    result = copy.deepcopy(values)
    for name, value in result.items():
        if not value.get('hwaddr'):
            octets = bytes.fromhex(identifier[:10]) if name == 'net0' else hashlib.sha256((identifier + ':' + name).encode()).digest()[:5]
            value['hwaddr'] = '02:' + ':'.join(f'{byte:02x}' for byte in octets)
    return result


def runtime_info(info, name, value):
    ipv4 = str(ipaddress.IPv4Interface(value['cidr']).ip) if value.get('cidr') else (info.get('address', '') if name == 'net0' else info.get('endpoint_addresses', {}).get(name, ''))
    mac = value.get('hwaddr') or (info.get('hwaddr') if name == 'net0' else None) or '02:' + ':'.join(info['id'][i:i+2] for i in range(0, 8, 2)) + ':' + f'{int(name[3:]):02x}'
    result = dict(info, **value)
    if value.get('ipv6mode') in ('auto', 'dhcp'):
        result.update(info.get('endpoint_ipv6', {}).get(name, {}))
    result.update(net=name, address=ipv4, hwaddr=mac, interface=link_name(info['id'], name), owner='betterdocker:' + info['id'] + ':' + name)
    return result


def resolved(info):
    return {name: runtime_info(info, name, value) for name, value in interfaces(info).items()}


def bind_runtime(config, info):
    values = resolved(info)
    selectors = {selector: name for name, value in values.items() for selector in value.get('ports', '').split(',') if selector}
    used = set()
    for service, definition in config['services'].items():
        ports, seen = [], set()
        for port in definition.get('ports', []):
            key = (str(port['published']), str(port['target']), port.get('protocol', 'tcp'))
            if key in seen:
                continue
            seen.add(key)
            selector = service + ':' + key[0] + '/' + key[2]
            used.add(selector)
            endpoint = values[selectors.get(selector, 'net0')]
            addresses = [endpoint.get('address')] if endpoint.get('address') else []
            if endpoint.get('cidr6'):
                addresses.append(str(ipaddress.IPv6Interface(endpoint['cidr6']).ip))
            if not addresses:
                raise ValueError('An endpoint publishing ports must have an address')
            ports.extend(dict(port, host_ip=address) for address in addresses)
        definition['ports'] = ports
    if set(selectors) - used:
        raise ValueError('An assigned published port is missing from the deployment')
    return config


def port_bindings(ports):
    result = {}
    for port in ports:
        public = str(port['published']).split('-'); target = str(port['target']).split('-')
        pstart, pend = int(public[0]), int(public[-1]); tstart, tend = int(target[0]), int(target[-1])
        if (pend - pstart != tend - tstart and tstart != tend) or not 1 <= pstart <= pend <= 65535 or not 1 <= tstart <= tend <= 65535:
            raise ValueError('Invalid published port range')
        if tstart == tend and pend > pstart:
            # Docker selects an available port in this range for each replica.
            result.setdefault(str(tstart) + '/' + port.get('protocol', 'tcp'), []).append({'HostIp': port['host_ip'], 'HostPort': str(port['published'])})
            continue
        for offset in range(pend - pstart + 1):
            result.setdefault(str(tstart + offset) + '/' + port.get('protocol', 'tcp'), []).append({'HostIp': port['host_ip'], 'HostPort': str(pstart + offset)})
    return result
