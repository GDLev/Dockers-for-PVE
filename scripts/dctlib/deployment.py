"""Compose validation, deployment and recreation."""
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
import runtime as node_runtime


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Deployment must be an object')
    allowed = {'name', 'mode', 'compose', 'image', 'ports', 'environment', 'bridge', 'address', 'ipv4mode', 'cidr', 'hwaddr', 'mtu', 'vmid', 'firewall'}
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
        worker.parse_ports(data.get('ports', ''))
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
    if raw_mtu not in (None, '') and not re.fullmatch(r'[0-9]+', str(raw_mtu)):
        raise ValueError('Invalid MTU')
    mtu = int(raw_mtu) if raw_mtu not in (None, '') else 0
    if mtu and not 576 <= mtu <= 9216:
        raise ValueError('MTU must be between 576 and 9216')
    if data.get('firewall', 0) not in (0, 1, False, True):
        raise ValueError('Firewall must be 0 or 1')
    return dict(data, mode=mode, environment=environment, address=address, cidr=cidr,
                ipv4mode=ipv4mode, hwaddr=hwaddr, mtu=mtu, firewall=int(data.get('firewall', 0)))


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
        'ports': worker.parse_ports(data.get('ports', '')), 'env_file': ['.env'],
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
    command = ['docker', 'compose', '--project-name', 'bd-' + identifier,
               '--project-directory', str(directory), '--env-file', str(directory / '.env'),
               '-f', str(directory / filename)]
    if (directory / 'ha.json').exists():
        runtime = json.loads((directory / 'compose.runtime.json').read_text())
        override = directory / '.ha-compose.json'
        worker.write_json(override, {'services': {name: {'restart': 'no'} for name in runtime['services']}})
        os.chmod(override, 0o600)
        command.extend(['-f', str(override)])
    return command


def prepare(data, identifier, directory, project_directory=None):
    project_directory = project_directory or directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    source = data['compose'] if data['mode'] == 'compose' else worker.image_compose(data)
    if not worker.trusted_management():
        worker.validate_delegated_source(source)
    (directory / 'compose.source.yaml').write_text(source)
    (directory / '.env').write_text(data.get('environment', ''))
    for name in ('compose.source.yaml', '.env'):
        os.chmod(directory / name, 0o600)
    command = worker.compose_base(directory, identifier, 'compose.source.yaml') + ['config', '--no-env-resolution', '--format', 'json']
    # Use the permanent project directory even when validating an edit in staging.
    command[command.index('--project-directory') + 1] = str(project_directory)
    # Canonical config may contain passwords; never send it to the task log.
    try:
        config = json.loads(worker.capture(command))
    except subprocess.CalledProcessError as error:
        locations = sorted(set(re.findall(r'line [0-9]+', error.stderr or '')))
        location = ' (' + ', '.join(locations) + ')' if locations else ''
        raise ValueError('Invalid Compose configuration' + location + '. Check YAML, .env and referenced files.') from None
    config = worker.bind_ports(config, data['address'], identifier)
    worker.apply_log_defaults(config)
    if node_runtime.load().rootless:
        for service in config['services'].values():
            for mount in service.get('volumes', []):
                if mount.get('type') == 'bind':
                    worker.safe_bind(mount.get('source', ''), identifier)
    if not worker.trusted_management():
        worker.validate_delegated_config(config, identifier)
    worker.apply_settings(config, worker.load_settings(project_directory))
    worker.apply_storage(config, identifier)
    worker.project_limits.check_directory(project_directory, config)
    # config's result is already interpolated. Escape literal dollars before a second Compose read.
    def escape(value):
        if isinstance(value, str):
            return value.replace('$', '$$')
        if isinstance(value, list):
            return [escape(v) for v in value]
        if isinstance(value, dict):
            return {k: escape(v) for k, v in value.items()}
        return value
    worker.write_json(directory / 'compose.runtime.json', escape(config))
    os.chmod(directory / 'compose.runtime.json', 0o600)
    return config


