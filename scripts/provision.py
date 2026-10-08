#!/usr/bin/env python3
"""Native host Docker projects. Public cluster metadata never contains deployment secrets."""
import argparse
import contextlib
from datetime import datetime
import fcntl
import hashlib
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
PVE_NODES = Path('/etc/pve/nodes')


def reserved_ids():
    registry = METADATA / 'ids'
    return {int(path.stem): json.loads(path.read_text()) for path in registry.glob('*.json')}


def reserve_id(identifier, node, preferred=None):
    """Called under the cluster datacenter.cfg lock by the API/installer."""
    project_id(identifier)
    registry = reserved_ids()
    for vmid, entry in registry.items():
        if entry['id'] == identifier:
            path = METADATA / (identifier + '.json')
            if path.exists():
                info = json.loads(path.read_text())
                info['vmid'] = vmid
                write_json(path, info)
            return vmid
    occupied = set(registry)
    occupied.update(int(path.stem) for kind in ('qemu-server', 'lxc')
                    for path in PVE_NODES.glob('*/' + kind + '/*.conf') if path.stem.isdigit())
    occupied.update(int(info['vmid']) for info in summaries() if info.get('vmid'))
    if preferred not in (None, ''):
        vmid = int(preferred)
        if not 100 <= vmid <= 999999999:
            raise ValueError('DCT ID must be between 100 and 999999999')
        if vmid in occupied:
            raise ValueError(f'Guest ID {vmid} is already in use')
    else:
        vmid = next(value for value in range(100, 1000000) if value not in occupied)
    write_json(METADATA / 'ids' / f'{vmid}.json', {'id': identifier, 'node': node})
    path = METADATA / (identifier + '.json')
    if path.exists():
        info = json.loads(path.read_text())
        info['vmid'] = vmid
        write_json(path, info)
    return vmid


def release_id(identifier):
    for vmid, entry in reserved_ids().items():
        if entry['id'] == identifier:
            (METADATA / 'ids' / f'{vmid}.json').unlink()


def run(args, **kwargs):
    kwargs.setdefault('timeout', 60)
    kwargs.setdefault('env', {'PATH': os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin'), 'HOME': '/root'})
    kwargs.setdefault('check', True)
    return subprocess.run(args, text=True, **kwargs)


def capture(args):
    return run(args, capture_output=True).stdout


def write_json(path, value):
    if path.parent == METADATA and isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != 'digest'}
    # A refresh that started before migration must not overwrite the assigned ID.
    if path.parent == METADATA and isinstance(value, dict) and value.get('id') and not value.get('vmid'):
        vmid = next((number for number, entry in reserved_ids().items() if entry['id'] == value['id']), None)
        if vmid:
            value = dict(value, vmid=vmid)
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
    allowed = {'name', 'mode', 'compose', 'image', 'ports', 'environment', 'bridge', 'address', 'ipv4mode', 'cidr', 'hwaddr', 'mtu', 'vmid'}
    if set(data) - allowed:
        raise ValueError('Unknown deployment setting')
    vmid = data.get('vmid')
    if vmid not in (None, '') and (isinstance(vmid, bool) or not isinstance(vmid, int) or not 100 <= vmid <= 999999999):
        raise ValueError('DCT ID must be an integer between 100 and 999999999')
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
    except subprocess.CalledProcessError as error:
        locations = sorted(set(re.findall(r'line [0-9]+', error.stderr or '')))
        location = ' (' + ', '.join(locations) + ')' if locations else ''
        raise ValueError('Invalid Compose configuration' + location + '. Check YAML, .env and referenced files.') from None
    config = bind_ports(config, data['address'], identifier)
    apply_settings(config, load_settings(project_directory))
    apply_storage(config, identifier)
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
    if not isinstance(value, dict) or set(value) - {'cpus', 'memory', 'swap', 'cpuunits', 'pids', 'dns', 'dns_search', 'restart', 'disk'}:
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
    if 'disk' in value:
        disk = value['disk']
        if isinstance(disk, bool) or not isinstance(disk, int) or not 0 <= disk <= 1048576:
            raise ValueError('Invalid disk limit')
        result['disk'] = disk
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
        if 'disk' in values:
            options = service.setdefault('storage_opt', {})
            options.pop('size', None)
            if values['disk']:
                options['size'] = str(values['disk']) + 'M'
            if not options:
                service.pop('storage_opt', None)
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
                        'image': item['Config']['Image'], 'pid': item['State'].get('Pid', 0),
                        'stop_signal': item['Config'].get('StopSignal') or 'SIGTERM',
                        'health': item['State'].get('Health', {}).get('Status', 'none'),
                        'restarts': item.get('RestartCount', 0), 'exitcode': item['State'].get('ExitCode', 0)})
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
    seconds = {'hour': 3600, 'day': 86400, 'week': 604800}
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


def rootfs_quota_support():
    """Do not advertise Docker size quotas on an unsupported backing filesystem."""
    cache = ROOT.parent / 'rootfs-quota.json'
    if cache.exists() and time.time() - cache.stat().st_mtime < 60:
        return json.loads(cache.read_text())
    result = {'supported': False, 'reason': 'Requires overlay2 on XFS with project quotas'}
    try:
        info = json.loads(capture(['docker', 'info', '--format', '{{json .}}']))
        fs = json.loads(capture(['findmnt', '-J', '-T', info['DockerRootDir'], '-o', 'FSTYPE,OPTIONS']))['filesystems'][0]
        options = set(fs['options'].split(','))
        result['supported'] = info.get('Driver') == 'overlay2' and fs['fstype'] == 'xfs' and bool(options & {'pquota', 'prjquota'})
        result['driver'] = info.get('Driver', '')
        if result['supported']:
            result['reason'] = ''
        write_json(cache, result)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return result


def disk_mib(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(?:i?b)?', str(value), re.I)
    if not match:
        return 0
    return int(float(match[1]) * (1024 ** (' kmgt'.index(match[2].lower()))) / 1048576)


def storage_config(identifier):
    path = ROOT / project_id(identifier) / 'storage.json'
    return json.loads(path.read_text()) if path.exists() else {}


def storage_mount(identifier):
    folder = ROOT / project_id(identifier)
    config = storage_config(identifier)
    if not config:
        return
    mountpoint = folder / 'storage-data'
    image = folder / 'storage.ext4'
    # Mount by exact owned image; fail closed if an unrelated mount occupies the path.
    if os.path.ismount(mountpoint):
        device = capture(['findmnt', '-n', '-M', str(mountpoint), '-o', 'SOURCE']).strip()
        backing = capture(['losetup', '-n', '-O', 'BACK-FILE', device]).strip()
        if Path(backing).resolve() != image.resolve():
            raise ValueError('Project storage mount is occupied by another filesystem')
        return
    mountpoint.mkdir(mode=0o700, exist_ok=True)
    run(['mount', '-o', 'loop,nodev,nosuid', str(image), str(mountpoint)])


def apply_storage(config, identifier):
    settings = storage_config(identifier)
    if settings:
        storage_mount(identifier)
        mapping = settings['mapping']
        for service in config.get('services', {}).values():
            for mount in service.get('volumes', []):
                key = mount.get('type', '') + ':' + mount.get('source', '')
                if key in mapping:
                    mount.update(type='bind', source=mapping[key])
                    mount.pop('volume', None)
                    mount.pop('bind', None)
                elif not mount.get('read_only') and mount.get('type') != 'tmpfs' and mount.get('source') not in mapping.values():
                    raise ValueError('New writable mounts require a separate data migration; existing data disk cannot be bypassed')
    return config


def project_storage_status(identifier):
    settings = storage_config(identifier)
    result = {'limit_mib': settings.get('size_mib', 0), 'rootfs': rootfs_quota_support(), 'mounted': False}
    if settings:
        point = ROOT / identifier / 'storage-data'
        result['mounted'] = os.path.ismount(point)
        if result['mounted']:
            fs = os.statvfs(point)
            result.update(used=(fs.f_blocks - fs.f_bfree) * fs.f_frsize, available=fs.f_bavail * fs.f_frsize)
    return result


def project_storage_limit(identifier, node, size_mib, digest=None):
    size_mib = int(size_mib)
    if not 256 <= size_mib <= 1048576:
        raise ValueError('Project data disk must be between 256 and 1048576 MiB')
    with locked():
        detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        if any(item['state'] not in ('created', 'exited', 'dead') for item in container_records(identifier)):
            raise ValueError('Stop all project services before changing the data disk')
        folder = ROOT / identifier
        previous = storage_config(identifier)
        if not previous:
            check_protection(detail(identifier, node), "can't replace DCT data mounts")
        if previous and size_mib <= previous['size_mib']:
            raise ValueError('Data disks can only be enlarged; shrinking or removing a disk is not supported')
        image, point = folder / 'storage.ext4', folder / 'storage-data'
        if previous:
            storage_mount(identifier)
            with image.open('r+b') as handle:
                handle.truncate(size_mib * 1048576)
            device = capture(['findmnt', '-n', '-M', str(point), '-o', 'SOURCE']).strip()
            run(['losetup', '-c', device])
            run(['resize2fs', device], timeout=1800)
            previous['size_mib'] = size_mib
            write_json(folder / 'storage.json', previous)
            return
        runtime = folder / 'compose.runtime.json'
        original = runtime.read_text()
        config = json.loads(original)
        mapping, sources = {}, {}
        for service in config['services'].values():
            for mount in service.get('volumes', []):
                if mount.get('read_only') or mount.get('type') == 'tmpfs':
                    continue
                kind, source = mount.get('type'), mount.get('source', '')
                key = str(kind) + ':' + source
                if kind == 'bind':
                    path = Path(source)
                    # Do not migrate arbitrary host paths, sockets or configuration files.
                    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or path.resolve() == folder.resolve():
                        raise ValueError('Data disk requires project-owned directory mounts; external host paths are not migrated')
                    if path.exists() and not path.is_dir():
                        raise ValueError('Data disk supports directory mounts only')
                elif kind == 'volume':
                    definition = config.get('volumes', {}).get(source, {})
                    if definition.get('external') or definition.get('driver_opts'):
                        raise ValueError('External or custom-driver volumes cannot be migrated to the data disk')
                    volume_name = definition.get('name', 'bd-' + identifier + '_' + source)
                    volume = json.loads(capture(['docker', 'volume', 'inspect', volume_name]))[0]
                    if volume.get('Driver') != 'local' or volume.get('Options') or volume.get('Labels', {}).get('com.docker.compose.project') != 'bd-' + identifier:
                        raise ValueError('Only local volumes owned by this Compose project can be migrated')
                    path = Path(volume['Mountpoint'])
                    users = capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + volume_name]).split()
                    owned = {item['id'] for item in container_records(identifier)}
                    if any(item not in owned for item in users):
                        raise ValueError('A volume is shared with another project')
                else:
                    raise ValueError('Unsupported writable mount')
                sources[key] = path
        if not sources:
            raise ValueError('Project has no writable data directory or local volume')
        if image.exists() or point.exists():
            raise ValueError('An unfinished storage migration exists; inspect it before retrying')
        needed = sum(int(capture(['du', '-sx', '-B1', str(path)]).split()[0]) for path in set(sources.values()) if path.exists())
        if needed + 64 * 1048576 > size_mib * 1048576 * 0.9:
            raise ValueError('Data disk is too small for the current data and filesystem overhead')
        for command in ('mkfs.ext4', 'mount', 'losetup', 'resize2fs', 'findmnt', 'cp', 'du', 'systemd-escape', 'systemctl'):
            if not shutil.which(command):
                raise ValueError('Missing storage tool: ' + command)
        # Original sources remain intact as a recovery copy. Runtime is switched only after copying succeeds.
        with image.open('xb') as handle:
            os.chmod(image, 0o600)
            handle.truncate(size_mib * 1048576)
        mounted = False
        try:
            run(['mkfs.ext4', '-q', '-m', '0', str(image)])
            point.mkdir(mode=0o700)
            run(['mount', '-o', 'loop,nodev,nosuid', str(image), str(point)])
            mounted = True
            for index, (key, path) in enumerate(sources.items()):
                target = point / ('data-' + str(index))
                target.mkdir()
                if path.exists():
                    run(['cp', '-a', '--', str(path) + '/.', str(target)], timeout=1800)
                mapping[key] = str(target)
            write_json(folder / 'storage.json', {'size_mib': size_mib, 'mapping': mapping})
            apply_storage(config, identifier)
            write_json(runtime, config)
            unit = capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
            if not re.fullmatch(r'[a-zA-Z0-9_.\\-]+\.mount', unit):
                raise ValueError('Invalid storage mount unit')
            unit_path = Path('/etc/systemd/system') / unit
            unit_path.write_text('[Unit]\nDescription=BetterDocker project data disk\nBefore=docker.service\n\n[Mount]\nWhat=' + str(image) + '\nWhere=' + str(point) + '\nType=ext4\nOptions=loop,nodev,nosuid\n\n[Install]\nRequiredBy=docker.service\n')
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'enable', unit])
        except Exception:
            runtime.write_text(original)
            (folder / 'storage.json').unlink(missing_ok=True)
            if 'unit_path' in locals():
                with contextlib.suppress(subprocess.SubprocessError):
                    run(['systemctl', 'disable', unit])
                unit_path.unlink(missing_ok=True)
                run(['systemctl', 'daemon-reload'])
            if mounted:
                run(['umount', str(point)])
            # Preserve partially copied disk for diagnosis; never erase application data.
            raise
        (folder / 'snapshot-restored.json').unlink(missing_ok=True)
        print('Project data moved to a bounded disk. Original sources retained as a recovery copy.')



