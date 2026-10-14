"""Host links, leases and transactions for published DCT interfaces."""
from .secure_io import read_json, read_text
import ipaddress
import json
import os
from pathlib import Path
import subprocess
from . import endpoints as model


def preflight(data, identifier=None):
    from . import projects as w
    links = json.loads(w.capture(['ip', '-j', '-d', 'link', 'show']))
    all_addresses = json.loads(w.capture(['ip', '-j', 'address', 'show']))
    other = []
    for guest in w.summaries():
        if guest['id'] != identifier:
            other.extend(model.resolved(guest).values())
    for name, value in model.validate(data['interfaces']).items():
        bridge = next((link for link in links if link['ifname'] == value['bridge']), None)
        if not bridge or not Path('/sys/class/net', value['bridge'], 'bridge').is_dir():
            raise ValueError('Select a Linux bridge')
        w.check_sdn_bridge(value['bridge'])
        if value['tag'] and not bridge.get('linkinfo', {}).get('info_data', {}).get('vlan_filtering'):
            raise ValueError('VLAN tags require a VLAN-aware bridge')
        if value['mtu'] and value['mtu'] > bridge['mtu']:
            raise ValueError('MTU cannot exceed bridge MTU')
        if not w.trusted_management():
            actor = os.environ.get('BETTERDOCKER_TASK_USER')
            if not actor:
                raise ValueError('Missing authenticated network owner')
            program = "use PVE::RPCEnvironment; use PVE::GuestHelpers; my $r=PVE::RPCEnvironment->init('cli');$r->init_request();$r->set_user($ARGV[0]);PVE::GuestHelpers::check_vnet_access($r,$ARGV[0],$ARGV[1],$ARGV[2] || undef,undef);"
            w.capture(['perl', '-e', program, actor, value['bridge'], str(value['tag'])])
        owned = model.link_name(identifier, name) if identifier else None
        if value['hwaddr'] and any(link['ifname'] != owned and link.get('address', '').lower() == value['hwaddr'] for link in links):
            raise ValueError('MAC address already belongs to a host interface')
        for key in ('cidr', 'cidr6'):
            if not value[key]:
                continue
            address = str(ipaddress.ip_interface(value[key]).ip)
            if any(link['ifname'] != owned and any(entry.get('local') == address for entry in link.get('addr_info', [])) for link in all_addresses):
                raise ValueError('Address already belongs to the host')
            if any(row.get('address') == address or row.get('cidr6') and str(ipaddress.IPv6Interface(row['cidr6']).ip) == address for row in other):
                raise ValueError('Address belongs to another DCT')
            already = any(link['ifname'] == owned and any(e.get('local') == address for e in link.get('addr_info', [])) for link in all_addresses)
            if key == 'cidr' and not value['tag']:
                _, subnets = w.bridge_addresses(value['bridge'])
                if not any(ipaddress.IPv4Interface(value[key]).network == subnet for subnet in subnets):
                    raise ValueError('IPv4/CIDR must match a connected bridge subnet')
            if not already and not value['tag']:
                command = ['arping', '-D', '-I', value['bridge'], '-c', '3', '-w', '4', address] if key == 'cidr' else ['ndisc6', '-q', '-r', '1', address, value['bridge']]
                result = subprocess.run(command, capture_output=True, timeout=10)
                if (key == 'cidr' and result.returncode != 0) or (key == 'cidr6' and result.returncode == 0):
                    raise ValueError('Address responded to duplicate-address detection')
        if value['hwaddr'] and any(row.get('hwaddr') == value['hwaddr'] for row in other):
            raise ValueError('MAC address belongs to another DCT')


