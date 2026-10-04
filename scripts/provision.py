#!/usr/bin/env python3
"""Native host Docker projects. Public cluster metadata never contains deployment secrets."""
import argparse
import contextlib
from datetime import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path('/var/lib/betterdocker/projects')
METADATA = Path('/etc/pve/betterdocker')
LOCK = Path('/run/lock/betterdocker-projects.lock')
PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup')


def run(args, **kwargs):
    kwargs.setdefault('timeout', 60)
    kwargs.setdefault('env', {'PATH': os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin'), 'HOME': '/root'})
    return subprocess.run(args, check=True, text=True, **kwargs)


def capture(args):
    return run(args, capture_output=True).stdout


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2) + '\n'
    if path.exists() and path.read_text() == content:
        return
    # pmxcfs does not implement chmod: only public summaries are stored there.
    fd, temporary = tempfile.mkstemp(prefix='.betterdocker-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def project_id(value):
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid Docker project ID')
    return value


def summaries():
    results = []
    if METADATA.exists():
        for path in METADATA.glob('*.json'):
            try:
                results.append(json.loads(path.read_text()))
            except (ValueError, OSError):
                continue
    return results


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Deployment must be an object')
    allowed = {'name', 'mode', 'compose', 'image', 'ports', 'environment', 'bridge', 'address', 'ipv4mode', 'cidr', 'hwaddr', 'mtu'}
    if set(data) - allowed:
        raise ValueError('Unknown deployment setting')
    name = data.get('name', '')
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', name):
        raise ValueError('Name must contain 1–63 letters, numbers, dots, underscores or hyphens')
    mode = data.get('mode', 'compose')
    if mode not in ('compose', 'image'):
        raise ValueError('Choose Docker Compose or Docker image')
    if mode == 'compose':
        yaml = data.get('compose', '')
        if not isinstance(yaml, str) or not yaml.strip() or len(yaml.encode()) > 240000:
            raise ValueError('Compose YAML is empty or too large')
    else:
        image = data.get('image', '')
        if not isinstance(image, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}', image):
            raise ValueError('Invalid Docker image')
        parse_ports(data.get('ports', ''))
    environment = data.get('environment', '')
    if not isinstance(environment, str) or len(environment.encode()) > 16000:
        raise ValueError('.env contents must be text (maximum 16 KB)')
    bridge = data.get('bridge', '')
    if not isinstance(bridge, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge):
        raise ValueError('Select a Linux bridge')
    ipv4mode = data.get('ipv4mode', 'static')
    if ipv4mode not in ('static', 'dhcp'):
        raise ValueError('Choose static IPv4 or DHCP')
    address, cidr = '', ''
    if ipv4mode == 'static':
        interface = ipaddress.IPv4Interface(data.get('cidr') or data.get('address', ''))
        address, cidr = str(interface.ip), str(interface)
        if interface.ip.is_multicast or interface.ip.is_unspecified or interface.ip.is_loopback or interface.ip.is_link_local:
            raise ValueError('Choose a usable LAN IPv4 address')
    if not isinstance(data.get('hwaddr', ''), str):
        raise ValueError('MAC address must be text')
    hwaddr = data.get('hwaddr', '').strip().lower()
    if hwaddr and (not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', hwaddr) or
                   int(hwaddr[:2], 16) & 1 or hwaddr == '00:00:00:00:00:00'):
        raise ValueError('MAC address must be a valid unicast Ethernet address')
    raw_mtu = data.get('mtu')
    if isinstance(raw_mtu, bool):
        raise ValueError('Invalid MTU')
    mtu = int(raw_mtu) if raw_mtu not in (None, '') else 0
    if mtu and not 576 <= mtu <= 9216:
        raise ValueError('MTU must be between 576 and 9216')
    return dict(data, mode=mode, environment=environment, address=address, cidr=cidr,
                ipv4mode=ipv4mode, hwaddr=hwaddr, mtu=mtu)


def parse_ports(text):
    if not isinstance(text, str):
        raise ValueError('Ports must be text: one published:container[/tcp|udp] mapping per line')
    ports = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'\s*(\d+):(\d+)(?:/(tcp|udp))?\s*', line)
        if not match:
            raise ValueError('Invalid port mapping; use 8080:80 or 5353:53/udp')
        published, target = map(int, match.group(1, 2))
        if not 1 <= published <= 65535 or not 1 <= target <= 65535:
            raise ValueError('Port numbers must be between 1 and 65535')
        ports.append({'published': str(published), 'target': target, 'protocol': match[3] or 'tcp'})
    return ports


def image_compose(data):
    return json.dumps({'services': {'app': {
        'image': data['image'], 'restart': 'unless-stopped',
        'ports': parse_ports(data.get('ports', '')), 'env_file': ['.env'],
    }}})


def bind_ports(config, address, identifier):
    """Normalize Compose's actual service ports; internal networks/volumes stay as supplied."""
    services = config.get('services')
    if not isinstance(services, dict) or not services:
        raise ValueError('Compose must define at least one service')
    published = set()
    for name, service in services.items():
        if service.get('network_mode'):
            raise ValueError(f'{name}: network_mode is incompatible with a dedicated project IP')
        if service.get('build'):
            raise ValueError(f'{name}: build contexts are not uploaded; use an image')
        if service.get('profiles'):
            raise ValueError(f'{name}: profiles are not supported; supply the services to deploy')
        for port in service.get('ports', []):
            if not isinstance(port, dict):
                raise ValueError('Compose returned an unsupported port format')
            port['host_ip'] = address
            if not port.get('published'):
                raise ValueError(f'{name}: specify a published port (for example 8080:80)')
            protocol = port.get('protocol', 'tcp')
            # Docker validates individual values and ranges when parsing Compose.
            binding = (str(port['published']), protocol)
            if binding in published:
                raise ValueError('Two services publish the same port on the project IP')
            published.add(binding)
        labels = service.get('labels') or {}
        labels['betterdocker.id'] = identifier
        service['labels'] = labels
    return config


def compose_base(directory, identifier, filename='compose.runtime.json'):
    return ['docker', 'compose', '--project-name', 'bd-' + identifier,
            '--project-directory', str(directory), '--env-file', str(directory / '.env'),
            '-f', str(directory / filename)]


def prepare(data, identifier, directory, project_directory=None):
    project_directory = project_directory or directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    source = data['compose'] if data['mode'] == 'compose' else image_compose(data)
    (directory / 'compose.source.yaml').write_text(source)
    (directory / '.env').write_text(data.get('environment', ''))
    for name in ('compose.source.yaml', '.env'):
        os.chmod(directory / name, 0o600)
    command = compose_base(directory, identifier, 'compose.source.yaml') + ['config', '--no-env-resolution', '--format', 'json']
    # Use the permanent project directory even when validating an edit in staging.
    command[command.index('--project-directory') + 1] = str(project_directory)
    # Canonical config may contain passwords; never send it to the task log.
    try:
        config = json.loads(capture(command))
    except subprocess.CalledProcessError:
        raise ValueError('Invalid Compose configuration. Check YAML, .env and referenced files.') from None
    config = bind_ports(config, data['address'], identifier)
    apply_settings(config, load_settings(project_directory))
    # config's result is already interpolated. Escape literal dollars before a second Compose read.
    def escape(value):
        if isinstance(value, str):
            return value.replace('$', '$$')
        if isinstance(value, list):
            return [escape(v) for v in value]
        if isinstance(value, dict):
            return {k: escape(v) for k, v in value.items()}
        return value
    write_json(directory / 'compose.runtime.json', escape(config))
    os.chmod(directory / 'compose.runtime.json', 0o600)
    return config


def load_settings(directory):
    path = directory / 'settings.json'
    return json.loads(path.read_text()) if path.exists() else {}


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - {'cpus', 'memory', 'swap', 'cpuunits', 'pids', 'dns', 'dns_search', 'restart'}:
        raise ValueError('Invalid service settings')
    result = {}
    for key, maximum in [('cpus', 8192), ('memory', 1048576), ('swap', 1048576), ('cpuunits', 262144), ('pids', 4194304)]:
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not (-1 if key == 'swap' else 0) <= raw <= maximum or (key != 'cpus' and int(raw) != raw):
            raise ValueError(f'Invalid {key} limit')
        result[key] = raw
    if result['cpuunits'] and result['cpuunits'] < 2:
        raise ValueError('CPU units must be at least 2')
    if result['swap'] and not result['memory']:
        raise ValueError('Set a memory limit before setting swap')
    dns = value.get('dns', [])
    if not isinstance(dns, list) or len(dns) > 8:
        raise ValueError('Invalid DNS servers')
    result['dns'] = [str(ipaddress.ip_address(server)) for server in dns]
    search = value.get('dns_search', [])
    if not isinstance(search, list) or len(search) > 8 or any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,253}', v) for v in search):
        raise ValueError('Invalid DNS search domains')
    result['dns_search'] = search
    restart = value.get('restart', 'unless-stopped')
    if not isinstance(restart, str) or not re.fullmatch(r'(?:no|always|unless-stopped|on-failure(?::[1-9][0-9]*)?)', restart):
        raise ValueError('Invalid restart policy')
    result['restart'] = restart
    return result