def service_values(service):
    limits = service.get('deploy', {}).get('resources', {}).get('limits', {})
    memory = int(service.get('mem_limit') or limits.get('memory') or 0) // 1048576
    total = int(service.get('memswap_limit') or 0) // 1048576
    values = {'cpus': float(service.get('cpus') or limits.get('cpus') or 0), 'memory': memory,
              'swap': -1 if service.get('memswap_limit') == -1 else max(0, total - memory) if total else memory, 'cpuunits': service.get('cpu_shares', 0),
              'pids': max(0, int(service.get('pids_limit') or limits.get('pids') or 0)),
              'dns': service.get('dns', []), 'dns_search': service.get('dns_search', []),
              'restart': service.get('restart', 'no')}
    if service.get('storage_opt', {}).get('size'):
        values['disk'] = disk_mib(service['storage_opt']['size'])
    return values


def load_pending(directory):
    path = directory / 'pending.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_pending(directory, pending):
    # Empty queues have the same digest as configurations created before this feature.
    pending = {name: changes for name, changes in pending.items() if changes}
    if pending:
        write_json(directory / 'pending.json', pending)
    else:
        (directory / 'pending.json').unlink(missing_ok=True)


def pending_settings(identifier, node, service):
    detail(identifier, node)
    directory = ROOT / identifier
    config = json.loads((directory / 'compose.runtime.json').read_text())
    if service not in config['services']:
        raise ValueError('Unknown Compose service')
    values = load_settings(directory).get(service, service_values(config['services'][service]))
    values = dict(values, disk=values.get('disk', 0))
    pending = load_pending(directory).get(service, {})
    return [dict(key=key, value=value, **({'pending': pending[key]} if key in pending else {}))
            for key, value in values.items()]


def revert_settings(identifier, node, service, keys, digest=None):
    with locked():
        detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        directory = ROOT / identifier
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if service not in config['services']:
            raise ValueError('Unknown Compose service')
        values = load_settings(directory).get(service, service_values(config['services'][service]))
        values = dict(values, disk=values.get('disk', 0))
        names = keys.split(',')
        authorize_config_keys(names)
        if not names or any(key not in values for key in names):
            raise ValueError('Invalid setting to revert')
        pending = load_pending(directory)
        changes = dict(pending.get(service, {}))
        for key in names:
            changes.pop(key, None)
        validate_settings(dict(values, **changes))
        pending[service] = changes
        save_pending(directory, pending)



def management(identifier, node, shared_stats=None):
    info = detail(identifier, node)
    directory = ROOT / identifier
    config = json.loads((directory / 'compose.runtime.json').read_text())
    settings = load_settings(directory)
    info['services'] = []
    for name, service in config['services'].items():
        values = service_values(service)
        info['services'].append({'name': name, 'image': service.get('image', ''),
                                 'settings': settings.get(name, values), 'replicas': service.get('deploy', {}).get('replicas', 1),
                                 'mounts': service.get('volumes', []), 'ports': service.get('ports', []),
                                 'overridden': name in settings, 'pending': load_pending(directory).get(name, {})})
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
    info['storage'] = project_storage_status(identifier)
    info['operation'] = operation_state(identifier)
    return info


def require_stopped(containers):
    if any(c['state'] not in ('created', 'exited', 'dead', 'stopped') for c in containers):
        raise ValueError('Stop the affected service or DCT before changing these settings')


def live_settings_arguments(before, after):
    """Only Docker's mutable settings qualify; everything else needs a stop."""
    flags = {'cpus': '--cpus', 'cpu_shares': '--cpu-shares',
             'mem_limit': '--memory', 'memswap_limit': '--memory-swap',
             'pids_limit': '--pids-limit', 'restart': '--restart'}
    old, new = dict(before), dict(after)
    arguments = []
    for key, flag in flags.items():
        a, b = old.pop(key, None), new.pop(key, None)
        if a == b:
            continue
        # Zero CPU, memory and shares are ignored by Docker update. Never silently recreate.
        if key in ('cpus', 'mem_limit') and not a:
            raise ValueError('Stop the affected service before adding this limit to an unlimited container')
        if key in ('cpus', 'mem_limit', 'cpu_shares') and not b:
            raise ValueError('Stop the affected service before removing this limit')
        value = b if b is not None else -1 if key in ('memswap_limit', 'pids_limit') else 'no' if key == 'restart' else 0
        arguments.extend([flag, str(value)])
    for key in ('dns', 'dns_search'):
        if not old.get(key): old.pop(key, None)
        if not new.get(key): new.pop(key, None)
    if old != new:
        raise ValueError('Stop the affected service before changing DNS, disk or Compose settings')
    return arguments


def update_live_containers(containers, arguments):
    if not arguments:
        return
    snapshots = json.loads(capture(['docker', 'inspect', *[c['id'] for c in containers]]))
    saved = {c['Id']: c['HostConfig'] for c in snapshots}
    for flag, key in (('--cpus', 'NanoCpus'), ('--memory', 'Memory')):
        if flag in arguments[::2] and any(not h.get(key) for h in saved.values()):
            raise ValueError('Stop the affected service before adding this limit to an unlimited container')
    touched = []
    try:
        for container in containers:
            identifier = container['id']
            touched.append(identifier)
            run(['docker', 'update', *arguments, identifier])
    except Exception as error:
        failures = []
        keys = {'--cpus': ('NanoCpus', 0), '--cpu-shares': ('CpuShares', 1024),
                '--memory': ('Memory', 0), '--memory-swap': ('MemorySwap', -1),
                '--pids-limit': ('PidsLimit', -1)}
        for identifier in touched:
            rollback = []
            for flag in arguments[::2]:
                if flag == '--restart':
                    policy = saved[identifier]['RestartPolicy']
                    value = policy['Name'] or 'no'
                    if value == 'on-failure' and policy.get('MaximumRetryCount'):
                        value += ':' + str(policy['MaximumRetryCount'])
                else:
                    key, default = keys[flag]
                    value = saved[identifier].get(key) or default
                    if flag == '--memory-swap' and not saved[identifier].get(key):
                        value = 2 * saved[identifier].get('Memory', 0) or -1
                    if flag == '--cpus': value /= 1000000000
                rollback.extend([flag, str(value)])
            try:
                run(['docker', 'update', *rollback, identifier])
            except Exception as failure:
                failures.append(str(failure))
        if failures:
            raise ValueError('Resource update failed; restoring container settings also failed: ' + '; '.join(failures)) from error
        raise


def authorize_config_keys(keys):
    allowed = os.environ.get('BETTERDOCKER_CONFIG_KEYS', '*')
    if allowed != '*' and set(keys) - set(allowed.split(',')):
        raise ValueError('Permission denied: configuration field requires additional privileges')