def ensure_link(info):
    from . import projects as w
    links = json.loads(w.capture(['ip', '-j', '-d', 'link', 'show']))
    parent = info['bridge']
    if info.get('tag'):
        parent = info['interface'][:11] + 'v' + info['net'][3:]
        existing = next((link for link in links if link['ifname'] == parent), None)
        if existing and (existing.get('ifalias') != info['owner'] or existing.get('linkinfo', {}).get('info_data', {}).get('id') != info['tag'] or existing.get('link') != info['bridge']):
            raise ValueError('VLAN device ownership changed')
        if not existing:
            w.run(['ip', 'link', 'add', 'link', info['bridge'], 'name', parent, 'type', 'vlan', 'id', str(info['tag'])])
            w.run(['ip', 'link', 'set', 'dev', parent, 'alias', info['owner']])
        w.run(['ip', 'link', 'set', 'dev', parent, 'up'])
    existing = next((link for link in links if link['ifname'] == info['interface']), None)
    aliases = (info['owner'], 'betterdocker:' + info['id']) if info['net'] == 'net0' else (info['owner'],)
    if existing and (existing.get('ifalias') not in aliases or existing.get('linkinfo', {}).get('info_kind') != 'macvlan' or existing.get('link') != parent):
        raise ValueError('Endpoint device ownership changed')
    if not existing:
        w.run(['ip', 'link', 'add', 'link', parent, 'name', info['interface'], 'address', info['hwaddr'], 'type', 'macvlan', 'mode', 'bridge'])
    w.run(['ip', 'link', 'set', 'dev', info['interface'], 'alias', info['owner']])
    for setting, value in (('accept_ra', '0'), ('autoconf', '0'), ('disable_ipv6', '0' if info.get('cidr6') or info.get('ipv6mode') in ('auto', 'dhcp') else '1')):
        path = Path('/proc/sys/net/ipv6/conf') / info['interface'] / setting
        if path.exists():
            w.run(['sysctl', '-q', '-w', 'net.ipv6.conf.' + info['interface'] + '.' + setting + '=' + value])
    if existing and existing.get('address', '').lower() != info['hwaddr']:
        raise ValueError('Endpoint MAC address changed')
    if info.get('mtu'):
        w.run(['ip', 'link', 'set', 'dev', info['interface'], 'mtu', str(info['mtu'])])
    w.run(['ip', 'link', 'set', 'dev', info['interface'], 'up'])
    if info.get('ipv6mode', 'none') != 'none':
        from .ipv6_controls import ensure
        ensure(info)
    return parent


def route_table(info, allocate=True):
    # These tables contain only DCT source routes; the node's main table stays intact.
    from . import projects as w
    key = info['id'] + ':' + info['net']
    registry = w.ROOT.parent / 'route-tables.json'
    saved = read_json(registry) if registry.exists() else {}
    if key not in saved:
        if not allocate:
            return None
        existing = set(saved.values())
        table = 100000
        while table in existing:
            table += 1
        saved[key] = table
        w.write_json(registry, saved)
    return saved[key]


def routing(info, remove=False):
    from . import projects as w
    table = str(route_table(info))
    for version, key, gateway in ((4, 'cidr', 'gw'), (6, 'cidr6', 'gw6')):
        rules = json.loads(w.capture(['ip', '-' + str(version), '-j', 'rule', 'show']))
        for rule in rules:
            if str(rule.get('table')) == table:
                w.run(['ip', '-' + str(version), 'rule', 'del', 'priority', str(rule['priority'])])
        w.run(['ip', '-' + str(version), 'route', 'flush', 'table', table], check=False, capture_output=True)
        if remove or not info.get(key) or not info.get(gateway):
            continue
        address = ipaddress.ip_interface(info[key])
        w.run(['ip', '-' + str(version), 'route', 'replace', str(address.network), 'dev', info['interface'], 'src', str(address.ip), 'table', table])
        w.run(['ip', '-' + str(version), 'route', 'replace', 'default', 'via', info[gateway], 'dev', info['interface'], 'table', table])
        w.run(['ip', '-' + str(version), 'rule', 'add', 'priority', '10000', 'from', str(address.ip), 'table', table])
        w.run(['ip', '-' + str(version), 'rule', 'add', 'priority', '10001', 'fwmark', table, 'table', table])