def apply_settings(config, settings):
    for name, values in settings.items():
        if name not in config['services']:
            continue
        service = config['services'][name]
        values = validate_settings(values)
        for key in ('cpus', 'cpu_shares', 'cpu_period', 'cpu_quota', 'mem_limit', 'memswap_limit', 'pids_limit'):
            service.pop(key, None)
        resources = service.get('deploy', {}).get('resources', {})
        for group in ('limits', 'reservations'):
            for key in ('cpus', 'memory', 'pids'):
                resources.get(group, {}).pop(key, None)
            if group in resources and not resources[group]:
                resources.pop(group)
        if 'resources' in service.get('deploy', {}) and not resources:
            service['deploy'].pop('resources')
        if values['cpus']:
            service['cpus'] = values['cpus']
        if values['cpuunits']:
            service['cpu_shares'] = values['cpuunits']
        if values['memory']:
            service['mem_limit'] = int(values['memory']) * 1048576
            service['memswap_limit'] = -1 if values['swap'] == -1 else int(values['memory'] + values['swap']) * 1048576
            # A source reservation cannot exceed a new hard limit.
            service.pop('mem_reservation', None)
        if values['pids']:
            service['pids_limit'] = values['pids']
        service['dns'] = values['dns']
        service['dns_search'] = values['dns_search']
        service['restart'] = values['restart']
    return config