def service_settings(identifier, node, service, values, digest=None):
    validate_settings(values)
    if values.get('disk') and not rootfs_quota_support()['supported']:
        raise ValueError('Container disk limit requires overlay2 on XFS with project quotas; volumes use a separate data disk')
    with locked():
        detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        rows = pending_settings(identifier, node, service)
        effective = {row['key']: row.get('pending', row['value']) for row in rows}
        proposed = validate_settings(dict(effective, **values))
        if 'disk' not in values and not proposed.get('disk'):
            proposed.pop('disk', None)
        authorize_config_keys(key for key in proposed if proposed[key] != effective.get(key))
        _service_settings(identifier, node, service, proposed)


def _service_settings(identifier, node, service, values):
    """Caller holds the worker lock. Only active settings enter the runtime file."""
    directory = ROOT / identifier
    runtime = directory / 'compose.runtime.json'
    original = runtime.read_text()
    config = json.loads(original)
    if service not in config['services']:
        raise ValueError('Unknown Compose service')
    settings = load_settings(directory)
    current = settings.get(service, service_values(config['services'][service]))
    pending = load_pending(directory)
    containers = [c for c in container_records(identifier) if c['service'] == service]
    active = any(c['state'] not in ('created', 'exited', 'dead', 'stopped') for c in containers)
    changed = {key: value for key, value in values.items() if current.get(key, 0 if key == 'disk' else None) != value}
    if 'disk' in changed:
        check_protection(detail(identifier, node), "can't update DCT container disk")
    if not changed:
        pending.pop(service, None)
        save_pending(directory, pending)
        return
    if active:
        # Persist requested values first, as LXC does before trying hotplug.
        pending[service] = changed
        save_pending(directory, pending)
        immediate = dict(current)
        waiting = dict(changed)
        before = json.loads(original)['services'][service]
        for group in (('memory', 'swap'), ('cpus',), ('cpuunits',), ('pids',), ('restart',), ('dns', 'dns_search'), ('disk',)):
            selected = {key: values[key] for key in group if key in changed}
            if not selected or any(c['state'] != 'running' for c in containers):
                continue
            candidate = dict(immediate, **selected)
            proposed = json.loads(original)
            apply_settings(proposed, {service: candidate})
            try:
                live_settings_arguments(before, proposed['services'][service])
            except ValueError:
                continue
            immediate = candidate
            for key in selected:
                waiting.pop(key, None)
        if immediate != current:
            apply_settings(config, {service: immediate})
            arguments = live_settings_arguments(before, config['services'][service])
            # Docker errors leave the requested changes pending and current state intact.
            update_live_containers(containers, arguments)
            write_json(runtime, config)
            settings[service] = immediate
            write_json(directory / 'settings.json', settings)
        pending[service] = waiting
        save_pending(directory, pending)
    else:
        # Save the queue before recreation, so a failed Start can be retried or reverted.
        pending[service] = changed
        save_pending(directory, pending)
        apply_settings(config, {service: values})
        write_json(runtime, config)
        try:
            run(compose_base(directory, identifier) + ['up', '--no-start', '--no-deps', service], timeout=1800)
        except Exception:
            runtime.write_text(original)
            raise
        settings[service] = values
        write_json(directory / 'settings.json', settings)
        pending.pop(service, None)
        save_pending(directory, pending)
        marker = directory / 'snapshot-restored.json'
        if marker.exists():
            restored = json.loads(marker.read_text())
            restored['containers'] = [row['id'] for row in container_records(identifier)]
            write_json(marker, restored)
    refresh(node)


