"""Published endpoints, DHCP and network recovery."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time
import runtime as node_runtime

NETWORK_KEYS = ('bridge', 'ipv4mode', 'cidr', 'hwaddr', 'mtu', 'firewall')


def bridge_addresses(bridge):
    interfaces = json.loads(worker.capture(['ip', '-j', '-4', 'address', 'show']))
    selected = next((interface for interface in interfaces if interface['ifname'] == bridge), None)
    if not selected or not Path('/sys/class/net', bridge, 'bridge').is_dir():
        raise ValueError('The selected interface is not a Linux bridge')
    networks = [ipaddress.IPv4Network(f"{entry['local']}/{entry['prefixlen']}", strict=False)
                for entry in selected.get('addr_info', []) if entry.get('prefixlen', 32) < 31]
    if not networks:
        raise ValueError('Select a bridge with a connected IPv4 subnet; project routing is inherited from the host')
    return interfaces, networks


def check_address(address, bridge, identifier=None, owned_interface=None, probe=False):
    address = ipaddress.IPv4Address(address)
    interfaces, networks = worker.bridge_addresses(bridge)
    if any(str(address) == entry.get('local') for interface in interfaces
           if interface['ifname'] != owned_interface for entry in interface.get('addr_info', [])):
        raise ValueError('This address already belongs to the host; choose a separate unused IP')
    if not any(address in network and address not in (network.network_address, network.broadcast_address) for network in networks):
        raise ValueError('The project IP must belong to a connected IPv4 subnet on this bridge')
    if any(item.get('address') == str(address) and item.get('id') != identifier for item in worker.summaries()):
        raise ValueError('This IP is already reserved by another Docker project')
    already_owned = any(interface['ifname'] == owned_interface and
                        any(entry.get('local') == str(address) for entry in interface.get('addr_info', []))
                        for interface in interfaces)
    if probe and not already_owned:
        result = subprocess.run(['arping', '-D', '-I', bridge, '-c', '3', '-w', '4', str(address)],
                                text=True, capture_output=True, timeout=10)
        if result.returncode != 0:
            raise ValueError('The IP responded to duplicate-address detection; choose an unused address')


def check_sdn_bridge(bridge):
    program = ("use PVE::Network::SDN::Vnets; use PVE::Network::SDN::Zones; "
               "my $v=PVE::Network::SDN::Vnets::get_vnet($ARGV[0],1); "
               "if ($v) { my $z=PVE::Network::SDN::Zones::get_zone($v->{zone},1); "
               "die qq(DCT endpoints do not support automatic SDN IPAM or SDN DHCP allocation\\n) "
               "if $z->{ipam} || $z->{dhcp}; }")
    worker.capture(['perl', '-e', program, bridge])


def network_preflight(data):
    worker.check_sdn_bridge(data['bridge'])
    if not worker.trusted_management():
        actor = os.environ.get('BETTERDOCKER_TASK_USER')
        if not actor:
            raise ValueError('Missing authenticated deployment owner')
        program = ("use PVE::RPCEnvironment; use PVE::GuestHelpers; "
                   "my $r=PVE::RPCEnvironment->init('cli'); $r->init_request(); $r->set_user($ARGV[0]); "
                   "PVE::GuestHelpers::check_vnet_access($r,$ARGV[0],$ARGV[1],undef,undef);")
        worker.capture(['perl', '-e', program, actor, data['bridge']])
    worker.bridge_addresses(data['bridge'])
    if data.get('ipv4mode', 'static') == 'static':
        worker.check_address(data['address'], data['bridge'], probe=True)
    if data.get('hwaddr') and any(item.get('hwaddr') == data['hwaddr'] for item in worker.summaries()):
        raise ValueError('This MAC address is already used by another project')


def ensure_link(info):
    interface = info['interface']
    links = json.loads(worker.capture(['ip', '-j', '-d', 'link', 'show']))
    existing = next((link for link in links if link['ifname'] == interface), None)
    owner = 'betterdocker:' + info['id']
    if existing:
        if (existing.get('ifalias') != owner or existing.get('linkinfo', {}).get('info_kind') != 'macvlan' or
                existing.get('link') != info['bridge']):
            raise ValueError('Project interface name is already used by another network device')
    else:
        worker.run(['ip', 'link', 'add', 'link', info['bridge'], 'name', interface, 'address', info['hwaddr'],
             'type', 'macvlan', 'mode', 'bridge'])
        worker.run(['ip', 'link', 'set', 'dev', interface, 'alias', owner])
    # This is an IPv4 endpoint. Prevent RA from installing an unintended host IPv6 route.
    ipv6 = Path('/proc/sys/net/ipv6/conf') / interface / 'disable_ipv6'
    if ipv6.exists() and ipv6.read_text().strip() != '1':
        worker.run(['sysctl', '-q', '-w', 'net.ipv6.conf.' + interface + '.disable_ipv6=1'])
    if existing and existing.get('address', '').lower() != info['hwaddr'].lower():
        raise ValueError('Project interface MAC differs from its configuration')
    if info.get('mtu'):
        worker.run(['ip', 'link', 'set', 'dev', interface, 'mtu', str(info['mtu'])])
    worker.run(['ip', 'link', 'set', 'dev', interface, 'up'])


def add_address(interface, address):
    existing = json.loads(worker.capture(['ip', '-j', '-4', 'address', 'show', 'dev', interface]))
    if any(entry.get('local') == address for item in existing for entry in item.get('addr_info', [])):
        return
    # No connected routes, default routes or DNS settings on the management interface are changed.
    worker.run(['ip', 'address', 'add', address + '/32', 'dev', interface])


def dhcp_paths(info):
    directory = worker.ROOT / worker.project_id(info['id'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / 'dhcp-address.json', Path('/run') / ('betterdocker-dhcp-' + info['id'] + '.pid')


def lease(info):
    path, _ = worker.dhcp_paths(info)
    try:
        data = json.loads(path.read_text())
        if data.get('expires', 0) > time.time() and data.get('interface') == info['interface']:
            return data
    except (OSError, ValueError):
        pass
    return None


def dhcp_command(info):
    directory = worker.ROOT / info['id']
    _, pid = worker.dhcp_paths(info)
    config = directory / 'dhclient.conf'
    config.write_text('timeout 60;\nretry 10;\n' +
                      'send host-name "' + info['name'] + '";\n' +
                      'send dhcp-client-identifier 1:' + info['hwaddr'] + ';\n' +
                      'request subnet-mask, dhcp-lease-time;\n')
    return ['dhclient', '-4', '-pf', str(pid), '-lf', str(directory / 'dhclient.leases'),
            '-cf', str(config), '-sf', '/usr/local/lib/betterdocker/dhcp-hook.py']


def start_dhcp(info):
    # A dedicated systemd unit survives pvedaemon restarts and oneshot refresh jobs.
    worker.run(['systemctl', 'start', 'betterdocker-dhcp@' + worker.project_id(info['id']) + '.service'])


def dhcp_client(identifier, release=False):
    identifier = worker.project_id(identifier)
    info = json.loads((worker.METADATA / (identifier + '.json')).read_text())
    if info['node'] != worker.current_node() or info.get('ipv4mode') != 'dhcp' or info.get('interface') != 'bd' + identifier[:12]:
        raise ValueError('Invalid DHCP project')
    command = worker.dhcp_command(info) + (['-r'] if release else ['-d']) + [info['interface']]
    if release:
        worker.run(command)
    else:
        os.execvpe(command[0], command, {'PATH': os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin'), 'HOME': '/root'})


def ensure_address(info, wait=True):
    # Existing projects from the alias-only release keep their network and address.
    if not info.get('interface'):
        worker.add_address(info['bridge'], info['address'])
        return info['address']
    worker.ensure_link(info)
    if info.get('ipv4mode', 'static') == 'static':
        worker.add_address(info['interface'], info['address'])
        return info['address']
    worker.start_dhcp(info)
    deadline = time.monotonic() + (60 if wait else 0)
    while True:
        assigned = worker.lease(info)
        if assigned:
            worker.check_address(assigned['address'], info['bridge'], info['id'], info['interface'])
            worker.add_address(info['interface'], assigned['address'])
            info['address'] = assigned['address']
            return info['address']
        if not wait:
            return None
        if time.monotonic() >= deadline:
            raise ValueError('No DHCP lease received within 60 seconds. Check the bridge, DHCP server and MAC filtering.')
        time.sleep(1)


def dhcp_event(environment):
    interface = environment.get('interface', '')
    info = next((item for item in worker.summaries() if item.get('interface') == interface and
                 item['node'] == worker.current_node() and item.get('ipv4mode') == 'dhcp'), None)
    if not info:
        raise ValueError('DHCP hook was called for an unmanaged interface')
    reason = environment.get('reason', '')
    path, _ = worker.dhcp_paths(info)
    if reason == 'TIMEOUT':
        raise ValueError('Waiting for a valid DHCP offer; an expired fallback lease is not accepted')
    if reason in ('BOUND', 'RENEW', 'REBIND', 'REBOOT'):
        address = str(ipaddress.IPv4Address(environment.get('new_ip_address', '')))
        worker.check_address(address, info['bridge'], info['id'], interface)
        previous = worker.lease(info)
        if not previous or previous['address'] != address:
            worker.check_address(address, info['bridge'], info['id'], interface, probe=True)
        worker.add_address(interface, address)
        seconds = int(environment.get('new_dhcp_lease_time', '3600'))
        worker.write_json(path, {'address': address, 'interface': interface, 'expires': time.time() + max(1, seconds)})
        if previous and previous['address'] != address:
            worker.run(['ip', 'address', 'del', previous['address'] + '/32', 'dev', interface])
    elif reason in ('EXPIRE', 'FAIL', 'STOP', 'RELEASE'):
        addresses = json.loads(worker.capture(['ip', '-j', '-4', 'address', 'show', 'dev', interface]))
        for item in addresses:
            for entry in item.get('addr_info', []):
                worker.run(['ip', 'address', 'del', f"{entry['local']}/{entry['prefixlen']}", 'dev', interface])
        path.unlink(missing_ok=True)

    # PREINIT and other advisory events do not touch routes or resolv.conf.


def rebind_runtime(directory, address):
    if (directory / 'storage.json').exists():
        worker.storage_mount(directory.name)
    if not address or address == '0.0.0.0':
        raise ValueError('A dedicated address must be acquired before deployment')
    path = directory / 'compose.runtime.json'
    config = json.loads(path.read_text())
    if (directory / 'storage.json').exists():
        worker.apply_storage(config, directory.name)
    for service in config['services'].values():
        for port in service.get('ports', []):
            port['host_ip'] = address
    worker.write_json(path, config)


def network_boot_id():
    try:
        return (worker.PROC / 'sys/kernel/random/boot_id').read_text().strip()
    except FileNotFoundError:
        return ''


def save_network_resume(path, instances):
    worker.write_json(path, {'boot_id': worker.network_boot_id(), 'instances': instances})


def read_network_resume(path):
    state = json.loads(path.read_text())
    return state['instances'] if isinstance(state, dict) else state


def active_network_instances(identifier):
    return [{'service': row['service'], 'number': row.get('number', '1'), 'paused': row['state'] == 'paused'}
            for row in worker.container_records(identifier) if row['state'] in ('running', 'restarting', 'paused')]


def clear_network_resume(identifier, service=None):
    path = worker.ROOT / identifier / 'network-resume.json'
    if not path.exists():
        return
    if service is None:
        path.unlink()
    else:
        remaining = [row for row in worker.read_network_resume(path) if row['service'] != service]
        if remaining:
            worker.save_network_resume(path, remaining)
        else:
            path.unlink()


def stop_network_instances(identifier):
    records = [row for row in worker.container_records(identifier) if row['state'] in ('running', 'restarting', 'paused')]
    for row in records:
        if row['state'] == 'paused':
            worker.run(['docker', 'unpause', row['id']], capture_output=True)
    if records:
        worker.run(['docker', 'stop', *[row['id'] for row in records]], timeout=1800)


def prepare_network_start(info):
    # A lease can change between refresh and Start; reconcile published bindings first.
    previous = info.get('address')
    worker.ensure_address(info)
    if info.get('ipv4mode') == 'dhcp':
        info['address'] = previous
        worker.sync_dhcp(info)
    worker.rebind_runtime(worker.ROOT / info['id'], info['address'])
    worker.reconcile_network_runtime(info)


def reconcile_network_runtime(info):
    directory = worker.ROOT / info['id']
    marker = directory / 'network-reconcile.json'
    if marker.exists():
        worker.require_stopped(worker.container_records(info['id']))
        worker.run(worker.compose_base(directory, info['id']) + ['up', '--no-start', '--force-recreate'], timeout=1800)
        marker.unlink()


def sync_dhcp(info):
    if info.get('ipv4mode') != 'dhcp':
        return True
    directory = worker.ROOT / info['id']
    marker = directory / 'network-resume.json'
    if marker.exists():
        state = json.loads(marker.read_text())
        if isinstance(state, dict) and state['boot_id'] != worker.network_boot_id():
            marker.unlink()
            info.pop('network_resume', None)
            info['status'] = 'stopped'
            worker.write_json(worker.METADATA / (info['id'] + '.json'), info)
    worker.ensure_link(info)
    worker.start_dhcp(info)
    assigned = worker.lease(info)
    if not assigned:
        active = worker.active_network_instances(info['id'])
        if active:
            if not marker.exists():
                worker.save_network_resume(marker, active)
            worker.stop_network_instances(info['id'])
            info['network_resume'] = True
            info['status'] = 'network-error'
            worker.write_json(worker.METADATA / (info['id'] + '.json'), info)
        return False
    changed = info.get('address') != assigned['address']
    if changed or marker.exists() or info.get('network_resume'):
        # Persist the exact active replicas before a rebind. Never restart stopped siblings.
        if not marker.exists():
            worker.save_network_resume(marker, worker.active_network_instances(info['id']))
        resume = worker.read_network_resume(marker)
        if (directory / 'snapshot-restored.json').exists() and changed:
            raise ValueError('DCT address changed after rollback; restore a snapshot again to rebind preserved filesystems')
        worker.rebind_runtime(directory, assigned['address'])
        worker.write_json(directory / 'network-reconcile.json', {'address': assigned['address']})
        worker.stop_network_instances(info['id'])
        worker.reconcile_network_runtime(info)
        if info.get('firewall'):
            info['address'] = assigned['address']
            worker.write_json(worker.METADATA / (info['id'] + '.json'), info)
            worker.prepare_firewall(info)
        records = worker.container_records(info['id'])
        for saved in resume:
            row = next((row for row in records if row['service'] == saved['service'] and
                        row.get('number', '1') == saved['number']), None)
            if row is None:
                raise ValueError('DHCP recovery could not find a previously active instance')
            worker.run(['docker', 'start', row['id']], capture_output=True)
            if saved.get('paused'):
                worker.run(['docker', 'pause', row['id']], capture_output=True)
        worker.restore_restart_policy(info['id'])
        info['address'] = assigned['address']
        info.pop('network_resume', None)
        info['status'] = 'creating' if resume else 'stopped'
        worker.write_json(worker.METADATA / (info['id'] + '.json'), info)
        marker.unlink()
    return True


def firewall_check(identifier, node):
    info = worker.detail(identifier, node)
    worker.delegated_access(identifier, node)
    import firewall
    firewall.networks(info)
    return info


def prepare_firewall(info):
    rootless = node_runtime.load().rootless
    if not info.get('firewall') and not rootless:
        return
    if info.get('firewall'):
        worker.firewall_check(info['id'], info['node'])
    directory = worker.ROOT / info['id']
    if not (directory / 'snapshot-restored.json').exists():
        worker.run(worker.compose_base(directory, info['id']) + ['up', '--no-start', '--no-recreate'], timeout=1800)
    worker.run([str(worker.HELPERS / 'firewall.py'), 'apply'])


def finish_network_start(info):
    if not info.get('firewall') and not node_runtime.load().rootless:
        return
    try:
        worker.run([str(worker.HELPERS / 'firewall.py'), 'apply'])
    except Exception:
        # Never leave a newly started guest with incomplete requested filtering.
        worker.run(worker.compose_base(worker.ROOT / info['id'], info['id']) + ['stop'], timeout=1800)
        raise


def load_pending_network(directory):
    path = directory / 'pending-network.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_pending_network(directory, values):
    if values:
        worker.write_json(directory / 'pending-network.json', values)
    else:
        (directory / 'pending-network.json').unlink(missing_ok=True)


def network_config(identifier, node):
    info = worker.detail(identifier, node)
    return dict({key: info.get(key, '') for key in NETWORK_KEYS},
                ipv4mode=info.get('ipv4mode', 'static'), mtu=info.get('mtu', 0), firewall=int(info.get('firewall', 0)),
                cidr=(info.get('cidr') or info.get('address', '')) if info.get('ipv4mode', 'static') == 'static' else '', digest=info['digest'], pending=worker.load_pending_network(worker.ROOT / identifier))


def recover_network_transaction(identifier, node):
    directory = worker.ROOT / identifier
    journal = directory / 'network-transaction.json'
    if not journal.exists():
        return False
    if node != worker.current_node():
        raise ValueError('Project belongs to another node')
    saved = json.loads(journal.read_text())
    current = json.loads((worker.METADATA / (identifier + '.json')).read_text())
    if saved['metadata']['id'] != identifier or saved['metadata']['node'] != node:
        raise ValueError('Invalid network recovery journal')
    if current['node'] != node or current['id'] != identifier:
        raise ValueError('Project belongs to another node')
    # Address restoration runs before Docker at boot. An inactive daemon has no running containers.
    daemon = node_runtime.service('is-active', runner=worker.run)
    if daemon.returncode == 0:
        worker.require_stopped(worker.container_records(identifier))
    worker.remove_network(current)
    for name, content in saved['files'].items():
        if name not in ('request.json', 'compose.runtime.json', 'dhcp-address.json', 'dhclient.leases', 'dhclient.conf'):
            raise ValueError('Invalid network recovery file')
        path = directory / name
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content)
            os.chmod(path, 0o600)
    info = saved['metadata']
    worker.write_json(worker.METADATA / (identifier + '.json'), info)
    worker.ensure_address(info, wait=False)
    worker.write_json(directory / 'network-reconcile.json', {'recovery': True})
    journal.unlink()
    return True


def network_candidate(identifier, node, info, values):
    directory = worker.ROOT / identifier
    before = {key: info.get(key, 'static' if key == 'ipv4mode' else 0 if key in ('mtu', 'firewall') else '') for key in NETWORK_KEYS}
    requested = dict(before, **values)
    data = worker.validate(dict(info['deployment'], **requested, address=requested['cidr']))
    requested = {key: data[key] for key in NETWORK_KEYS}
    if all(requested[key] == before[key] for key in NETWORK_KEYS if key != 'firewall'):
        return data, before
    if (directory / 'snapshot-restored.json').exists():
        raise ValueError('Redeploy or restore a snapshot before changing a restored DCT network')
    worker.check_sdn_bridge(data['bridge'])
    if any(requested[key] != before[key] for key in NETWORK_KEYS if key not in ('mtu', 'firewall')):
        for manifest in worker.snapshot_root(identifier).glob('*/manifest.json'):
            captured = manifest.parent / 'configuration' / 'metadata.json'
            if not captured.exists() or not all(key in json.loads(captured.read_text()) for key in NETWORK_KEYS if key != 'firewall'):
                raise ValueError('Remove snapshots without a restorable network configuration before changing the endpoint')
    _, subnets = worker.bridge_addresses(data['bridge'])
    if data['ipv4mode'] == 'static':
        address = ipaddress.IPv4Interface(data['cidr'])
        if not any(address.ip in subnet and address.network.prefixlen == subnet.prefixlen for subnet in subnets):
            raise ValueError('IPv4/CIDR must match a connected subnet on the bridge; routing is inherited from the host')
        worker.check_address(data['address'], data['bridge'], identifier, info.get('interface'), probe=True)
    if any(row.get('id') != identifier and row.get('hwaddr') == data['hwaddr'] for row in worker.summaries()):
        raise ValueError('This MAC address is already used by another project')
    if not data['hwaddr']:
        data['hwaddr'] = info['hwaddr']
    bridge_links = json.loads(worker.capture(['ip', '-j', 'link', 'show', 'dev', data['bridge']]))
    if data['mtu'] and data['mtu'] > bridge_links[0]['mtu']:
        raise ValueError('MTU cannot exceed the bridge MTU')
    return data, before


def apply_network(identifier, node, info, values):
    # Caller owns the global lock; the recovery journal protects each mutation.
    directory = worker.ROOT / identifier
    worker.require_stopped(worker.container_records(identifier))
    if (directory / 'network-resume.json').exists():
        raise ValueError('Stop the DCT explicitly before changing a recovering DHCP network')
    data, before = worker.network_candidate(identifier, node, info, values)
    requested = {key: data[key] for key in NETWORK_KEYS}
    if requested == before:
        return
    if all(requested[key] == before[key] for key in NETWORK_KEYS if key != 'firewall'):
        metadata = worker.METADATA / (identifier + '.json')
        request = directory / 'request.json'
        saved_metadata, saved_request = metadata.read_text(), request.read_text()
        try:
            candidate = json.loads(saved_metadata)
            candidate['firewall'] = requested['firewall']
            deployment = json.loads(saved_request)
            deployment['firewall'] = requested['firewall']
            if candidate['firewall']:
                worker.firewall_check(identifier, node)
            worker.write_json(metadata, candidate)
            worker.write_json(request, deployment)
            worker.run([str(worker.HELPERS / 'firewall.py'), 'apply'])
        except Exception:
            worker.write_json(metadata, json.loads(saved_metadata))
            worker.write_json(request, json.loads(saved_request))
            worker.run([str(worker.HELPERS / 'firewall.py'), 'apply'])
            raise
        worker.refresh(node)
        return
    # The private journal is written before releasing the old link or DHCP client.
    saved = {'metadata': {key: value for key, value in info.items() if key not in ('deployment', 'digest')},
             'files': {name: (directory / name).read_text() if (directory / name).exists() else None
                       for name in ('request.json', 'compose.runtime.json', 'dhcp-address.json', 'dhclient.leases', 'dhclient.conf')}}
    journal = directory / 'network-transaction.json'
    worker.write_json(journal, saved)
    try:
        worker.remove_network(info)
        (directory / 'dhcp-address.json').unlink(missing_ok=True)
        (directory / 'dhclient.leases').unlink(missing_ok=True)
        candidate = dict(saved['metadata'], **{key: data[key] for key in (*NETWORK_KEYS, 'address')},
                         interface='bd' + identifier[:12], status='stopped')
        candidate.pop('network_resume', None)
        worker.write_json(worker.METADATA / (identifier + '.json'), candidate)
        # Change only network fields. Keep the original Compose, .env and resource overrides.
        deployment = dict(info['deployment'], **{key: data[key] for key in (*NETWORK_KEYS, 'address')})
        worker.write_json(directory / 'request.json', deployment)
        worker.ensure_address(candidate)
        worker.rebind_runtime(directory, candidate['address'])
        worker.write_json(directory / 'network-reconcile.json', {'address': candidate['address']})
        worker.reconcile_network_runtime(candidate)
        worker.write_json(worker.METADATA / (identifier + '.json'), candidate)
        if candidate.get('firewall'):
            worker.firewall_check(identifier, node)
        worker.run([str(worker.HELPERS / 'firewall.py'), 'apply'])
        worker.clear_network_resume(identifier)
        journal.unlink()
    except Exception as error:
        try:
            worker.recover_network_transaction(identifier, node)
            worker.reconcile_network_runtime(saved['metadata'])
        except Exception as recovery:
            raise ValueError(f'Network update failed: {error}; recovery failed: {recovery}') from error
        raise
    worker.refresh(node)


def project_network(identifier, node, values, digest=None):
    if not isinstance(values, dict) or set(values) - set((*NETWORK_KEYS, 'revert')):
        raise ValueError('Unsupported network setting')
    if 'revert' in values and (set(values) != {'revert'} or values['revert'] != 1):
        raise ValueError('Revert cannot be combined with network settings')
    worker.authorize_config_keys(NETWORK_KEYS)
    with worker.locked(identifier):
        worker.recover_network_transaction(identifier, node)
        info = worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        directory = worker.ROOT / identifier
        if info.get('template'):
            raise ValueError('A DCT template cannot change its network')
        if not worker.trusted_management():
            worker.delegated_access(identifier, node)
        if values.get('revert'):
            worker.save_pending_network(directory, {})
            return
        requested = dict(worker.load_pending_network(directory), **values)
        data, before = worker.network_candidate(identifier, node, info, requested)
        if any(row['state'] in ('running', 'restarting', 'paused') for row in worker.container_records(identifier)):
            worker.save_pending_network(directory, {key: data[key] for key in NETWORK_KEYS if data[key] != before[key]})
            return
        worker.apply_network(identifier, node, info, requested)
        worker.save_pending_network(directory, {})


def apply_pending_network(identifier, node):
    directory = worker.ROOT / identifier
    pending = worker.load_pending_network(directory)
    if pending:
        worker.apply_network(identifier, node, worker.detail(identifier, node), pending)
        worker.save_pending_network(directory, {})


def remove_network(info):
    if not info.get('interface'):
        worker.run(['ip', 'address', 'del', info['address'] + '/32', 'dev', info['bridge']])
        return
    if info.get('ipv4mode') == 'dhcp':
        worker.run(['systemctl', 'stop', 'betterdocker-dhcp@' + info['id'] + '.service'])
    links = json.loads(worker.capture(['ip', '-j', '-d', 'link', 'show']))
    if any(link['ifname'] == info['interface'] and link.get('ifalias') == 'betterdocker:' + info['id'] for link in links):
        worker.run(['ip', 'link', 'del', 'dev', info['interface']])


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