def container_records(identifier):
    ids = capture(['docker', 'ps', '-aq', '--filter', 'label=betterdocker.id=' + identifier]).split()
    if not ids:
        return []
    inspected = json.loads(capture(['docker', 'inspect', *ids]))
    records = []
    for item in inspected:
        labels = item['Config'].get('Labels') or {}
        if labels.get('betterdocker.id') != identifier:
            continue
        records.append({'id': item['Id'], 'name': item['Name'].lstrip('/'),
                        'service': labels.get('com.docker.compose.service', ''),
                        'state': item['State']['Status'], 'started': item['State'].get('StartedAt', ''),
                        'image': item['Config']['Image'], 'pid': item['State'].get('Pid', 0)})
    return records


def byte_size(value):
    match = re.fullmatch(r'\s*([0-9.]+)\s*([kmgtep]?i?b)?\s*', value, re.I)
    if not match:
        raise ValueError('Invalid Docker size measurement')
    unit = (match[2] or 'b').lower()
    power = 'bkmgtpe'.index(unit[0]) if unit[0] != 'b' else 0
    return float(match[1]) * (1024 if 'i' in unit else 1000) ** power


def metric_snapshot(containers, stats, services):
    active = [item for item in containers if item['state'] == 'running']
    pairs = lambda field: [tuple(byte_size(part) for part in row[field].split('/')) for row in stats]
    memory, network, disk = pairs('MemUsage'), pairs('NetIO'), pairs('BlockIO')
    cores = sum(float(row['CPUPerc'].rstrip('%')) / 100 for row in stats)
    cpus = min(os.cpu_count() or 1, sum((item['settings']['cpus'] or (os.cpu_count() or 1)) * item.get('replicas', 1) for item in services)) or 1
    host_memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    assigned_memory = sum(item['settings']['memory'] * 1048576 * item.get('replicas', 1) for item in services)
    maxmem = min(host_memory, assigned_memory) if services and all(item['settings']['memory'] for item in services) else host_memory
    started = []
    for item in active:
        try:
            # Docker timestamps have nanosecond precision; datetime accepts it.
            started.append(datetime.fromisoformat(item['started'].replace('Z', '+00:00')).timestamp())
        except (ValueError, KeyError):
            pass
    return {'time': int(time.time()), 'cpu': cores / cpus, 'cpus': cpus,
            'mem': sum(pair[0] for pair in memory), 'maxmem': maxmem,
            'netin_total': sum(pair[0] for pair in network), 'netout_total': sum(pair[1] for pair in network),
            'diskread_total': sum(pair[0] for pair in disk), 'diskwrite_total': sum(pair[1] for pair in disk),
            'uptime': max(0, int(time.time() - min(started))) if started else 0,
            'instances': ','.join(sorted(item['id'] for item in active))}



def swap_usage(containers, services):
    try:
        match = re.search(r'^SwapTotal:\s+(\d+)\s+kB$', (PROC / 'meminfo').read_text(), re.M)
        if not match:
            return {'swap': None, 'maxswap': None}
        host_swap = int(match[1]) * 1024
        active = [item for item in containers if item['state'] == 'running']
        used, capacity, seen = 0, 0, set()
        for item in active:
            pid = item.get('pid', 0)
            if not isinstance(pid, int) or pid <= 0:
                return {'swap': None, 'maxswap': None}
            groups = (PROC / str(pid) / 'cgroup').read_text().splitlines()
            relative = next((line[3:] for line in groups if line.startswith('0::')), None)
            # A recycled PID must never attribute another workload's swap to this project.
            if not relative or item['id'] not in relative:
                return {'swap': None, 'maxswap': None}
            path = (CGROUP / relative.lstrip('/')).resolve()
            if not path.is_relative_to(CGROUP.resolve()):
                return {'swap': None, 'maxswap': None}
            if path in seen:
                continue
            seen.add(path)
            used += int((path / 'memory.swap.current').read_text())
            limit = (path / 'memory.swap.max').read_text().strip()
            capacity += host_swap if limit == 'max' else int(limit)
        if not active:
            unlimited = any(not item['settings']['memory'] or item['settings']['swap'] == -1 for item in services)
            capacity = host_swap if unlimited else sum(item['settings']['swap'] * 1048576 * item.get('replicas', 1) for item in services)
        return {'swap': used, 'maxswap': min(capacity, host_swap)}
    except (OSError, ValueError):
        return {'swap': None, 'maxswap': None}