def ensure_address(info, wait=True):
    from . import projects as w
    from . import network as legacy
    addresses = {}
    for name, endpoint in model.resolved(info).items():
        parent = ensure_link(endpoint)
        if endpoint['ipv4mode'] == 'dhcp':
            legacy.start_dhcp(endpoint)
            import time
            deadline = time.monotonic() + (60 if wait else 0)
            while True:
                lease = legacy.lease(endpoint)
                if lease:
                    from .network_security import block_address
                    block_address(endpoint, lease['address'], 4)
                    addresses[name] = lease['address']; w.add_address(endpoint['interface'], lease['address']); break
                if not wait:
                    break
                if time.monotonic() >= deadline:
                    raise ValueError('No DHCP lease received for ' + name)
                time.sleep(1)
        elif endpoint.get('address'):
            if endpoint.get('tag'):
                existing = json.loads(w.capture(['ip', '-j', '-4', 'address', 'show', 'dev', endpoint['interface']]))
                owned = any(entry.get('local') == endpoint['address'] for link in existing for entry in link.get('addr_info', []))
                if not owned and subprocess.run(['arping', '-D', '-I', parent, '-c', '3', '-w', '4', endpoint['address']], capture_output=True, timeout=10).returncode:
                    raise ValueError('Duplicate VLAN endpoint address')
            from .network_security import block_address
            block_address(endpoint, endpoint['address'], 4)
            w.add_address(endpoint['interface'], endpoint['address']); addresses[name] = endpoint['address']
        if endpoint.get('ipv6mode') in ('auto', 'dhcp'):
            from .ipv6_dynamic import lease as ipv6_lease
            assigned = ipv6_lease(endpoint, wait)
            endpoint['cidr6'] = ''
            if assigned:
                endpoint.update(assigned)
                info.setdefault('endpoint_ipv6', {})[name] = assigned
            else:
                info.setdefault('endpoint_ipv6', {}).pop(name, None)
            entries = json.loads(w.capture(['ip', '-j', '-6', 'address', 'show', 'dev', endpoint['interface']]))
            wanted = str(ipaddress.IPv6Interface(endpoint['cidr6']).ip) if endpoint.get('cidr6') else None
            for entry in (entry for link in entries for entry in link.get('addr_info', [])):
                if entry.get('scope') == 'global' and entry['local'] != wanted:
                    w.run(['ip', '-6', 'address', 'del', entry['local'] + '/' + str(entry['prefixlen']), 'dev', endpoint['interface']])
        if endpoint.get('cidr6'):
            address = str(ipaddress.IPv6Interface(endpoint['cidr6']).ip)
            node_addresses = json.loads(w.capture(['ip', '-j', '-6', 'address', 'show']))
            if any(link['ifname'] != endpoint['interface'] and any(entry.get('local') == address for entry in link.get('addr_info', [])) for link in node_addresses):
                raise ValueError('IPv6 address already belongs to another host interface')
            if any(guest['id'] != info['id'] and any(value.get('cidr6') and str(ipaddress.IPv6Interface(value['cidr6']).ip) == address
                    for value in model.resolved(guest).values()) for guest in w.summaries()):
                raise ValueError('IPv6 address belongs to another DCT')
            existing = json.loads(w.capture(['ip', '-j', '-6', 'address', 'show', 'dev', endpoint['interface']]))
            if not any(entry.get('local') == address for link in existing for entry in link.get('addr_info', [])):
                from .network_security import acquired, block_address
                acquired(endpoint, address, 6)
                block_address(endpoint, address, 6)
                command = ['ip', '-6', 'address', 'add', address + '/128', 'dev', endpoint['interface']]
                if endpoint.get('expires'):
                    import time
                    command += ['valid_lft', str(max(1, int(endpoint['expires'] - time.time()))),
                                'preferred_lft', str(max(0, int(endpoint.get('preferred', endpoint['expires']) - time.time())))]
                w.run(command)
            elif endpoint.get('expires'):
                import time
                w.run(['ip', '-6', 'address', 'change', address + '/128', 'dev', endpoint['interface'],
                       'valid_lft', str(max(1, int(endpoint['expires'] - time.time()))),
                       'preferred_lft', str(max(0, int(endpoint.get('preferred', endpoint['expires']) - time.time())))])
            # DAD remains enabled. Never expose a duplicate IPv6 endpoint.
            if wait:
                import time
                for _ in range(30):
                    entries = json.loads(w.capture(['ip', '-j', '-6', 'address', 'show', 'dev', endpoint['interface']]))
                    assigned = next((e for l in entries for e in l.get('addr_info', []) if e.get('local') == address), {})
                    if assigned.get('dadfailed'):
                        raise ValueError('Duplicate IPv6 address')
                    if not assigned.get('tentative', False):
                        break
                    time.sleep(.1)
                else:
                    raise ValueError('IPv6 duplicate-address detection did not complete')
        routes = endpoint
        if endpoint.get('cidr6') and not wait:
            entries = json.loads(w.capture(['ip', '-j', '-6', 'address', 'show', 'dev', endpoint['interface']]))
            assigned = next((e for link in entries for e in link.get('addr_info', []) if e.get('local') == address), {})
            if assigned.get('dadfailed'):
                raise ValueError('Duplicate IPv6 address')
            if not assigned or assigned.get('tentative'):
                routes = dict(endpoint, cidr6='', gw6='')
        routing(routes)
    info['endpoint_addresses'] = addresses
    info['address'] = addresses.get('net0', info.get('address', ''))
    return info['address']