def apply_pending_settings(identifier, node, service=None):
    directory = ROOT / identifier
    for name, changes in list(load_pending(directory).items()):
        if service is not None and name != service:
            continue
        require_stopped([c for c in container_records(identifier) if c['service'] == name])
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if name not in config['services']:
            raise ValueError('Unknown Compose service in pending settings')
        current = load_settings(directory).get(name, service_values(config['services'][name]))
        _service_settings(identifier, node, name, validate_settings(dict(current, **changes)))


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
    if (directory / 'storage.json').exists():
        storage_mount(directory.name)
    if not address or address == '0.0.0.0':
        raise ValueError('A dedicated address must be acquired before deployment')
    path = directory / 'compose.runtime.json'
    config = json.loads(path.read_text())
    if (directory / 'storage.json').exists():
        apply_storage(config, directory.name)
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
            if not restored_start(info['id']):
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
        vmid = next((vmid for vmid, entry in reserved_ids().items() if entry['id'] == identifier), None)
        if vmid is None:
            raise ValueError('DCT ID must be reserved through the Proxmox API before deployment')
        if any((PVE_NODES / host.name / kind / f'{vmid}.conf').exists()
               for host in PVE_NODES.iterdir() for kind in ('qemu-server', 'lxc')):
            raise ValueError(f'Guest ID {vmid} is already in use by VM or LXC')
        network_preflight(data)
        directory = ROOT / identifier
        try:
            config = prepare(dict(data, address=data.get('address') or '0.0.0.0'), identifier, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        (directory / 'snapshot-restored.json').unlink(missing_ok=True)
        write_json(directory / 'request.json', data)
        info = {'id': identifier, 'vmid': vmid, 'node': node, 'name': data['name'], 'bridge': data['bridge'],
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


def configuration_digest(directory, deployment, protection=False, tags='', lifecycle=None):
    # Runtime files include assigned IPs and Docker state, not source configuration.
    state = {'deployment': deployment, 'settings': load_settings(directory),
             'storage': json.loads((directory / 'storage.json').read_text()) if (directory / 'storage.json').exists() else None}
    if lifecycle:
        state['lifecycle'] = lifecycle
    if protection:
        state['protection'] = True
    if tags:
        state['tags'] = tags
    notes_path = directory / 'notes.txt'
    if notes_path.exists() and notes_path.read_text():
        state['description'] = notes_path.read_text()
    pending = load_pending(directory)
    if pending:
        state['pending'] = pending
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def assert_configuration_digest(identifier, node, expected):
    if expected is None or expected == '':
        return  # Like PVE, older API clients may omit optimistic concurrency checks.
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid configuration digest')
    if detail(identifier, node)['digest'] != expected:
        raise ValueError('checksum mismatch (file change by other user?)')


def detail(identifier, node):
    identifier = project_id(identifier)
    info = json.loads((METADATA / (identifier + '.json')).read_text())
    if info['node'] != node or node != current_node():
        raise ValueError('Project belongs to another node')
    directory = ROOT / identifier
    deployment = json.loads((directory / 'request.json').read_text())
    return dict(info, deployment=deployment, digest=configuration_digest(directory, deployment, info.get('protection', False), info.get('tags', ''), {key: info[key] for key in ('onboot', 'startup') if info.get(key)}))


def parse_startup(value):
    if not isinstance(value, str):
        raise ValueError('Invalid startup order')
    result = {}
    for entry in value.split(','):
        if not entry.strip():
            continue
        match = re.fullmatch(r'(?:(order|up|down)=)?([0-9]+)', entry)
        if not match:
            raise ValueError('Invalid startup order')
        result[match[1] or 'order'] = int(match[2])
    return result


def access_state(identifier, node):
    info = detail(identifier, node)
    return {'status': info['status'], 'protection': int(bool(info.get('protection'))),
            'operation': operation_state(identifier),
            'active_containers': int(any(item['state'] in ('running', 'restarting', 'paused')
                                         for item in container_records(identifier)))}


def lifecycle_config(identifier, node):
    info = detail(identifier, node)
    return {'onboot': int(bool(info.get('onboot'))), 'startup': info.get('startup', '')}


def restore_restart_policy(identifier, service=None):
    marker = ROOT.parent / 'restart-paused' / (identifier + '.json')
    if not marker.exists():
        return
    config = json.loads((ROOT / identifier / 'compose.runtime.json').read_text())
    for item in container_records(identifier):
        if item['state'] == 'running' and (service is None or item['service'] == service):
            policy = config['services'][item['service']].get('restart', 'no')
            run(['docker', 'update', '--restart=' + policy, item['id']], capture_output=True)


def daemon_prepare(node):
    # Record before changing Docker policies, so an interrupted stop can be retried.
    path = ROOT.parent / 'lifecycle.json'
    with locked():
        rows = []
        for info in summaries():
            if info['node'] != node:
                continue
            records = container_records(info['id'])
            if records:
                write_json(ROOT.parent / 'restart-paused' / (info['id'] + '.json'), {'paused': True})
            for item in records:
                host = json.loads(capture(['docker', 'inspect', item['id']]))[0]['HostConfig']
                rows.append({'project': info['id'], 'id': item['id'], 'state': item['state'],
                             'policy': host['RestartPolicy']})
        previous = json.loads(path.read_text()) if path.exists() else {}
        saved = {row['id']: row for row in previous.get('containers', [])}
        for row in rows:
            if row['policy']['Name'] == 'no' and row['id'] in saved:
                row['policy'] = saved[row['id']]['policy']
        write_json(path, {'boot_id': (PROC / 'sys/kernel/random/boot_id').read_text().strip(), 'containers': rows})
        os.chmod(path, 0o600)
        if rows:
            run(['docker', 'update', '--restart=no', *[row['id'] for row in rows]], capture_output=True)


def daemon_resume(node):
    path = ROOT.parent / 'lifecycle.json'
    if not path.exists():
        return
    with locked():
        state = json.loads(path.read_text())
        if state['boot_id'] != (PROC / 'sys/kernel/random/boot_id').read_text().strip():
            # PVE's startall owns boot ordering. Policies are restored by project Start.
            path.unlink()
            return
        owned = {item['id'] for info in summaries() if info['node'] == node
                 for item in container_records(info['id'])}
        for row in state['containers']:
            if row['id'] not in owned:
                continue
            if row['state'] in ('running', 'restarting', 'paused'):
                run(['docker', 'start', row['id']], capture_output=True)
            policy = row['policy']['Name'] or 'no'
            if policy == 'on-failure' and row['policy'].get('MaximumRetryCount'):
                policy += ':' + str(row['policy']['MaximumRetryCount'])
            run(['docker', 'update', '--restart=' + policy, row['id']], capture_output=True)
        for project in {row['project'] for row in state['containers']}:
            (ROOT.parent / 'restart-paused' / (project + '.json')).unlink(missing_ok=True)
        path.unlink()


def daemon_unmanage(node):
    with locked():
        for info in summaries():
            if info['node'] != node:
                continue
            config = json.loads((ROOT / info['id'] / 'compose.runtime.json').read_text())
            for item in container_records(info['id']):
                policy = config['services'][item['service']].get('restart', 'no')
                run(['docker', 'update', '--restart=' + policy, item['id']], capture_output=True)
            (ROOT.parent / 'restart-paused' / (info['id'] + '.json')).unlink(missing_ok=True)
        (ROOT.parent / 'lifecycle.json').unlink(missing_ok=True)


def lifecycle_stop(identifier, node, timeout=180, force=1):
    with operation(identifier, 'shutdown'):
        try:
            action(identifier, node, 'shutdown', timeout)
        except ValueError as error:
            if not int(force) or not str(error).startswith('Shutdown timed out.'):
                raise
            action(identifier, node, 'force-stop')


def project_config(identifier, node, changes, digest=None):
    if not isinstance(changes, dict) or set(changes) - {'description', 'tags', 'protection', 'onboot', 'startup'}:
        raise ValueError('Invalid project configuration')
    if 'description' in changes and (not isinstance(changes['description'], str) or len(changes['description']) > 8192):
        raise ValueError('Notes cannot exceed 8192 characters')
    if 'tags' in changes:
        if not isinstance(changes['tags'], str):
            raise ValueError('Invalid tags')
        entries = sorted(set(tag.strip().lower() for tag in re.split(r'[;,]', changes['tags']) if tag.strip()))
        if len(entries) > 32 or any(not re.fullmatch(r'[a-z0-9_][a-z0-9_.+-]{0,63}', tag) for tag in entries):
            raise ValueError('Invalid tags')
        changes = dict(changes, tags=';'.join(entries))
    if 'onboot' in changes:
        if changes['onboot'] not in (0, 1):
            raise ValueError('Start at boot must be 0 or 1')
        changes = dict(changes, onboot=int(changes['onboot']))
    if 'startup' in changes:
        parsed = parse_startup(changes['startup'])
        changes = dict(changes, startup=','.join(f'{key}={parsed[key]}' for key in ('order', 'up', 'down') if key in parsed))
    if 'protection' in changes:
        if changes['protection'] not in (0, 1):
            raise ValueError('Protection must be 0 or 1')
        changes = dict(changes, protection=int(changes['protection']))
    with locked():
        info = detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        authorize_config_keys(changes)
        info.pop('deployment', None)
        path = ROOT / identifier / 'notes.txt'
        previous = path.read_text() if path.exists() else None
        try:
            if 'description' in changes:
                fd, temporary = tempfile.mkstemp(prefix='.notes-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w') as handle:
                        handle.write(changes['description'])
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            info.update({key: value for key, value in changes.items() if key != 'description'})
            if set(changes) - {'description'}:
                write_json(METADATA / (identifier + '.json'), info)
        except Exception:
            if 'description' in changes:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(previous)
                    os.chmod(path, 0o600)
            raise


def check_protection(info, message="can't remove DCT"):
    if info.get('protection'):
        raise ValueError(message + ' - protection mode enabled')


def project_protection(identifier, node, value=None, digest=None):
    with locked():
        info = detail(identifier, node)
        if value is not None:
            if str(value) not in ('0', '1'):
                raise ValueError('Protection must be 0 or 1')
            assert_configuration_digest(identifier, node, digest)
            info.pop('deployment', None)
            info['protection'] = int(value)
            write_json(METADATA / (identifier + '.json'), info)
            info = detail(identifier, node)
        return {'protection': int(bool(info.get('protection'))), 'digest': info['digest']}


def check_protected_layout(info, before, after):
    if not info.get('protection'):
        return
    def layout(config):
        return {name: {'image': service.get('image'), 'build': service.get('build'),
                       'replicas': service.get('deploy', {}).get('replicas', 1),
                       'volumes': service.get('volumes', []), 'storage_opt': service.get('storage_opt', {})}
                for name, service in config['services'].items()}
    if layout(before) != layout(after) or before.get('volumes', {}) != after.get('volumes', {}):
        check_protection(info, "can't update DCT services or disks")


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
                snapshot = management(info['id'], node, shared_stats=shared_stats)
                record_metrics(snapshot)
                public = json.loads((METADATA / (info['id'] + '.json')).read_text())
                public['metrics'] = {key: snapshot['metrics'][key] for key in ('cpu', 'cpus', 'mem', 'maxmem', 'uptime', 'time')}
                write_json(METADATA / (info['id'] + '.json'), public)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f'Metrics unavailable: {error}', file=sys.stderr)


def graceful_shutdown(identifier, timeout=60, service=None):
    timeout = int(timeout)
    if not 0 <= timeout <= 86400:
        raise ValueError('Invalid shutdown timeout')
    def selected_records():
        return [item for item in container_records(identifier) if service is None or item['service'] == service]

    records = selected_records()
    active_states = ('running', 'restarting', 'paused')
    for container in records:
        check_shutdown_cancellation(identifier)
        if container['state'] not in active_states:
            continue
        try:
            run(['docker', 'kill', '--signal', container.get('stop_signal') or 'SIGTERM', container['id']],
                capture_output=True, timeout=30)
        except subprocess.CalledProcessError:
            if any(item['id'] == container['id'] and item['state'] in active_states
                   for item in selected_records()):
                raise
    deadline = time.monotonic() + int(timeout)
    while any(item['state'] in active_states for item in selected_records()):
        check_shutdown_cancellation(identifier)
        if time.monotonic() >= deadline:
            raise ValueError('Shutdown timed out. DCT remains running; use Stop to force termination.')
        time.sleep(0.2)
    return [item['id'] for item in records]


def action(identifier, node, operation, timeout=60):
    with locked():
        info = detail(identifier, node)
        directory = ROOT / identifier
        commands = {'start': ['up', '-d'], 'stop': ['stop'], 'restart': ['restart'], 'delete': ['down']}
        if operation not in commands and operation not in ('shutdown', 'reboot', 'force-stop'):
            raise ValueError('Unsupported operation')
        if operation == 'delete':
            check_protection(info)
        if operation == 'delete' and any(item['state'] in ('running', 'restarting', 'paused')
                                          for item in container_records(identifier)):
            raise ValueError('DCT is running. Stop it before removal.')
        if operation == 'reboot' and not any(item['state'] in ('running', 'restarting', 'paused')
                                            for item in container_records(identifier)):
            raise ValueError('DCT is not running')
        if operation in ('start', 'restart', 'reboot'):
            ensure_address(info)
            rebind_runtime(directory, info['address'])
            info.pop('deployment', None)
            info.pop('network_resume', None)
            info['status'] = 'creating'
            write_json(METADATA / (identifier + '.json'), info)
        if operation == 'start':
            apply_pending_settings(identifier, node)
        if operation in ('shutdown', 'reboot'):
            ids = graceful_shutdown(identifier, timeout)
            if operation == 'reboot' and ids:
                apply_pending_settings(identifier, node)
                ids = [c['id'] for c in container_records(identifier)]
                run(['docker', 'start', *ids], timeout=1800)
        elif operation == 'force-stop':
            ids = [item['id'] for item in container_records(identifier)
                   if item['state'] in ('running', 'restarting', 'paused')]
            if ids:
                run(['docker', 'kill', *ids], capture_output=True, timeout=30)
        elif operation != 'start' or not restored_start(identifier):
            containers = container_records(identifier) if operation == 'start' else []
            # Live resource updates change the Compose hash, not container identity.
            # Start existing instances without asking Compose to reconcile that hash.
            if containers and len(containers) == info.get('expected'):
                run(['docker', 'start', *[c['id'] for c in containers]], timeout=1800)
            else:
                run(compose_base(directory, identifier) + commands[operation], timeout=1800)
        if operation in ('start', 'restart', 'reboot'):
            restore_restart_policy(identifier)
        if operation == 'delete':
            # Reserve the metadata until the owned alias is successfully released.
            remove_network(info)
            (METADATA / (identifier + '.json')).unlink()
            release_id(identifier)
            print(f'Project removed. Volumes and bind-mount data are retained at {directory}.')
        else:
            info.pop('deployment', None)
            if operation in ('stop', 'shutdown', 'force-stop'):
                info.pop('network_resume', None)
                info['status'] = 'stopped'
                write_json(METADATA / (identifier + '.json'), info)
            refresh(node)


def update(identifier, node, data, digest=None):
    with locked():
        existing = detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        if data.get('vmid') and data['vmid'] != existing.get('vmid'):
            raise ValueError('DCT ID cannot be changed during deployment edits')
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
        require_stopped(container_records(identifier))
        directory = ROOT / identifier
        if load_pending(directory):
            raise ValueError('Revert pending service settings before editing deployment')
        with tempfile.TemporaryDirectory(prefix='betterdocker-update-', dir=ROOT) as folder:
            staging = Path(folder)
            config = prepare(dict(data, address=existing.get('address') or '0.0.0.0'), identifier, staging, project_directory=directory)
            check_protected_layout(existing, json.loads((directory / 'compose.runtime.json').read_text()),
                                   json.loads((staging / 'compose.runtime.json').read_text()))
            for filename in ('compose.source.yaml', '.env', 'compose.runtime.json'):
                os.replace(staging / filename, directory / filename)
        (directory / 'snapshot-restored.json').unlink(missing_ok=True)
        write_json(directory / 'request.json', data)
        info = {key: value for key, value in existing.items() if key != 'deployment'}
        info['expected'] = sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())
        info['status'] = 'creating'
        write_json(METADATA / (identifier + '.json'), info)
        try:
            ensure_address(info)
            rebind_runtime(directory, info['address'])
            write_json(METADATA / (identifier + '.json'), info)
            run(compose_base(directory, identifier) + ['up', '--no-start', '--remove-orphans'], timeout=1800)
            refresh(node)
        except Exception:
            info['status'] = 'error'
            write_json(METADATA / (identifier + '.json'), info)
            raise


def restore(node):
    with locked():
        for path in ROOT.glob('*/storage.json'):
            storage_mount(path.parent.name)
        for info in summaries():
            if info['node'] == node:
                ensure_address(info, wait=False)