def disk_usage(identifier, containers):
    path = ROOT / identifier / 'disk-usage.json'
    ids = sorted(item['id'] for item in containers)
    try:
        cached = json.loads(path.read_text()) if path.exists() else {}
        if cached.get('ids') == ids and time.time() - cached.get('time', 0) < 60:
            return cached['usage']
    except (OSError, ValueError, KeyError):
        pass
    try:
        data_root = capture(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip()
        if not data_root or not Path(data_root).is_absolute():
            raise ValueError('Invalid Docker storage directory')
        filesystem = os.statvfs(data_root)
        inspected = json.loads(capture(['docker', 'inspect', '--type', 'container', '--size', *ids])) if ids else []
        if {item['Id'] for item in inspected} != set(ids) or any(not isinstance(item.get('SizeRootFs'), int) or item['SizeRootFs'] < 0 for item in inspected):
            raise ValueError('Docker root filesystem measurement unavailable')
        # Logical root filesystem sizes. Capacity is shared Docker storage, not a project quota.
        usage = {'disk': sum(item['SizeRootFs'] for item in inspected),
                 'maxdisk': filesystem.f_blocks * filesystem.f_frsize}
        write_json(path, {'ids': ids, 'time': int(time.time()), 'usage': usage})
        os.chmod(path, 0o600)
        return usage
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'disk': None, 'maxdisk': None}


def record_metrics(info):
    directory = ROOT / info['id']
    path = directory / 'metrics.json'
    history = json.loads(path.read_text()) if path.exists() else []
    snapshot = info['metrics']
    previous = history[-1] if history else None
    if previous and snapshot['time'] - previous['time'] < 60:
        return
    elapsed = snapshot['time'] - previous['time'] if previous else 0
    for key in ('netin', 'netout', 'diskread', 'diskwrite'):
        snapshot[key] = max(0, (snapshot[key + '_total'] - previous[key + '_total']) / elapsed) if (
            previous and elapsed > 0 and snapshot['instances'] == previous['instances']) else 0
    history = [row for row in history if snapshot['time'] - row['time'] <= 7 * 86400]
    write_json(path, history + [snapshot])
    os.chmod(path, 0o600)