def remove(info):
    from . import projects as w
    for endpoint in model.resolved(info).values():
        routing(endpoint, remove=True)
        if endpoint.get('ipv6mode') == 'dhcp':
            w.run(['systemctl', 'stop', 'betterdocker-dhcp6@' + info['id'] + '-' + endpoint['net'] + '.service'])
        if endpoint['ipv4mode'] == 'dhcp':
            w.run(['systemctl', 'stop', 'betterdocker-dhcp@' + info['id'] + '-' + endpoint['net'] + '.service'])
            if endpoint['net'] == 'net0':
                w.run(['systemctl', 'stop', 'betterdocker-dhcp@' + info['id'] + '.service'])
        if endpoint.get('ipv6mode', 'none') != 'none':
            from .ipv6_controls import ensure
            ensure(endpoint, remove=True)
        links = json.loads(w.capture(['ip', '-j', '-d', 'link', 'show']))
        aliases = (endpoint['owner'], 'betterdocker:' + info['id'])
        # Delete the child before its VLAN parent, which would remove it implicitly.
        for link in sorted(links, key=lambda row: row.get('linkinfo', {}).get('info_kind') == 'vlan'):
            if link['ifname'] == endpoint['interface'] and link.get('ifalias') in aliases or link.get('ifalias') == endpoint['owner'] and link.get('linkinfo', {}).get('info_kind') == 'vlan':
                w.run(['ip', 'link', 'del', 'dev', link['ifname']])


def candidate(identifier, info, values):
    from . import projects as w
    value = model.validate(values)
    preflight(dict(info, interfaces=value), identifier)
    directory = w.ROOT / identifier
    runtime = read_json(directory / 'compose.runtime.json')
    provisional = dict(info, interfaces=value)
    # Static assignments are checked now; DHCP bindings are checked after acquisition.
    if all(entry['ipv4mode'] != 'dhcp' and entry.get('ipv6mode') not in ('auto', 'dhcp') for entry in value.values()):
        model.bind_runtime(runtime, provisional)
    return value


def apply(identifier, node, info, values):
    from . import projects as w
    value = candidate(identifier, info, values)
    w.require_stopped(w.container_records(identifier))
    directory = w.ROOT / identifier
    journal = directory / 'network-transaction.json'
    files = {path.name: read_text(path) for path in directory.iterdir() if path.name in ('request.json', 'compose.runtime.json', 'dhcp-address.json', 'dhclient.conf', 'dhclient.leases') or re_dhcp_file(path.name)}
    saved = {k: v for k, v in info.items() if k not in ('deployment', 'digest')}
    w.write_json(journal, {'metadata': saved, 'files': files, 'endpoints': True})
    try:
        w.remove_network(info)
        for filename in files:
            if filename.startswith(('dhcp-address', 'dhclient')):
                (directory / filename).unlink(missing_ok=True)
        primary = value['net0']
        updated = dict(saved, interfaces=value, **{k: primary[k] for k in w.NETWORK_KEYS}, endpoint_addresses={})
        updated['address'] = str(ipaddress.IPv4Interface(primary['cidr']).ip) if primary['cidr'] else ''
        updated['interface'] = model.link_name(identifier, 'net0')
        updated['hwaddr'] = model.runtime_info(updated, 'net0', primary)['hwaddr']
        request = dict(info['deployment'], interfaces=value, **{k: updated[k] for k in (*w.NETWORK_KEYS, 'address')})
        w.write_json(w.METADATA / (identifier + '.json'), updated)
        w.write_json(directory / 'request.json', request)
        w.ensure_address(updated)
        w.write_json(w.METADATA / (identifier + '.json'), updated)
        w.rebind_runtime(directory, updated['address'])
        w.write_json(directory / 'network-reconcile.json', {'address': updated['address']})
        w.reconcile_network_runtime(updated)
        w.run([str(w.HELPERS / 'firewall.py'), 'apply'])
        journal.unlink()
    except BaseException:
        w.recover_network_transaction(identifier, node)
        w.reconcile_network_runtime(saved)
        raise
    w.refresh(node)


def re_dhcp_file(name):
    import re
    return bool(re.fullmatch(r'(dhcp-address-net\d+\.json|dhclient-net\d+(?:-v6)?\.(conf|leases)|\.ipv6-net\d+\.json)', name))