def create(data, node, identifier=None, hookscript=None, start=True):
    if node != worker.current_node():
        raise ValueError('The request was sent to the wrong node')
    if hookscript:
        if os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam') != 'root@pam':
            raise ValueError('Changing the hookscript is only allowed for root@pam')
        worker.pve_hook(hookscript)
    identifier = worker.project_id(identifier) if identifier else uuid.uuid4().hex
    with worker.locked(identifier):
        if (worker.METADATA / (identifier + ".json")).exists() or (worker.ROOT / identifier).exists():
            raise ValueError("Project ID already exists")
        vmid = next((vmid for vmid, entry in worker.reserved_ids().items() if entry['id'] == identifier), None)
        if vmid is None:
            raise ValueError('DCT ID must be reserved through the Proxmox API before deployment')
        if any((worker.PVE_NODES / host.name / kind / f'{vmid}.conf').exists()
               for host in worker.PVE_NODES.iterdir() for kind in ('qemu-server', 'lxc')):
            raise ValueError(f'Guest ID {vmid} is already in use by VM or LXC')
        worker.network_preflight(data)
        directory = worker.ROOT / identifier
        try:
            config = worker.prepare(dict(data, address=data.get('address') or '0.0.0.0'), identifier, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        (directory / 'snapshot-restored.json').unlink(missing_ok=True)
        worker.write_json(directory / 'request.json', data)
        info = {'id': identifier, 'vmid': vmid, 'node': node, 'name': data['name'], 'bridge': data['bridge'],
                'address': data['address'], 'status': 'creating', 'created': int(time.time()),
                'expected': sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values()),
                'ipv4mode': data.get('ipv4mode', 'static'), 'cidr': data.get('cidr', ''),
                'interface': 'bd' + identifier[:12], 'mtu': data.get('mtu', 0), 'firewall': data.get('firewall', 0),
                'hwaddr': data.get('hwaddr') or '02:' + ':'.join(identifier[i:i+2] for i in range(0, 10, 2))}
        info['runtime_mode'] = node_runtime.load().mode
        if hookscript:
            info['hookscript'] = hookscript
        worker.write_json(worker.METADATA / (identifier + '.json'), info)
        try:
            worker.ensure_address(info)
            worker.rebind_runtime(directory, info['address'])
            worker.write_json(worker.METADATA / (identifier + '.json'), info)
            if start:
                worker.guest_hook(info, 'pre-start')
                worker.expect_guest_hook(info, 'post-start')
                worker.prepare_firewall(info)
                worker.run(worker.compose_base(directory, identifier) + ['up', '-d'], timeout=1800)
                worker.finish_network_start(info)
                worker.guest_hook(info, 'post-start')
            else:
                worker.run(worker.compose_base(directory, identifier) + ['up', '--no-start'], timeout=1800)
            worker.refresh(node)
        except Exception:
            info['status'] = 'error'
            worker.write_json(worker.METADATA / (identifier + '.json'), info)
            print(f'Project {identifier} retained for diagnosis; open its Docker screen.', file=sys.stderr)
            raise
    print(f"Created {data['name']} at {info['address']}")


def update(identifier, node, data, digest=None):
    with worker.locked(identifier):
        existing = worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        if data.get('vmid') and data['vmid'] != existing.get('vmid'):
            raise ValueError('DCT ID cannot be changed during deployment edits')
        previous = existing['deployment']
        for key in ('bridge', 'name', 'ipv4mode', 'hwaddr', 'mtu', 'firewall'):
            default = 'static' if key == 'ipv4mode' else 0 if key in ('mtu', 'firewall') else ''
            if data.get(key, default) != previous.get(key, default):
                raise ValueError('Network settings cannot be changed during redeployment')
        for key in ('bridge', 'name'):
            if data[key] != existing[key]:
                raise ValueError('Project name, bridge and IP cannot be changed during redeployment')
        if data.get('ipv4mode', 'static') == 'static' and data['address'] != existing['address']:
            raise ValueError('The project IP cannot be changed during redeployment')
        worker.require_stopped(worker.container_records(identifier))
        directory = worker.ROOT / identifier
        if worker.load_pending(directory) or worker.load_pending_network(directory):
            raise ValueError('Apply or revert pending changes before editing deployment')
        with tempfile.TemporaryDirectory(prefix='betterdocker-update-', dir=worker.ROOT) as folder:
            staging = Path(folder)
            config = worker.prepare(dict(data, address=existing.get('address') or '0.0.0.0'), identifier, staging, project_directory=directory)
            worker.check_protected_layout(existing, json.loads((directory / 'compose.runtime.json').read_text()),
                                   json.loads((staging / 'compose.runtime.json').read_text()))
            for filename in ('compose.source.yaml', '.env', 'compose.runtime.json'):
                os.replace(staging / filename, directory / filename)
        (directory / 'snapshot-restored.json').unlink(missing_ok=True)
        worker.write_json(directory / 'request.json', data)
        info = {key: value for key, value in existing.items() if key != 'deployment'}
        info['expected'] = sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())
        info['status'] = 'creating'
        worker.write_json(worker.METADATA / (identifier + '.json'), info)
        try:
            worker.ensure_address(info)
            worker.rebind_runtime(directory, info['address'])
            worker.write_json(worker.METADATA / (identifier + '.json'), info)
            worker.run(worker.compose_base(directory, identifier) + ['up', '--no-start', '--remove-orphans'], timeout=1800)
            worker.refresh(node)
        except Exception:
            info['status'] = 'error'
            worker.write_json(worker.METADATA / (identifier + '.json'), info)
            raise


