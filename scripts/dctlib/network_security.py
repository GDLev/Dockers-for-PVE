"""Validate acquired addresses and adapter bindings at network boundaries."""
import ipaddress
import json


def address(value, family):
    parsed = ipaddress.ip_address(value)
    if parsed.version != family or parsed.is_multicast or parsed.is_unspecified or parsed.is_loopback or parsed.is_link_local or parsed.version == 6 and parsed.ipv4_mapped is not None or family == 4 and (int(parsed) >= int(ipaddress.IPv4Address('240.0.0.0'))):
        raise ValueError('Invalid acquired IPv' + str(family) + ' address')
    return str(parsed)


def lifetime(value, name='lease lifetime', allow_zero=False):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('Invalid ' + name)
    result = int(value)
    if not (0 if allow_zero else 1) <= result <= 4294967295:
        raise ValueError('Invalid ' + name)
    return result


def acquired(info, value, family):
    from . import projects as w
    from .endpoints import resolved
    value = address(value, family)
    host = json.loads(w.capture(['ip', '-j', '-' + str(family), 'address', 'show']))
    if any(link['ifname'] != info['interface'] and any(entry.get('local') == value for entry in link.get('addr_info', [])) for link in host):
        raise ValueError('Acquired address belongs to another host interface')
    for guest in w.summaries():
        for endpoint in resolved(guest).values():
            candidate = endpoint.get('address') if family == 4 else str(ipaddress.IPv6Interface(endpoint['cidr6']).ip) if endpoint.get('cidr6') else None
            if candidate == value and (guest['id'] != info['id'] or endpoint.get('interface') != info['interface']):
                raise ValueError('Acquired address belongs to another DCT endpoint')
    return value


def dhcp4(environment):
    value = address(environment.get('new_ip_address', ''), 4)
    seconds = lifetime(environment.get('new_dhcp_lease_time', '3600'))
    mask = environment.get('new_subnet_mask')
    if mask:
        network = ipaddress.IPv4Network(value + '/' + mask, strict=False)
        if network.prefixlen < 31 and ipaddress.IPv4Address(value) in (network.network_address, network.broadcast_address):
            raise ValueError('DHCP address is a subnet or broadcast address')
    return value, seconds


def proxy_mappings(values):
    if not isinstance(values, list) or len(values) > 4096:
        raise ValueError('Invalid proxy mapping list')
    seen = set()
    result = []
    pool = ipaddress.IPv4Network('10.240.0.0/16')
    for value in values:
        if not isinstance(value, dict) or set(value) != {'address', 'port', 'protocol', 'target', 'internal'}:
            raise ValueError('Invalid proxy mapping')
        item = dict(value, address=address(value['address'], 6), target=address(value['target'], 4))
        if ipaddress.ip_address(item['target']) not in pool or item['target'] in (str(pool.network_address), str(pool.broadcast_address)) or item['protocol'] not in ('tcp', 'udp'):
            raise ValueError('Invalid proxy target or protocol')
        if any(type(item[key]) is not int or not 1 <= item[key] <= 65535 for key in ('port', 'internal')):
            raise ValueError('Invalid proxy port')
        key = (item['address'], item['port'], item['protocol'])
        if key in seen:
            raise ValueError('Duplicate proxy listener')
        seen.add(key); result.append(item)
    return result


def rootless_resources(config):
    if any(service.get('volume_driver') not in (None, '', 'local') for service in config.get('services', {}).values()):
        raise ValueError('Rootless DCT resources require the local volume driver')
    for kind in ('volumes', 'networks'):
        for definition in config.get(kind, {}).values():
            definition = definition or {}
            if definition.get('external') or definition.get('driver_opts') or definition.get('driver', 'local' if kind == 'volumes' else 'bridge') != ('local' if kind == 'volumes' else 'bridge'):
                raise ValueError('Rootless DCT resources require owned local volumes and bridge networks without driver options')