# Operation markers contain no deployment data and live outside project directories.
# The per-project flock rejects conflicting requests instead of queueing them.
def check_shutdown_cancellation(identifier):
    path = ROOT.parent / 'operations' / (project_id(identifier) + '.cancel.json')
    if not path.exists():
        return
    request = json.loads(path.read_text())
    process_start = (PROC / str(os.getpid()) / 'stat').read_text().split(') ', 1)[1].split()[19]
    if request.get('pid') == os.getpid() and request.get('process_start') == process_start:
        raise ValueError('Shutdown interrupted by Stop.')


def operation_state(identifier):
    path = ROOT.parent / 'operations' / (project_id(identifier) + '.json')
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if state.get('active'):
        pid = state.get('pid', 0)
        try:
            alive = pid > 0 and (PROC / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19] == state.get('process_start')
        except (OSError, IndexError):
            alive = False
        if not alive:
            state.update(active=False, failed=True, message='Operation interrupted. Open Task History.')
    return state


@contextlib.contextmanager
def operation(identifier, name, overrule=False, service=None):
    project_id(identifier)
    folder = ROOT.parent / 'operations'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / (identifier + '.lock')).open('w') as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not overrule or name != 'force-stop':
                raise ValueError('Another operation is running for this project') from None
            previous = operation_state(identifier)
            if not previous.get('active') or previous.get('action') != 'shutdown' or previous.get('service') != service:
                raise ValueError('Only a matching active Shutdown can be overruled') from None
            actor = os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam')
            owner = previous.get('user', '')
            privileged = os.environ.get('BETTERDOCKER_TASK_SYS_MODIFY', '1' if actor == 'root@pam' else '0') == '1'
            if not privileged and (not owner or actor.split('!', 1)[0] != owner.split('!', 1)[0]):
                raise ValueError('Permission denied: cannot overrule another user\'s Shutdown') from None
            token = {key: previous[key] for key in ('pid', 'process_start')}
            write_json(folder / (identifier + '.cancel.json'), token)
            deadline = time.monotonic() + 35
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('Shutdown did not release its lock; Stop was not performed') from None
                    time.sleep(0.2)
            # A competing request must not overrule a newer operation after handoff.
            latest = operation_state(identifier)
            if any(latest.get(key) != value for key, value in token.items()):
                raise ValueError('Operation changed before Stop acquired the lock')
        (folder / (identifier + '.cancel.json')).unlink(missing_ok=True)
        path = folder / (identifier + '.json')
        pid = os.getpid()
        state = {'active': True, 'action': name, 'pid': pid, 'started': int(time.time()),
                 'user': os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam'), 'service': service,
                 'process_start': (PROC / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19]}
        write_json(path, state)
        os.chmod(path, 0o600)
        try:
            yield
        except Exception:
            state.update(active=False, failed=True, message='Operation failed. Open Task History.')
            raise
        finally:
            state.update(active=False, finished=int(time.time()))
            write_json(path, state)


def preview(data, identifier=None):
    identifier = project_id(identifier) if identifier else uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='betterdocker-preview-') as folder:
        config = prepare(dict(validate(data), address=data.get('address') or '0.0.0.0'), identifier,
                         Path(folder), project_directory=ROOT / identifier)
        # Deliberately return only a plan, never expanded env values or secrets.
        return [{'service': name, 'image': service.get('image', ''),
                 'replicas': service.get('deploy', {}).get('replicas', 1),
                 'ports': service.get('ports', []),
                 'mounts': [{'type': m.get('type'), 'target': m.get('target'),
                             'read_only': m.get('read_only', False)} for m in service.get('volumes', [])]}
                for name, service in config['services'].items()]


def project_tags(identifier, node, tags=None):
    with locked():
        info = detail(identifier, node)
        info.pop('deployment', None)
        if tags is not None:
            entries = sorted(set(tag.strip().lower() for tag in re.split(r'[;,]', tags) if tag.strip()))
            if len(entries) > 32 or any(not re.fullmatch(r'[a-z0-9_][a-z0-9_.+-]{0,63}', tag) for tag in entries):
                raise ValueError('Invalid tags')
            info['tags'] = ';'.join(entries)
            write_json(METADATA / (identifier + '.json'), info)
        return {'tags': info.get('tags', '')}


def service_action(identifier, node, service, verb, timeout=60):
    with locked():
        info = detail(identifier, node)
        directory = ROOT / identifier
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if service not in config['services'] or verb not in ('start', 'stop', 'restart', 'shutdown', 'reboot', 'force-stop'):
            raise ValueError('Invalid service operation')
        if verb in ('start', 'restart', 'reboot'):
            ensure_address(info)
            rebind_runtime(directory, info['address'])
        if verb == 'start':
            apply_pending_settings(identifier, node, service)
        if verb in ('shutdown', 'reboot'):
            ids = graceful_shutdown(identifier, timeout, service)
            if verb == 'reboot' and ids:
                apply_pending_settings(identifier, node, service)
                ids = [c['id'] for c in container_records(identifier) if c['service'] == service]
                run(['docker', 'start', *ids], timeout=1800)
        elif verb == 'force-stop':
            ids = [item['id'] for item in container_records(identifier)
                   if item['service'] == service and item['state'] in ('running', 'restarting', 'paused')]
            if ids:
                run(['docker', 'kill', *ids], capture_output=True, timeout=30)
        else:
            command = {'start': ['up', '-d', '--no-deps'], 'stop': ['stop'], 'restart': ['restart']}[verb]
            if verb != 'start' or not restored_start(identifier, service):
                containers = [c for c in container_records(identifier) if c['service'] == service] if verb == 'start' else []
                expected = config['services'][service].get('deploy', {}).get('replicas', 1)
                if containers and len(containers) == expected:
                    run(['docker', 'start', *[c['id'] for c in containers]], timeout=1800)
                else:
                    run(compose_base(directory, identifier) + command + [service], timeout=1800)
        if verb in ('start', 'restart', 'reboot'):
            restore_restart_policy(identifier, service)
        refresh(node)


def reset_settings(identifier, node, service, digest=None):
    with locked():
        info = detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        directory = ROOT / identifier
        settings = load_settings(directory)
        if service not in settings:
            if service in load_pending(directory):
                require_stopped([c for c in container_records(identifier) if c['service'] == service])
                pending = load_pending(directory)
                pending.pop(service, None)
                save_pending(directory, pending)
            return
        original = (directory / 'compose.runtime.json').read_text()
        previous = dict(settings)
        settings.pop(service)
        require_stopped([c for c in container_records(identifier) if c['service'] == service])
        marker = directory / 'snapshot-restored.json'
        if marker.exists():
            restored = json.loads(marker.read_text())
            restored['containers'] = [row['id'] for row in container_records(identifier)]
            write_json(marker, restored)
        write_json(directory / 'settings.json', settings)
        os.chmod(directory / 'settings.json', 0o600)
        try:
            with tempfile.TemporaryDirectory(prefix='betterdocker-reset-', dir=ROOT) as folder:
                staging = Path(folder)
                prepare(dict(info['deployment'], address=info.get('address') or '0.0.0.0'), identifier, staging, directory)
                check_protected_layout(info, json.loads(original), json.loads((staging / 'compose.runtime.json').read_text()))
                os.replace(staging / 'compose.runtime.json', directory / 'compose.runtime.json')
            run(compose_base(directory, identifier) + ['up', '--no-start', '--no-deps', service], timeout=1800)
            marker = directory / 'snapshot-restored.json'
            if marker.exists():
                restored = json.loads(marker.read_text())
                restored['containers'] = [row['id'] for row in container_records(identifier)]
                write_json(marker, restored)
        except Exception:
            write_json(directory / 'settings.json', previous)
            (directory / 'compose.runtime.json').write_text(original)
            raise
        pending = load_pending(directory)
        pending.pop(service, None)
        save_pending(directory, pending)
        refresh(node)


def project_logs(identifier, node, service='', container='', tail=200):
    detail(identifier, node)
    tail = int(tail)
    if not 1 <= tail <= 5000:
        raise ValueError('Tail must be between 1 and 5000')
    if container:
        if not any(c['id'] == container for c in container_records(identifier)):
            raise ValueError('Container does not belong to this project')
        result = run(['docker', 'logs', '--timestamps', '--tail', str(tail), container], capture_output=True)
        return '\n'.join(sorted((result.stdout + result.stderr).splitlines())) + '\n'
    config = json.loads((ROOT / identifier / 'compose.runtime.json').read_text())
    if service and service not in config['services']:
        raise ValueError('Unknown service')
    return capture(compose_base(ROOT / identifier, identifier) + ['logs', '--no-color', '--timestamps', '--tail', str(tail)] + ([service] if service else []))


def diagnostics(node):
    if node != current_node():
        raise ValueError('Wrong node')
    def probe(command):
        try:
            return capture(command).strip()
        except (OSError, subprocess.SubprocessError):
            return 'Unavailable'
    docker_root = probe(['docker', 'info', '--format', '{{.DockerRootDir}}'])
    disk = shutil.disk_usage(docker_root) if Path(docker_root).is_dir() else None
    projects = [item for item in summaries() if item['node'] == node]
    targets = ['/usr/share/pve-manager/js/betterdocker.js', '/usr/share/pve-manager/css/betterdocker.css',
               '/usr/share/perl5/PVE/API2/BetterDocker.pm', '/usr/local/lib/betterdocker/provision.py']
    markers = ['/usr/share/pve-manager/index.html.tpl', '/usr/share/perl5/PVE/API2/Nodes.pm',
               '/usr/share/perl5/PVE/API2/Cluster.pm', '/usr/share/perl5/PVE/Cluster.pm']
    integration = all(Path(p).is_file() for p in targets) and all(Path(p).is_file() and 'BEGIN BETTERDOCKER' in Path(p).read_text() for p in markers)
    return {'addon': '19', 'pve': probe(['pveversion']), 'engine': probe(['docker', '--version']),
            'compose': probe(['docker', 'compose', 'version', '--short']),
            'timer': probe(['systemctl', 'is-active', 'betterdocker-refresh.timer']),
            'addresses': probe(['systemctl', 'is-active', 'betterdocker-addresses.service']),
            'integration': 'OK' if integration else 'Repair required',
            'integration_check': probe(['systemctl', 'show', '--property=Result', '--value', 'betterdocker-integration.service']), 'projects': len(projects),
            'storage': {'path': docker_root, 'total': disk.total, 'used': disk.used, 'free': disk.free} if disk else {},
            'dhcp': [{'vmid': p.get('vmid'), 'state': probe(['systemctl', 'is-active', 'betterdocker-dhcp@' + p['id'] + '.service'])}
                     for p in projects if p.get('ipv4mode') == 'dhcp']}