def rrddata(identifier, node, timeframe, cf):
    detail(identifier, node)
    seconds = {'hour': 3600, 'day': 86400, 'week': 604800, 'month': 2592000, 'year': 31536000}
    if timeframe not in seconds or cf not in ('AVERAGE', 'MAX'):
        raise ValueError('Invalid chart range or consolidation function')
    path = ROOT / identifier / 'metrics.json'
    history = json.loads(path.read_text()) if path.exists() else []
    cutoff = time.time() - seconds[timeframe]
    bucket_size = max(60, seconds[timeframe] // 600)
    buckets = {}
    for row in history:
        if row['time'] >= cutoff:
            buckets.setdefault(row['time'] // bucket_size * bucket_size, []).append(row)
    fields = ('cpu', 'mem', 'maxmem', 'netin', 'netout', 'diskread', 'diskwrite')
    return [dict(time=stamp, **{key: (max(row[key] for row in rows) if cf == 'MAX' else
                                     sum(row[key] for row in rows) / len(rows)) for key in fields})
            for stamp, rows in sorted(buckets.items())]


def notes(identifier, node, description=None):
    detail(identifier, node)
    path = ROOT / identifier / 'notes.txt'
    if description is not None:
        if len(description) > 8192:
            raise ValueError('Notes cannot exceed 8192 characters')
        path.write_text(description)
        os.chmod(path, 0o600)
    return {'description': path.read_text() if path.exists() else ''}


def management(identifier, node, shared_stats=None):
    info = detail(identifier, node)
    directory = ROOT / identifier
    config = json.loads((directory / 'compose.runtime.json').read_text())
    settings = load_settings(directory)
    info['services'] = []
    for name, service in config['services'].items():
        limits = service.get('deploy', {}).get('resources', {}).get('limits', {})
        memory = int(service.get('mem_limit') or limits.get('memory') or 0) // 1048576
        total = int(service.get('memswap_limit') or 0) // 1048576
        values = {'cpus': float(service.get('cpus') or limits.get('cpus') or 0), 'memory': memory,
                  'swap': -1 if service.get('memswap_limit') == -1 else max(0, total - memory) if total else memory, 'cpuunits': service.get('cpu_shares', 0),
                  'pids': max(0, int(service.get('pids_limit') or limits.get('pids') or 0)),
                  'dns': service.get('dns', []), 'dns_search': service.get('dns_search', []),
                  'restart': service.get('restart', 'no')}
        info['services'].append({'name': name, 'image': service.get('image', ''),
                                 'settings': settings.get(name, values), 'replicas': service.get('deploy', {}).get('replicas', 1),
                                 'mounts': service.get('volumes', []), 'ports': service.get('ports', [])})
    info['containers'] = container_records(identifier)
    active = [item['id'] for item in info['containers'] if item['state'] == 'running']
    info['stats'] = []
    if shared_stats is not None:
        names = {item['name'] for item in info['containers']}
        info['stats'] = [row for row in shared_stats if row['Name'] in names]
    elif active:
        info['stats'] = [json.loads(line) for line in capture(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *active]).splitlines()]
    info['metrics'] = metric_snapshot(info['containers'], info['stats'], info['services'])
    info['metrics'].update(swap_usage(info['containers'], info['services']))
    info['metrics'].update(disk_usage(identifier, info['containers']))
    return info


def service_settings(identifier, node, service, values):
    values = validate_settings(values)
    with locked():
        info = detail(identifier, node)
        directory = ROOT / identifier
        runtime = directory / 'compose.runtime.json'
        original = runtime.read_text()
        config = json.loads(original)
        if service not in config['services']:
            raise ValueError('Unknown Compose service')
        settings = load_settings(directory)
        settings[service] = values
        active = any(item['service'] == service and item['state'] == 'running' for item in container_records(identifier))
        apply_settings(config, {service: values})
        write_json(runtime, config)
        os.chmod(runtime, 0o600)
        try:
            run(compose_base(directory, identifier) + ['up', '-d' if active else '--no-start', '--no-deps', service], timeout=1800)
        except Exception:
            runtime.write_text(original)
            raise
        write_json(directory / 'settings.json', settings)
        os.chmod(directory / 'settings.json', 0o600)
        refresh(node)


def console_target(identifier, node, container, shell):
    detail(identifier, node)
    if shell not in ('/bin/sh', '/bin/bash') or not re.fullmatch(r'[0-9a-f]{64}', container):
        raise ValueError('Invalid console target')
    if not any(item['id'] == container and item['state'] == 'running' for item in container_records(identifier)):
        raise ValueError('Choose a running container belonging to this project')
    return ['docker', 'exec', '-it', container, shell]


def bridge_addresses(bridge):
    interfaces = json.loads(capture(['ip', '-j', '-4', 'address', 'show']))
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
    interfaces, networks = bridge_addresses(bridge)
    if any(str(address) == entry.get('local') for interface in interfaces
           if interface['ifname'] != owned_interface for entry in interface.get('addr_info', [])):
        raise ValueError('This address already belongs to the host; choose a separate unused IP')
    if not any(address in network and address not in (network.network_address, network.broadcast_address) for network in networks):
        raise ValueError('The project IP must belong to a connected IPv4 subnet on this bridge')
    if any(item.get('address') == str(address) and item.get('id') != identifier for item in summaries()):
        raise ValueError('This IP is already reserved by another Docker project')
    already_owned = any(interface['ifname'] == owned_interface and
                        any(entry.get('local') == str(address) for entry in interface.get('addr_info', []))
                        for interface in interfaces)
    if probe and not already_owned:
        result = subprocess.run(['arping', '-D', '-I', bridge, '-c', '3', '-w', '4', str(address)],
                                text=True, capture_output=True, timeout=10)
        if result.returncode != 0:
            raise ValueError('The IP responded to duplicate-address detection; choose an unused address')


def network_preflight(data):
    bridge_addresses(data['bridge'])
    if data.get('ipv4mode', 'static') == 'static':
        check_address(data['address'], data['bridge'], probe=True)
    if any(item['node'] == current_node() and item['name'] == data['name'] for item in summaries()):
        raise ValueError('A project with this name already exists on this node')
    if data.get('hwaddr') and any(item.get('hwaddr') == data['hwaddr'] for item in summaries()):
        raise ValueError('This MAC address is already used by another project')


def ensure_link(info):
    interface = info['interface']
    links = json.loads(capture(['ip', '-j', '-d', 'link', 'show']))
    existing = next((link for link in links if link['ifname'] == interface), None)
    owner = 'betterdocker:' + info['id']
    if existing:
        if existing.get('ifalias') != owner or existing.get('linkinfo', {}).get('info_kind') != 'macvlan':
            raise ValueError('Project interface name is already used by another network device')
    else:
        run(['ip', 'link', 'add', 'link', info['bridge'], 'name', interface, 'address', info['hwaddr'],
             'type', 'macvlan', 'mode', 'bridge'])
        run(['ip', 'link', 'set', 'dev', interface, 'alias', owner])
    # This is an IPv4 endpoint. Prevent RA from installing an unintended host IPv6 route.
    ipv6 = Path('/proc/sys/net/ipv6/conf') / interface / 'disable_ipv6'
    if ipv6.exists() and ipv6.read_text().strip() != '1':
        run(['sysctl', '-q', '-w', 'net.ipv6.conf.' + interface + '.disable_ipv6=1'])
    if info.get('mtu'):
        run(['ip', 'link', 'set', 'dev', interface, 'mtu', str(info['mtu'])])
    run(['ip', 'link', 'set', 'dev', interface, 'up'])


def add_address(interface, address):
    existing = json.loads(capture(['ip', '-j', '-4', 'address', 'show', 'dev', interface]))
    if any(entry.get('local') == address for item in existing for entry in item.get('addr_info', [])):
        return
    # No connected routes, default routes or DNS settings on the management interface are changed.
    run(['ip', 'address', 'add', address + '/32', 'dev', interface])


def dhcp_paths(info):
    directory = ROOT / project_id(info['id'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / 'dhcp-address.json', Path('/run') / ('betterdocker-dhcp-' + info['id'] + '.pid')


def lease(info):
    path, _ = dhcp_paths(info)
    try:
        data = json.loads(path.read_text())
        if data.get('expires', 0) > time.time() and data.get('interface') == info['interface']:
            return data
    except (OSError, ValueError):
        pass
    return None


def dhcp_command(info):
    directory = ROOT / info['id']
    _, pid = dhcp_paths(info)
    config = directory / 'dhclient.conf'
    config.write_text('timeout 60;\nretry 10;\n' +
                      'send host-name "' + info['name'] + '";\n' +
                      'send dhcp-client-identifier 1:' + info['hwaddr'] + ';\n' +
                      'request subnet-mask, dhcp-lease-time;\n')
    return ['dhclient', '-4', '-pf', str(pid), '-lf', str(directory / 'dhclient.leases'),
            '-cf', str(config), '-sf', '/usr/local/lib/betterdocker/dhcp-hook.py']


def start_dhcp(info):
    # A dedicated systemd unit survives pvedaemon restarts and oneshot refresh jobs.
    run(['systemctl', 'start', 'betterdocker-dhcp@' + project_id(info['id']) + '.service'])


def dhcp_client(identifier, release=False):
    identifier = project_id(identifier)
    info = json.loads((METADATA / (identifier + '.json')).read_text())
    if info['node'] != current_node() or info.get('ipv4mode') != 'dhcp' or info.get('interface') != 'bd' + identifier[:12]:
        raise ValueError('Invalid DHCP project')
    command = dhcp_command(info) + (['-r'] if release else ['-d']) + [info['interface']]
    if release:
        run(command)
    else:
        os.execvpe(command[0], command, {'PATH': os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin'), 'HOME': '/root'})


def ensure_address(info, wait=True):
    # Existing projects from the alias-only release keep their network and address.
    if not info.get('interface'):
        add_address(info['bridge'], info['address'])
        return info['address']
    ensure_link(info)
    if info.get('ipv4mode', 'static') == 'static':
        add_address(info['interface'], info['address'])
        return info['address']
    start_dhcp(info)
    deadline = time.monotonic() + (60 if wait else 0)
    while True:
        assigned = lease(info)
        if assigned:
            check_address(assigned['address'], info['bridge'], info['id'], info['interface'])
            add_address(info['interface'], assigned['address'])
            info['address'] = assigned['address']
            return info['address']
        if not wait:
            return None
        if time.monotonic() >= deadline:
            raise ValueError('No DHCP lease received within 60 seconds. Check the bridge, DHCP server and MAC filtering.')
        time.sleep(1)


def dhcp_event(environment):
    interface = environment.get('interface', '')
    info = next((item for item in summaries() if item.get('interface') == interface and
                 item['node'] == current_node() and item.get('ipv4mode') == 'dhcp'), None)
    if not info:
        raise ValueError('DHCP hook was called for an unmanaged interface')
    reason = environment.get('reason', '')
    path, _ = dhcp_paths(info)
    if reason == 'TIMEOUT':
        raise ValueError('Waiting for a valid DHCP offer; an expired fallback lease is not accepted')
    if reason in ('BOUND', 'RENEW', 'REBIND', 'REBOOT'):
        address = str(ipaddress.IPv4Address(environment.get('new_ip_address', '')))
        check_address(address, info['bridge'], info['id'], interface)
        previous = lease(info)
        if not previous or previous['address'] != address:
            check_address(address, info['bridge'], info['id'], interface, probe=True)
        add_address(interface, address)
        seconds = int(environment.get('new_dhcp_lease_time', '3600'))
        write_json(path, {'address': address, 'interface': interface, 'expires': time.time() + max(1, seconds)})
        if previous and previous['address'] != address:
            run(['ip', 'address', 'del', previous['address'] + '/32', 'dev', interface])
    elif reason in ('EXPIRE', 'FAIL', 'STOP', 'RELEASE'):
        addresses = json.loads(capture(['ip', '-j', '-4', 'address', 'show', 'dev', interface]))
        for item in addresses:
            for entry in item.get('addr_info', []):
                run(['ip', 'address', 'del', f"{entry['local']}/{entry['prefixlen']}", 'dev', interface])
        path.unlink(missing_ok=True)
    # PREINIT and other advisory events do not touch routes or resolv.conf.


def rebind_runtime(directory, address):
    if not address or address == '0.0.0.0':
        raise ValueError('A dedicated address must be acquired before deployment')
    path = directory / 'compose.runtime.json'
    config = json.loads(path.read_text())
    for service in config['services'].values():
        for port in service.get('ports', []):
            port['host_ip'] = address
    write_json(path, config)


def sync_dhcp(info):
    if info.get('ipv4mode') != 'dhcp':
        return True
    directory = ROOT / info['id']
    ensure_link(info)
    start_dhcp(info)
    assigned = lease(info)
    if not assigned:
        if info['status'] in ('running', 'partial', 'creating'):
            if (directory / 'compose.runtime.json').exists():
                run(compose_base(directory, info['id']) + ['stop'], timeout=1800)
            info['network_resume'] = True
            info['status'] = 'network-error'
            write_json(METADATA / (info['id'] + '.json'), info)
        return False
    changed = info.get('address') != assigned['address']
    if changed or info.get('network_resume'):
        rebind_runtime(directory, assigned['address'])
        info['address'] = assigned['address']
        if info['status'] in ('running', 'partial', 'creating') or info.get('network_resume'):
            run(compose_base(directory, info['id']) + ['up', '-d'], timeout=1800)
        info.pop('network_resume', None)
        if info['status'] == 'network-error':
            info['status'] = 'creating'
        write_json(METADATA / (info['id'] + '.json'), info)
    return True


def remove_network(info):
    if not info.get('interface'):
        run(['ip', 'address', 'del', info['address'] + '/32', 'dev', info['bridge']])
        return
    if info.get('ipv4mode') == 'dhcp':
        run(['systemctl', 'stop', 'betterdocker-dhcp@' + info['id'] + '.service'])
    links = json.loads(capture(['ip', '-j', '-d', 'link', 'show']))
    if any(link['ifname'] == info['interface'] and link.get('ifalias') == 'betterdocker:' + info['id'] for link in links):
        run(['ip', 'link', 'del', 'dev', info['interface']])


def current_node():
    return os.uname().nodename.split('.')[0]


def create(data, node, identifier=None):
    if node != current_node():
        raise ValueError('The request was sent to the wrong node')
    identifier = project_id(identifier) if identifier else uuid.uuid4().hex
    with locked():
        if (METADATA / (identifier + ".json")).exists() or (ROOT / identifier).exists():
            raise ValueError("Project ID already exists")
        network_preflight(data)
        directory = ROOT / identifier
        try:
            config = prepare(dict(data, address=data.get('address') or '0.0.0.0'), identifier, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        write_json(directory / 'request.json', data)
        info = {'id': identifier, 'node': node, 'name': data['name'], 'bridge': data['bridge'],
                'address': data['address'], 'status': 'creating', 'created': int(time.time()),
                'expected': sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values()),
                'ipv4mode': data.get('ipv4mode', 'static'), 'cidr': data.get('cidr', ''),
                'interface': 'bd' + identifier[:12], 'mtu': data.get('mtu', 0),
                'hwaddr': data.get('hwaddr') or '02:' + ':'.join(identifier[i:i+2] for i in range(0, 10, 2))}
        write_json(METADATA / (identifier + '.json'), info)
        try:
            ensure_address(info)
            rebind_runtime(directory, info['address'])
            write_json(METADATA / (identifier + '.json'), info)
            run(compose_base(directory, identifier) + ['up', '-d'], timeout=1800)
            refresh(node)
        except Exception:
            info['status'] = 'error'
            write_json(METADATA / (identifier + '.json'), info)
            print(f'Project {identifier} retained for diagnosis; open its Docker screen.', file=sys.stderr)
            raise
    print(f"Created {data['name']} at {info['address']}")


def detail(identifier, node):
    identifier = project_id(identifier)
    info = json.loads((METADATA / (identifier + '.json')).read_text())
    if info['node'] != node or node != current_node():
        raise ValueError('Project belongs to another node')
    return dict(info, deployment=json.loads((ROOT / identifier / 'request.json').read_text()))


def refresh(node):
    # Rebinding may recreate containers, so refresh the state list afterwards.
    projects = [info for info in summaries() if info['node'] == node]
    for info in projects:
        sync_dhcp(info)
    entries = json.loads('[' + ','.join(capture(['docker', 'ps', '-a', '--filter', 'label=betterdocker.id',
                                              '--format', '{{json .}}']).splitlines()) + ']')
    for info in projects:
        if info['status'] == 'network-error':
            continue
        states = [entry['State'] for entry in entries
                  if ('betterdocker.id=' + info['id']) in entry.get('Labels', '').split(',')]
        status = 'running' if states and len(states) >= info.get('expected', 1) and all(state == 'running' for state in states) else (
            'partial' if 'running' in states else 'stopped')
        # Preserve a failed deployment until it is started successfully or stopped explicitly.
        if info['status'] == 'error' and status != 'running':
            continue
        info['status'] = status
        write_json(METADATA / (info['id'] + '.json'), info)
    due = [info for info in projects if not (ROOT / info['id'] / 'metrics.json').exists() or
           time.time() - (ROOT / info['id'] / 'metrics.json').stat().st_mtime >= 60]
    if due:
        active = [entry['ID'] for entry in entries if entry['State'] == 'running']
        try:
            shared_stats = [json.loads(line) for line in capture(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *active]).splitlines()] if active else []
            for info in due:
                record_metrics(management(info['id'], node, shared_stats=shared_stats))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f'Metrics unavailable: {error}', file=sys.stderr)


def action(identifier, node, operation):
    with locked():
        info = detail(identifier, node)
        directory = ROOT / identifier
        commands = {'start': ['up', '-d'], 'stop': ['stop'], 'restart': ['up', '-d', '--force-recreate'], 'delete': ['down']}
        if operation not in commands:
            raise ValueError('Unsupported operation')
        if operation in ('start', 'restart'):
            ensure_address(info)
            rebind_runtime(directory, info['address'])
            info.pop('deployment', None)
            info.pop('network_resume', None)
            info['status'] = 'creating'
            write_json(METADATA / (identifier + '.json'), info)
        run(compose_base(directory, identifier) + commands[operation], timeout=1800)
        if operation == 'delete':
            # Reserve the metadata until the owned alias is successfully released.
            remove_network(info)
            (METADATA / (identifier + '.json')).unlink()
            print(f'Project removed. Volumes and bind-mount data are retained at {directory}.')
        else:
            info.pop('deployment', None)
            if operation == 'stop':
                info.pop('network_resume', None)
                info['status'] = 'stopped'
                write_json(METADATA / (identifier + '.json'), info)
            refresh(node)


def update(identifier, node, data):
    with locked():
        existing = detail(identifier, node)
        previous = existing['deployment']
        for key in ('bridge', 'name', 'ipv4mode', 'hwaddr', 'mtu'):
            default = 'static' if key == 'ipv4mode' else 0 if key == 'mtu' else ''
            if data.get(key, default) != previous.get(key, default):
                raise ValueError('Network settings cannot be changed during redeployment')
        for key in ('bridge', 'name'):
            if data[key] != existing[key]:
                raise ValueError('Project name, bridge and IP cannot be changed during redeployment')
        if data.get('ipv4mode', 'static') == 'static' and data['address'] != existing['address']:
            raise ValueError('The project IP cannot be changed during redeployment')
        directory = ROOT / identifier
        with tempfile.TemporaryDirectory(prefix='betterdocker-update-', dir=ROOT) as folder:
            staging = Path(folder)
            config = prepare(dict(data, address=existing.get('address') or '0.0.0.0'), identifier, staging, project_directory=directory)
            for filename in ('compose.source.yaml', '.env', 'compose.runtime.json'):
                os.replace(staging / filename, directory / filename)
        write_json(directory / 'request.json', data)
        info = {key: value for key, value in existing.items() if key != 'deployment'}
        info['expected'] = sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())
        info['status'] = 'creating'
        write_json(METADATA / (identifier + '.json'), info)
        try:
            ensure_address(info)
            rebind_runtime(directory, info['address'])
            write_json(METADATA / (identifier + '.json'), info)
            run(compose_base(directory, identifier) + ['up', '-d', '--remove-orphans'], timeout=1800)
            refresh(node)
        except Exception:
            info['status'] = 'error'
            write_json(METADATA / (identifier + '.json'), info)
            raise


def restore(node):
    with locked():
        for info in summaries():
            if info['node'] == node:
                ensure_address(info, wait=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['validate', 'create', 'detail', 'action', 'logs', 'restore', 'refresh', 'update', 'dhcp-client', 'dhcp-release', 'management', 'settings', 'console-check', 'console', 'new-id', 'notes', 'rrddata'])
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    values = args.arguments
    if args.operation == 'new-id':
        print(uuid.uuid4().hex)
    elif args.operation in ('validate', 'create'):
        data = validate(json.loads(Path(values[0]).read_text()))
        if args.operation == 'create':
            create(data, values[1], values[2] if len(values) > 2 else None)
    elif args.operation == 'update':
        update(values[0], values[1], validate(json.loads(Path(values[2]).read_text())))
    elif args.operation in ('dhcp-client', 'dhcp-release'):
        dhcp_client(values[0], release=args.operation == 'dhcp-release')
    elif args.operation == 'notes':
        print(json.dumps(notes(values[0], values[1], Path(values[2]).read_text() if len(values) > 2 else None)))
    elif args.operation == 'rrddata':
        print(json.dumps(rrddata(*values)))
    elif args.operation == 'management':
        print(json.dumps(management(*values)))
    elif args.operation == 'settings':
        service_settings(values[0], values[1], values[2], json.loads(Path(values[3]).read_text()))
    elif args.operation in ('console-check', 'console'):
        command = console_target(*values)
        if args.operation == 'console':
            os.execvp(command[0], command)
    elif args.operation == 'detail':
        print(json.dumps(detail(*values)))
    elif args.operation == 'action':
        action(*values)
    elif args.operation == 'logs':
        info = detail(*values)
        run(compose_base(ROOT / info['id'], info['id']) + ['logs', '--no-color', '--tail', '200'])
    elif args.operation == 'restore':
        restore(current_node())
    elif args.operation == 'refresh':
        with locked():
            refresh(current_node())


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'BetterDocker: {error}', file=sys.stderr)
        sys.exit(1)