def rootless_container(body):
    from . import projects as w
    identifier = (body.get('Labels') or {}).get('betterdocker.id')
    if not identifier: raise ValueError('Rootless DCT container ownership is missing')
    host = body.get('HostConfig') or {}
    if host.get('VolumeDriver') not in (None, '', 'local'):
        raise ValueError('Rootless mounts require the local volume driver')
    for binding in host.get('Binds') or []:
        source = binding.split(':', 1)[0]
        if source.startswith('/'):
            w.safe_bind(source, identifier)
        else:
            volume(source)
    for mount in host.get('Mounts') or []:
        if mount.get('Type') == 'bind': w.safe_bind(mount.get('Source', ''), identifier)
        elif mount.get('Type') == 'volume':
            driver = (mount.get('VolumeOptions') or {}).get('DriverConfig') or {}
            if driver.get('Name') not in (None, '', 'local') or driver.get('Options'):
                raise ValueError('Rootless mounts cannot use volume driver options or external drivers')
            if mount.get('Source'):
                volume(mount['Source'])


def volume(name):
    from . import projects as w
    values = json.loads(w.capture(['docker', 'volume', 'inspect', name]))
    if len(values) != 1 or values[0].get('Driver') != 'local' or values[0].get('Options'):
        raise ValueError('Rootless mounts cannot use volume driver options or external drivers')


from contextlib import contextmanager


@contextmanager
def acquisition_lock():
    import fcntl
    import os
    from . import projects as w, operation_capacity as admission
    from .secure_io import anchored
    directory = w.ROOT.parent
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with anchored(directory,directory=True) as parent:
        fd = admission.open_lock(parent,'address-quarantine.lock')
        try:
            fcntl.flock(fd,fcntl.LOCK_EX);yield
        finally:
            os.close(fd)


def apply_quarantine(values):
    import firewall
    lines=['table inet betterdocker_acquisition {']
    for hook in ('prerouting','output'):
        lines.append('chain ' + hook + ' { type filter hook ' + hook + ' priority -300; policy accept;')
        for value in values.values():
            parsed=ipaddress.ip_address(value['address']);family='ip6' if parsed.version==6 else 'ip'
            match=family+' daddr '+str(parsed)
            if parsed.version==6:
                lines.append(match+' icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept')
            lines.append(match+' drop')
        lines.append('}')
    lines.append('}')
    firewall.apply_rules('\n'.join(lines)+'\n',table='betterdocker_acquisition',cache='acquisition.nft')


def block_address(info, value, family):
    from . import projects as w
    from .secure_io import read_json
    if not w.node_runtime.load().rootless and not info.get('firewall') and 'interfaces' not in info: return
    value=address(value,family)
    current=json.loads(w.capture(['ip','-j','-'+str(family),'address','show','dev',info['interface']]))
    if any(item.get('local')==value for link in current for item in link.get('addr_info',[])):return
    file=w.ROOT.parent/'address-quarantine.json'
    with acquisition_lock():
        values=read_json(file) if file.exists() else {}
        if len(values) >= 4096 and value not in values:
            raise ValueError('Too many pending endpoint addresses; resolve acquisition failures')
        values[value]={'id':info['id'],'interface':info['interface'],'address':value}
        apply_quarantine(values)
        w.write_json(file,values)


def release_ready(infos):
    from . import projects as w
    from .secure_io import read_json
    from .endpoints import resolved
    file=w.ROOT.parent/'address-quarantine.json'
    if not file.exists():return
    confirmed=set()
    for info in infos:
        for endpoint in resolved(info).values():
            addresses=[endpoint['address']] if endpoint.get('address') else []
            if endpoint.get('cidr6'):addresses.append(str(ipaddress.IPv6Interface(endpoint['cidr6']).ip))
            confirmed.update((info['id'],endpoint['interface'],value) for value in addresses)
    with acquisition_lock():
        values=read_json(file)
        pending={key:value for key,value in values.items() if (value['id'],value['interface'],value['address']) not in confirmed}
        if pending!=values:
            apply_quarantine(pending)
            w.write_json(file,pending)