# DCT snapshots are private, complete copies independent of the Docker image cache.
def snapshot_name(value):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', value) or value in ('current', 'vzdump'):
        raise ValueError('Snapshot name must start with a letter and contain at most 40 letters, digits, underscores or hyphens')
    return value


def snapshot_root(identifier):
    return ROOT.parent / 'snapshots' / project_id(identifier)


def snapshot_tree_state(identifier, names=None):
    path = ROOT / project_id(identifier) / 'snapshot-tree.json'
    state = json.loads(path.read_text()) if path.exists() else {'parents': {}, 'descriptions': {}, 'current': ''}
    if names is None:
        names = {path.parent.name for path in snapshot_root(identifier).glob('*/manifest.json') if not path.parent.name.startswith('.')}
    if names is not None:
        state['parents'] = {name: parent if parent in names and parent != name else ''
                            for name, parent in state.get('parents', {}).items() if name in names}
        state['descriptions'] = {name: value for name, value in state.get('descriptions', {}).items() if name in names}
        if not path.exists():
            restored = ROOT / identifier / 'snapshot-restored.json'
            candidates = []
            for name in names:
                manifest = snapshot_root(identifier) / name / 'manifest.json'
                if manifest.exists():
                    data = json.loads(manifest.read_text())
                    if not data.get('recovery'):
                        candidates.append((data['created'], name))
            latest = max(candidates, default=(0, ''))
            state['current'] = latest[1]
            if restored.exists() and restored.stat().st_mtime > latest[0]:
                state['current'] = json.loads(restored.read_text()).get('name', '')
        if state.get('current') not in names:
            state['current'] = ''
        # An invalid legacy sidecar must never make the tree recursive.
        for name in state['parents']:
            seen, parent = {name}, state['parents'][name]
            while parent:
                if parent in seen:
                    state['parents'][name] = ''
                    break
                seen.add(parent)
                parent = state['parents'].get(parent, '')
    return state


def snapshot_tree_write(identifier, state):
    write_json(ROOT / identifier / 'snapshot-tree.json', state)


def snapshot_list(identifier, node):
    import hashlib
    info = detail(identifier, node)
    rows = []
    for path in snapshot_root(identifier).glob('*/manifest.json'):
        try:
            data = json.loads(path.read_text())
            if data.get('version') == 1 and data.get('id') == identifier and data.get('name') == path.parent.name:
                rows.append({key: data[key] for key in ('name', 'created', 'description', 'size', 'recovery')})
        except (OSError, ValueError, KeyError):
            continue
    rows.sort(key=lambda row: (row['created'], row['name']))
    state = snapshot_tree_state(identifier, {row['name'] for row in rows})
    for row in rows:
        row.update(parent=state['parents'].get(row['name'], ''), snaptime=row['created'],
                   description=state['descriptions'].get(row['name'], row['description']))
    current = {'name': 'current', 'parent': state.get('current', ''), 'description': '',
               'running': info.get('status') in ('running', 'partial')}
    current['digest'] = hashlib.sha256(json.dumps([rows, current], sort_keys=True).encode()).hexdigest()
    return rows + [current]


def snapshot_config(identifier, node, name):
    detail(identifier, node)
    folder = snapshot_root(identifier) / snapshot_name(name)
    if folder.is_symlink():
        raise ValueError('Invalid snapshot directory')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('id') != identifier or manifest.get('name') != name:
        raise ValueError('Invalid snapshot manifest')
    state = snapshot_tree_state(identifier)
    metadata = json.loads((folder / 'configuration' / 'metadata.json').read_text())
    config = json.loads((folder / 'configuration' / 'compose.runtime.json').read_text())
    # Native snapshot details are an inventory, not a way to read deployment secrets.
    result = {'description': state.get('descriptions', {}).get(name, manifest['description']),
              'snaptime': manifest['created'], 'hostname': metadata.get('name', ''),
              'net0': 'bridge=' + metadata.get('bridge', '') + ',ip=' + metadata.get('address', ''),
              'protection': int(bool(metadata.get('protection')))}
    for service, settings in sorted(config['services'].items()):
        for key in ('image', 'cpus', 'cpu_shares', 'mem_limit', 'memswap_limit', 'pids_limit', 'restart'):
            if key in settings:
                result[service + '.' + key] = str(settings[key])
        result[service + '.replicas'] = str(settings.get('deploy', {}).get('replicas', 1))
        limits = settings.get('deploy', {}).get('resources', {}).get('limits', {})
        for key in ('cpus', 'memory', 'pids'):
            if key in limits:
                result[service + '.limit.' + key] = str(limits[key])
        for key in ('dns', 'dns_search', 'storage_opt'):
            if settings.get(key):
                result[service + '.' + key] = json.dumps(settings[key], sort_keys=True)
        for index, mount in enumerate(settings.get('volumes', [])):
            result[service + '.mount' + str(index)] = str(mount.get('source', '')) + ',mp=' + str(mount.get('target', '')) + (',ro=1' if mount.get('read_only') else '')
        for index, port in enumerate(settings.get('ports', [])):
            result[service + '.port' + str(index)] = str(port.get('host_ip', '')) + ':' + str(port.get('published', '')) + ':' + str(port.get('target', '')) + '/' + str(port.get('protocol', 'tcp'))
    storage = folder / 'configuration' / 'storage.json'
    if storage.exists():
        result['data_disk'] = str(json.loads(storage.read_text())['size_mib']) + ' MiB'
    return result


def snapshot_edit(identifier, node, name, description):
    snapshot_name(name)
    if len(description) > 4096:
        raise ValueError('Snapshot description cannot exceed 4096 characters')
    with locked():
        snapshot_config(identifier, node, name)
        state = snapshot_tree_state(identifier)
        state.setdefault('descriptions', {})[name] = description
        snapshot_tree_write(identifier, state)


def snapshot_containers(identifier, allow_running=False, quiesced=False):
    records = container_records(identifier)
    allowed = {'created', 'exited'} | ({'running', 'paused'} if allow_running else set()) | ({'paused'} if quiesced else set())
    if any(row['state'] not in allowed for row in records):
        raise ValueError('DCT is changing state; wait until its services are stable')
    return json.loads(capture(['docker', 'inspect', *[row['id'] for row in records]])) if records else []


@contextlib.contextmanager
def snapshot_quiesce(identifier):
    records = container_records(identifier)
    if any(row['state'] not in ('running', 'created', 'exited') for row in records):
        raise ValueError('DCT is changing state or already paused; wait before taking a snapshot')
    paused = []
    def resume():
        failures = []
        for container in list(paused):
            try:
                run(['docker', 'unpause', container], capture_output=True)
                paused.remove(container)
            except (OSError, subprocess.SubprocessError):
                failures.append(container)
        if failures:
            raise ValueError('Could not resume all DCT services after snapshot; check Task History')
    try:
        for row in records:
            if row['state'] == 'running':
                run(['docker', 'pause', row['id']], capture_output=True)
                paused.append(row['id'])
        if paused:
            os.sync()
        yield resume
    finally:
        resume()


def snapshot_mounts(identifier, containers, allow_missing=False):
    """Resolve actual Docker mounts, never assume that a Compose volume is private."""
    folder = ROOT / identifier
    owned = {row['Id'] for row in containers}
    sources = {}
    for row in containers:
        for mount in row.get('Mounts', []):
            if not mount.get('RW', True) or mount['Type'] == 'tmpfs':
                continue
            source = Path(mount['Source'])
            if mount['Type'] == 'bind':
                if source.is_symlink() or not source.resolve().is_relative_to(folder.resolve()) or source.resolve() == folder.resolve():
                    raise ValueError('Snapshots require project-owned writable mounts; external host paths are not supported')
                if source.name in ('storage.ext4', 'request.json', 'compose.runtime.json', 'compose.source.yaml', '.env', 'settings.json', 'pending.json', 'storage.json'):
                    raise ValueError('Deployment files cannot also be writable application mounts')
            elif mount['Type'] == 'volume':
                volume = json.loads(capture(['docker', 'volume', 'inspect', mount['Name']]))[0]
                if volume.get('Driver') != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                    raise ValueError('Snapshots require local volumes owned by this DCT')
                users = set(capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + mount['Name']]).split())
                if users - owned:
                    raise ValueError('A writable volume is shared with another project')
                if source.resolve() != Path(volume['Mountpoint']).resolve():
                    raise ValueError('Volume mount does not match Docker metadata')
            else:
                raise ValueError('Unsupported writable snapshot mount')
            if not source.is_dir() and not (allow_missing and not source.exists()):
                raise ValueError('Snapshots support writable directory mounts only')
            sources[str(source)] = {'source': str(source), 'type': mount['Type'], 'volume': mount.get('Name', '')}
    paths = sorted(sources, key=lambda value: len(Path(value).parts))
    # Copy overlapping bind mounts only once; retain the exact volume ownership checks above.
    return [sources[value] for value in paths if not any(Path(value).is_relative_to(Path(parent)) for parent in paths if parent != value)]


def snapshot_digest(folder):
    """Inventory hashes include symlink targets and empty directories, without following links."""
    import hashlib
    result = {}
    for base, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            key = str(path.relative_to(folder))
            if key == 'checksums.json':
                continue
            if path.is_symlink():
                result[key] = 'link:' + os.readlink(path)
            elif path.is_dir():
                result[key] = 'directory'
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                result[key] = digest.hexdigest()
            else:
                raise ValueError('Snapshot contains an unsupported special file')
    return result


def snapshot_check(identifier, name):
    folder = snapshot_root(identifier) / snapshot_name(name)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Snapshot does not exist')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest.get('id') != identifier or manifest.get('name') != name:
        raise ValueError('Invalid snapshot manifest')
    if snapshot_digest(folder) != json.loads((folder / 'checksums.json').read_text()):
        raise ValueError('Snapshot integrity check failed; nothing was restored')
    return folder, manifest