def update(identifier, node, values, digest):
    from . import projects as w
    w.authorize_config_keys(w.NETWORK_KEYS)
    with w.locked(identifier):
        w.recover_network_transaction(identifier, node)
        info = w.detail(identifier, node)
        w.assert_configuration_digest(identifier, node, digest)
        if info.get('template'):
            raise ValueError('Templates cannot change their network')
        if not w.trusted_management():
            w.delegated_access(identifier, node)
        directory = w.ROOT / identifier
        pending = w.load_pending_network(directory)
        if values.get('revert'):
            w.save_pending_network(directory, {}); return
        requested = model.interfaces(dict(info, interfaces=pending['interfaces'])) if 'interfaces' in pending else model.interfaces(info)
        if 'interfaces' in values:
            if set(values) != {'interfaces'}:
                raise ValueError('Interfaces cannot be combined with individual settings')
            requested = values['interfaces']
        else:
            name = values.get('net', 'net0')
            if values.get('delete'):
                if name == 'net0' or name not in requested:
                    raise ValueError('net0 cannot be removed')
                requested.pop(name)
            else:
                requested.setdefault(name, {}).update({k: v for k, v in values.items() if k in model.KEYS})
        requested = candidate(identifier, info, requested)
        if w.active_network_instances(identifier):
            w.save_pending_network(directory, {'interfaces': requested} if requested != model.validate(model.interfaces(info)) else {})
        else:
            apply(identifier, node, info, requested)
            w.save_pending_network(directory, {})


def sync(info, wait=False):
    from . import projects as w
    directory = w.ROOT / info['id']
    before = {name: address for name, address in info.get('endpoint_addresses', {}).items() if address}
    import copy
    previous_leases = copy.deepcopy(info.get('endpoint_ipv6', {}))
    before_ipv6 = {name: {key: value.get(key) for key in ('cidr6', 'gw6')} for name, value in info.get('endpoint_ipv6', {}).items()}
    if info.get('address'):
        before.setdefault('net0', info['address'])
    active = w.active_network_instances(info['id'])
    w.ensure_address(info, wait)
    ready = all((value['ipv4mode'] != 'dhcp' or name in info['endpoint_addresses']) and
                (value.get('ipv6mode') not in ('auto', 'dhcp') or name in info.get('endpoint_ipv6', {})) for name, value in info['interfaces'].items())
    marker = directory / 'network-resume.json'
    if not ready:
        if active:
            if not marker.exists():
                w.save_network_resume(marker, active)
            w.stop_network_instances(info['id'])
            info.update(status='network-error', network_resume=True)
            w.write_json(w.METADATA / (info['id'] + '.json'), info)
        else:
            w.write_json(w.METADATA / (info['id'] + '.json'), info)
        return False
    if marker.exists():
        saved = read_json(marker)
        if isinstance(saved, dict) and saved.get('boot_id') != w.network_boot_id():
            marker.unlink()
            info.pop('network_resume', None)
    now_ipv6 = {name: {key: value.get(key) for key in ('cidr6', 'gw6')} for name, value in info.get('endpoint_ipv6', {}).items()}
    if before != info['endpoint_addresses'] or before_ipv6 != now_ipv6 or marker.exists():
        if not marker.exists():
            w.save_network_resume(marker, active)
        resume = w.read_network_resume(marker)
        w.stop_network_instances(info['id'])
        w.write_json(w.METADATA / (info['id'] + '.json'), info)
        w.rebind_runtime(directory, info['address'])
        w.write_json(directory / 'network-reconcile.json', {'addresses': info['endpoint_addresses']})
        w.reconcile_network_runtime(info)
        w.prepare_firewall(info)
        for saved in resume:
            row = next((r for r in w.container_records(info['id']) if r['service'] == saved['service'] and r.get('number', '1') == saved['number']), None)
            if not row:
                raise ValueError('DHCP recovery instance is missing')
            w.run(['docker', 'start', row['id']], capture_output=True)
            if saved.get('paused'):
                w.run(['docker', 'pause', row['id']], capture_output=True)
        w.restore_restart_policy(info['id'])
        w.finish_network_start(info)
        info.pop('network_resume', None)
        w.write_json(w.METADATA / (info['id'] + '.json'), info)
        marker.unlink()
    elif previous_leases != info.get('endpoint_ipv6', {}):
        # Lease renewal updates lifetimes without stopping unchanged endpoints.
        w.write_json(w.METADATA / (info['id'] + '.json'), info)
    return True