def restore(node):
    with worker.locked():
        for info in worker.summaries():
            if info['node'] == node and info.get('id'):
                worker.recover_storage_transaction(info['id'])
        for path in worker.ROOT.glob('*/storage.json'):
            metadata = worker.METADATA / (path.parent.name + '.json')
            if metadata.exists() and json.loads(metadata.read_text())['node'] == node:
                worker.storage_mount(path.parent.name)
        for info in worker.summaries():
            if info['node'] == node:
                # A fenced node can boot with stale cluster metadata. The HA
                # agent owns bringing its endpoint back, just as it owns Start.
                if info.get('ha_recovery') or (info.get('id') and (worker.ROOT / info['id'] / 'ha.json').exists()):
                    continue
                if info.get('id') and worker.recover_network_transaction(info['id'], node):
                    info = json.loads((worker.METADATA / (info['id'] + '.json')).read_text())
                worker.ensure_address(info, wait=False)


def preview(data, identifier=None):
    identifier = worker.project_id(identifier) if identifier else uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='betterdocker-preview-') as folder:
        config = worker.prepare(dict(worker.validate(data), address=data.get('address') or '0.0.0.0'), identifier,
                         Path(folder), project_directory=worker.ROOT / identifier)
        # Deliberately return only a plan, never expanded env values or secrets.
        return [{'service': name, 'image': service.get('image', ''),
                 'replicas': service.get('deploy', {}).get('replicas', 1),
                 'ports': service.get('ports', []),
                 'mounts': [{'type': m.get('type'), 'target': m.get('target'),
                             'read_only': m.get('read_only', False)} for m in service.get('volumes', [])]}
                for name, service in config['services'].items()]


def reset_settings(identifier, node, service, digest=None):
    with worker.locked(identifier):
        info = worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        directory = worker.ROOT / identifier
        settings = worker.load_settings(directory)
        if service not in settings:
            if service in worker.load_pending(directory):
                worker.require_stopped([c for c in worker.container_records(identifier) if c['service'] == service])
                pending = worker.load_pending(directory)
                pending.pop(service, None)
                worker.save_pending(directory, pending)
            return
        original = (directory / 'compose.runtime.json').read_text()
        previous = dict(settings)
        settings.pop(service)
        worker.require_stopped([c for c in worker.container_records(identifier) if c['service'] == service])
        marker = directory / 'snapshot-restored.json'
        if marker.exists():
            restored = json.loads(marker.read_text())
            restored['containers'] = [row['id'] for row in worker.container_records(identifier)]
            worker.write_json(marker, restored)
        worker.write_json(directory / 'settings.json', settings)
        os.chmod(directory / 'settings.json', 0o600)
        try:
            with tempfile.TemporaryDirectory(prefix='betterdocker-reset-', dir=worker.ROOT) as folder:
                staging = Path(folder)
                worker.prepare(dict(info['deployment'], address=info.get('address') or '0.0.0.0'), identifier, staging, directory)
                worker.check_protected_layout(info, json.loads(original), json.loads((staging / 'compose.runtime.json').read_text()))
                os.replace(staging / 'compose.runtime.json', directory / 'compose.runtime.json')
            worker.run(worker.compose_base(directory, identifier) + ['up', '--no-start', '--no-deps', service], timeout=1800)
            marker = directory / 'snapshot-restored.json'
            if marker.exists():
                restored = json.loads(marker.read_text())
                restored['containers'] = [row['id'] for row in worker.container_records(identifier)]
                worker.write_json(marker, restored)
        except Exception:
            worker.write_json(directory / 'settings.json', previous)
            (directory / 'compose.runtime.json').write_text(original)
            raise
        pending = worker.load_pending(directory)
        pending.pop(service, None)
        worker.save_pending(directory, pending)
        worker.refresh(node)


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