def snapshot_capture(identifier, node, name, description='', recovery=False, quiesced=False, resume=None):
    snapshot_name(name)
    if len(description) > 4096:
        raise ValueError('Snapshot description cannot exceed 4096 characters')
    info = detail(identifier, node)
    tree_state = snapshot_tree_state(identifier)
    containers = snapshot_containers(identifier, quiesced=quiesced)
    if not containers:
        raise ValueError('DCT has no containers to snapshot')
    config = json.loads((ROOT / identifier / 'compose.runtime.json').read_text())
    expected = sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())
    counts = {name: sum((row['Config'].get('Labels') or {}).get('com.docker.compose.service') == name for row in containers) for name in config['services']}
    if len(containers) != expected or any(counts[name] != service.get('deploy', {}).get('replicas', 1) for name, service in config['services'].items()):
        raise ValueError('DCT instances are incomplete; deploy all services before taking a snapshot')
    storage_mount(identifier)
    mounts = snapshot_mounts(identifier, containers)
    if storage_config(identifier):
        point = str(ROOT / identifier / 'storage-data')
        mounts = [entry for entry in mounts if not Path(entry['source']).is_relative_to(Path(point))]
        mounts.append({'source': point, 'type': 'bind', 'volume': ''})
    # Exclude nested filesystems; an application could otherwise cross ownership boundaries.
    mounted = json.loads(capture(['findmnt', '-J', '-l', '-o', 'TARGET']))['filesystems']
    for entry in mounts:
        path = Path(entry['source']).resolve()
        if any(Path(item['target']).is_relative_to(path) and Path(item['target']) != path for item in mounted):
            raise ValueError('Nested filesystems in writable mounts are not supported by snapshots')
    root = snapshot_root(identifier)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root.parent, 0o700)
    os.chmod(root, 0o700)
    final = root / name
    if final.exists():
        raise ValueError('Snapshot name already exists')
    temporary = Path(tempfile.mkdtemp(prefix='.creating-', dir=root))
    tags = []
    try:
        files = temporary / 'configuration'
        files.mkdir(mode=0o700)
        for filename in ('request.json', 'compose.source.yaml', '.env', 'compose.runtime.json', 'settings.json', 'pending.json', 'storage.json', 'notes.txt'):
            source = ROOT / identifier / filename
            if source.exists():
                shutil.copy2(source, files / filename)
        public = {key: value for key, value in info.items() if key != 'deployment'}
        write_json(files / 'metadata.json', public)
        data = temporary / 'data'
        data.mkdir(mode=0o700)
        for index, entry in enumerate(mounts):
            entry['copy'] = 'data/' + str(index)
            run(['cp', '-a', '--reflink=auto', '--', entry['source'], str(temporary / entry['copy'])], timeout=86400)
        for index, container in enumerate(containers):
            tag = 'betterdocker-snapshot/' + identifier + ':' + uuid.uuid4().hex
            run(['docker', 'commit', '--pause=false', container['Id'], tag], timeout=86400, capture_output=True)
            tags.append(tag)
            container['SnapshotImage'] = capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag]).strip()
            container['SnapshotTag'] = tag
        if resume:
            resume()
        run(['docker', 'image', 'save', '-o', str(temporary / 'images.tar'), *tags], timeout=86400)
        os.chmod(temporary / 'images.tar', 0o600)
        network_names = sorted({network for container in containers for network in container.get('NetworkSettings', {}).get('Networks', {})})
        networks = json.loads(capture(['docker', 'network', 'inspect', *network_names])) if network_names else []
        write_json(temporary / 'networks.json', networks)
        write_json(temporary / 'containers.json', containers)
        size = sum(path.stat().st_size for path in temporary.rglob('*') if path.is_file() and not path.is_symlink())
        write_json(temporary / 'manifest.json', {'version': 1, 'id': identifier, 'name': name,
                   'created': int(time.time()), 'description': description, 'recovery': recovery,
                   'mounts': mounts, 'size': size})
        write_json(temporary / 'checksums.json', snapshot_digest(temporary))
        os.replace(temporary, final)
        state = tree_state
        state.setdefault('parents', {})[name] = state.get('current', '')
        if not recovery:
            state['current'] = name
        snapshot_tree_write(identifier, state)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        for tag in tags:
            run(['docker', 'image', 'rm', tag], check=False, capture_output=True)
    print('Snapshot created: ' + name)
    return name


def docker_engine(method, path, body):
    """Only the local Docker socket is used; never log Config/Env or daemon responses."""
    import http.client
    import socket
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(1800)
            self.sock.connect('/var/run/docker.sock')
    connection = Connection('localhost', timeout=1800)
    try:
        connection.request(method, path, body=json.dumps(body), headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        payload = response.read()
        if response.status not in (200, 201, 204):
            raise ValueError('Docker snapshot restore failed (HTTP ' + str(response.status) + '); see Task History')
        return json.loads(payload) if payload else {}
    finally:
        connection.close()


def snapshot_apply(identifier, node, name, check_only=False):
    from urllib.parse import quote
    folder, manifest = snapshot_check(identifier, name)
    current = detail(identifier, node)
    existing = snapshot_containers(identifier, allow_running=check_only)
    # Validate both source and target ownership before touching containers or application files.
    snapshot_mounts(identifier, existing, allow_missing=True)
    saved = json.loads((folder / 'containers.json').read_text())
    configs = folder / 'configuration'
    before_storage = storage_config(identifier)
    saved_storage = json.loads((configs / 'storage.json').read_text()) if (configs / 'storage.json').exists() else {}
    mounted_targets = json.loads(capture(['findmnt', '-J', '-l', '-o', 'TARGET']))['filesystems']
    for entry in manifest['mounts']:
        source = Path(entry['source'])
        if any(Path(item['target']).is_relative_to(source.resolve()) and Path(item['target']) != source.resolve() for item in mounted_targets):
            raise ValueError('Nested filesystems in snapshot targets are not supported')
        if entry['type'] == 'bind' and (source.is_symlink() or not source.resolve().is_relative_to((ROOT / identifier).resolve())):
            raise ValueError('Snapshot mount ownership changed')
        if entry['type'] == 'volume':
            volume = json.loads(capture(['docker', 'volume', 'inspect', entry['volume']]))[0]
            if volume.get('Driver') != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                raise ValueError('Snapshot volume ownership changed')
            users = set(capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + entry['volume']]).split())
            if users - {row['Id'] for row in existing}:
                raise ValueError('Snapshot volume is now shared with another project')
    saved_networks = json.loads((folder / 'networks.json').read_text()) if (folder / 'networks.json').exists() else []
    existing_networks = set(capture(['docker', 'network', 'ls', '--format', '{{.Name}}']).splitlines())
    for network in saved_networks:
        if network['Name'] not in existing_networks:
            if (network.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                raise ValueError('An external snapshot network is missing')
            body = {key: network[key] for key in ('Name', 'Driver', 'Internal', 'Attachable', 'EnableIPv6', 'IPAM', 'Options', 'Labels') if key in network}
            if not check_only:
                docker_engine('POST', '/networks/create', body)
    if check_only:
        return
    run(['docker', 'image', 'load', '-i', str(folder / 'images.tar')], timeout=86400, capture_output=True)
    if existing:
        run(['docker', 'rm', *[row['Id'] for row in existing]], timeout=1800, capture_output=True)
    project = ROOT / identifier
    replaced_disk = None
    if before_storage and (not saved_storage or before_storage['size_mib'] != saved_storage['size_mib']):
        storage_mount(identifier)  # verifies that the loop device is owned by this DCT
        unit = capture(['systemd-escape', '--path', '--suffix=mount', str(project / 'storage-data')]).strip()
        # Remove only this DCT's dependency before unmounting; never stop the Docker daemon.
        run(['systemctl', 'disable', unit], check=False, capture_output=True)
        run(['systemctl', 'daemon-reload'], capture_output=True)
        # Wait for systemd's unmount job before mounting a replacement at the same path.
        run(['systemctl', 'stop', unit], capture_output=True)
        if os.path.ismount(project / 'storage-data'):
            run(['umount', str(project / 'storage-data')], capture_output=True)
        replaced_disk = project / ('storage-rollback-' + uuid.uuid4().hex + '.ext4')
        (project / 'storage.ext4').rename(replaced_disk)
        (project / 'storage.json').unlink()
        before_storage = {}
        if not saved_storage:
            (project / 'storage-data').rmdir()
            (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
            run(['systemctl', 'daemon-reload'], capture_output=True)
    if saved_storage:
        if not before_storage:
            image = project / 'storage.ext4'
            if image.exists():
                raise ValueError('Unmanaged data disk image exists')
            with image.open('xb') as handle:
                os.chmod(image, 0o600)
                handle.truncate(saved_storage['size_mib'] * 1048576)
            run(['mkfs.ext4', '-q', '-m', '0', str(image)])
        write_json(project / 'storage.json', saved_storage)
        unit = capture(['systemd-escape', '--path', '--suffix=mount', str(project / 'storage-data')]).strip()
        if not before_storage:
            unit_path = Path('/etc/systemd/system') / unit
            unit_path.write_text('[Unit]\nDescription=BetterDocker data disk\nBefore=docker.service\n\n[Mount]\nWhat=' + str(project / 'storage.ext4') + '\nWhere=' + str(project / 'storage-data') + '\nType=ext4\nOptions=loop,nodev,nosuid\n\n[Install]\nRequiredBy=docker.service\n')
            run(['systemctl', 'daemon-reload'], capture_output=True)
            run(['systemctl', 'enable', unit], capture_output=True)
            (project / 'storage-data').mkdir(mode=0o700, exist_ok=True)
            run(['systemctl', 'start', unit], capture_output=True)
        storage_mount(identifier)
    for entry in manifest['mounts']:
        target = Path(entry['source'])
        target.mkdir(parents=True, exist_ok=True)
        for child in target.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        run(['cp', '-a', '--', str(folder / entry['copy']) + '/.', str(target)], timeout=86400)
    for filename in ('request.json', 'compose.source.yaml', '.env', 'compose.runtime.json', 'settings.json', 'pending.json', 'notes.txt'):
        source, target = configs / filename, project / filename
        if source.exists():
            shutil.copy2(source, target)
        else:
            target.unlink(missing_ok=True)
    metadata = json.loads((configs / 'metadata.json').read_text())
    # Identity and the current DHCP lease belong to the existing DCT, not to a new guest.
    metadata.update({key: current[key] for key in ('id', 'vmid', 'node', 'address') if key in current})
    metadata.update(status='stopped')
    metadata.pop('network_resume', None)
    write_json(METADATA / (identifier + '.json'), metadata)
    rebind_runtime(project, metadata['address'])
    created = []
    for container in saved:
        body = dict(container['Config'])
        body['Image'] = container['SnapshotImage']
        body['HostConfig'] = dict(container['HostConfig'])
        if saved_storage:
            service_name = (container['Config'].get('Labels') or {}).get('com.docker.compose.service')
            runtime_config = json.loads((project / 'compose.runtime.json').read_text())
            configured = {mount['target']: mount for mount in runtime_config['services'][service_name].get('volumes', [])}
            # Data migration can be pending while stopped: use the saved runtime disk mapping.
            binds = body['HostConfig'].get('Binds') or []
            for destination, mount in configured.items():
                if mount.get('type') != 'bind' or mount.get('source') not in saved_storage['mapping'].values():
                    continue
                binds = [value for value in binds if value.split(':')[1] != destination]
                binds.append(mount['source'] + ':' + destination + (':ro' if mount.get('read_only') else ':rw'))
                body['HostConfig']['Mounts'] = [item for item in body['HostConfig'].get('Mounts') or [] if item.get('Target') != destination]
            body['HostConfig']['Binds'] = binds
        # Keep ephemeral Docker IDs/IP assignments out of create requests.
        networks = {}
        for network, endpoint in container.get('NetworkSettings', {}).get('Networks', {}).items():
            networks[network] = {key: endpoint[key] for key in ('Aliases', 'IPAMConfig', 'Links', 'DriverOpts') if endpoint.get(key)}
        body['NetworkingConfig'] = {'EndpointsConfig': networks}
        for bindings in (body['HostConfig'].get('PortBindings') or {}).values():
            for binding in bindings or []:
                if binding.get('HostIp') == current.get('address') or binding.get('HostIp') == json.loads((configs / 'metadata.json').read_text()).get('address'):
                    binding['HostIp'] = metadata['address']
        result = docker_engine('POST', '/containers/create?name=' + quote(container['Name'].lstrip('/'), safe=''), body)
        created.append(result['Id'])
    for container in saved:
        if container.get('SnapshotTag'):
            run(['docker', 'image', 'rm', container['SnapshotTag']], check=False, capture_output=True)
    write_json(project / 'snapshot-restored.json', {'name': name, 'containers': created, 'address': metadata['address']})
    state = snapshot_tree_state(identifier)
    state['current'] = name
    snapshot_tree_write(identifier, state)
    refresh(node)
    if replaced_disk:
        replaced_disk.unlink()


def project_snapshot(identifier, node, verb, name, description=''):
    snapshot_name(name)
    with locked():
        detail(identifier, node)
        if verb == 'create':
            with snapshot_quiesce(identifier) as resume:
                snapshot_capture(identifier, node, name, description, quiesced=True, resume=resume)
        elif verb == 'delete':
            folder = snapshot_root(identifier) / name
            if folder.is_symlink() or not (folder / 'manifest.json').exists():
                raise ValueError('Snapshot does not exist')
            state = snapshot_tree_state(identifier)
            parent = state.get('parents', {}).get(name, '')
            state['parents'] = {child: parent if ancestor == name else ancestor for child, ancestor in state.get('parents', {}).items() if child != name}
            state.setdefault('descriptions', {}).pop(name, None)
            if state.get('current') == name:
                state['current'] = parent
            shutil.rmtree(folder)
            snapshot_tree_write(identifier, state)
            print('Snapshot removed: ' + name)
        elif verb == 'restore':
            # LXC validates the target, stops the guest, then rolls back without another snapshot.
            snapshot_apply(identifier, node, name, check_only=True)
            records = container_records(identifier)
            for row in records:
                if row['state'] == 'paused':
                    run(['docker', 'unpause', row['id']], capture_output=True)
            active = [row['id'] for row in records if row['state'] in ('running', 'paused')]
            if active:
                run(['docker', 'stop', '--time', '0', *active], timeout=1800, capture_output=True)
            snapshot_apply(identifier, node, name)
            print('Snapshot restored; DCT remains stopped.')
        else:
            raise ValueError('Invalid snapshot operation')


def restored_start(identifier, service=None):
    marker = ROOT / identifier / 'snapshot-restored.json'
    if not marker.exists():
        return False
    restored = json.loads(marker.read_text())
    runtime = json.loads((ROOT / identifier / 'compose.runtime.json').read_text())
    addresses = {port.get('host_ip') for service_config in runtime['services'].values() for port in service_config.get('ports', [])}
    if addresses and addresses != {restored.get('address')}:
        raise ValueError('DCT address changed after rollback; restore a snapshot again to rebind preserved filesystems')
    saved = set(restored['containers'])
    records = container_records(identifier)
    if {row['id'] for row in records} != saved:
        raise ValueError('Restored DCT instances changed; redeploy before starting')
    ids = [row['id'] for row in records if not service or row['service'] == service]
    if ids:
        run(['docker', 'start', *ids], timeout=1800)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['daemon-unmanage', 'lifecycle', 'daemon-prepare', 'daemon-resume', 'lifecycle-stop', 'validate', 'create', 'detail', 'config', 'access-state', 'protection', 'check-digest', 'action', 'logs', 'restore', 'refresh', 'update', 'dhcp-client', 'dhcp-release', 'management', 'settings', 'pending', 'revert-settings', 'console-check', 'console', 'new-id', 'notes', 'rrddata', 'reserve-id', 'release-id', 'preview', 'tags', 'service-action', 'reset-settings', 'diagnostics', 'storage-limit', 'snapshots', 'snapshot', 'snapshot-config', 'snapshot-edit'])
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    values = args.arguments
    if args.operation == 'reserve-id':
        print(reserve_id(values[0], values[1], values[2] if len(values) > 2 else None))
    elif args.operation == 'release-id':
        if not (METADATA / (project_id(values[0]) + '.json')).exists():
            release_id(values[0])
    elif args.operation == 'new-id':
        print(uuid.uuid4().hex)
    elif args.operation in ('validate', 'create'):
        data = validate(json.loads(Path(values[0]).read_text()))
        if args.operation == 'create':
            with operation(values[2], 'create'):
                create(data, values[1], values[2])
    elif args.operation == 'update':
        with operation(values[0], 'update'):
            update(values[0], values[1], validate(json.loads(Path(values[2]).read_text())), values[3] if len(values) > 3 else None)
    elif args.operation in ('dhcp-client', 'dhcp-release'):
        dhcp_client(values[0], release=args.operation == 'dhcp-release')
    elif args.operation == 'notes':
        print(json.dumps(notes(values[0], values[1], Path(values[2]).read_text() if len(values) > 2 else None)))
    elif args.operation == 'rrddata':
        print(json.dumps(rrddata(*values)))
    elif args.operation == 'management':
        print(json.dumps(management(*values)))
    elif args.operation == 'pending':
        print(json.dumps(pending_settings(*values)))
    elif args.operation == 'revert-settings':
        with operation(values[0], 'settings'):
            revert_settings(*values)
    elif args.operation == 'settings':
        with operation(values[0], 'settings'):
            service_settings(values[0], values[1], values[2], json.loads(Path(values[3]).read_text()), values[4] if len(values) > 4 else None)
    elif args.operation in ('console-check', 'console'):
        command = console_target(*values)
        if args.operation == 'console':
            os.execvp(command[0], command)
    elif args.operation == 'detail':
        print(json.dumps(detail(*values)))
    elif args.operation == 'lifecycle':
        print(json.dumps(lifecycle_config(*values)))
    elif args.operation == 'daemon-unmanage':
        daemon_unmanage(values[0] if values else current_node())
    elif args.operation == 'daemon-prepare':
        daemon_prepare(values[0] if values else current_node())
    elif args.operation == 'daemon-resume':
        daemon_resume(values[0] if values else current_node())
    elif args.operation == 'lifecycle-stop':
        lifecycle_stop(*values)
    elif args.operation == 'config':
        project_config(values[0], values[1], json.loads(Path(values[2]).read_text()), values[3] if len(values) > 3 else None)
    elif args.operation == 'access-state':
        print(json.dumps(access_state(*values)))
    elif args.operation == 'protection':
        print(json.dumps(project_protection(*values)))
    elif args.operation == 'check-digest':
        assert_configuration_digest(*values)
    elif args.operation == 'action':
        overrule = len(values) > 4 and values[4] == '1'
        if overrule and values[2] != 'force-stop':
            raise ValueError('Overrule is only available for Stop')
        with operation(values[0], values[2], overrule=overrule):
            action(*values[:4])
    elif args.operation == 'logs':
        print(project_logs(*values), end='')
    elif args.operation == 'preview':
        print(json.dumps(preview(json.loads(Path(values[0]).read_text()), values[1] if len(values) > 1 else None)))
    elif args.operation == 'tags':
        print(json.dumps(project_tags(*values)))
    elif args.operation == 'service-action':
        overrule = len(values) > 5 and values[5] == '1'
        if overrule and values[3] != 'force-stop':
            raise ValueError('Overrule is only available for Stop')
        with operation(values[0], values[3], overrule=overrule, service=values[2]):
            service_action(*values[:5])
    elif args.operation == 'reset-settings':
        with operation(values[0], 'settings'):
            reset_settings(*values)
    elif args.operation == 'snapshot-config':
        print(json.dumps(snapshot_config(*values)))
    elif args.operation == 'snapshot-edit':
        with operation(values[0], 'snapshot-edit'):
            snapshot_edit(*values)
    elif args.operation == 'snapshots':
        print(json.dumps(snapshot_list(*values)))
    elif args.operation == 'snapshot':
        with operation(values[0], 'snapshot-' + values[2]):
            project_snapshot(*values)
    elif args.operation == 'storage-limit':
        with operation(values[0], 'settings'):
            project_storage_limit(*values)
    elif args.operation == 'diagnostics':
        print(json.dumps(diagnostics(*values)))
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
