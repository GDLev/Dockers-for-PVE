#!/usr/bin/env python3
"""Native host Docker projects. Public cluster metadata never contains deployment secrets."""
import argparse
import contextlib
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
    allowed = {'name', 'mode', 'compose', 'image', 'ports', 'environment', 'bridge', 'address'}
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
    address = ipaddress.IPv4Address(data.get('address', ''))
    if address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local:
        raise ValueError('Choose a usable LAN IPv4 address')
    return dict(data, mode=mode, environment=environment, address=str(address))


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


def prepare(data, identifier, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    source = data['compose'] if data['mode'] == 'compose' else image_compose(data)
    (directory / 'compose.source.yaml').write_text(source)
    (directory / '.env').write_text(data.get('environment', ''))
    for name in ('compose.source.yaml', '.env'):
        os.chmod(directory / name, 0o600)
    # Relative bind mounts would resolve into managed project/staging directories.
    command = compose_base(directory, identifier, 'compose.source.yaml') + ['config', '--format', 'json']
    # Canonical config may contain passwords; never send it to the task log.
    try:
        config = json.loads(capture(command))
    except subprocess.CalledProcessError:
        raise ValueError('Invalid Compose configuration. Check YAML, .env and referenced files.') from None
    for service in config.get('services', {}).values():
        for volume in service.get('volumes', []):
            if volume.get('type') == 'bind' and str(volume.get('source', '')).startswith(str(directory) + '/'):
                raise ValueError('Use absolute host paths for bind mounts; project files are not uploaded')
    config = bind_ports(config, data['address'], identifier)
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


def network_preflight(data):
    address = ipaddress.IPv4Address(data['address'])
    interfaces = json.loads(capture(['ip', '-j', '-4', 'address', 'show']))
    selected = next((interface for interface in interfaces if interface['ifname'] == data['bridge']), None)
    if not selected or not Path('/sys/class/net', data['bridge'], 'bridge').is_dir():
        raise ValueError('The selected interface is not a Linux bridge')
    if any(str(address) == entry.get('local') for interface in interfaces for entry in interface.get('addr_info', [])):
        raise ValueError('This address already belongs to the host; choose a separate unused IP')
    networks = [ipaddress.IPv4Network(f"{entry['local']}/{entry['prefixlen']}", strict=False)
                for entry in selected.get('addr_info', []) if entry.get('prefixlen', 32) < 31]
    if not any(address in network and address not in (network.network_address, network.broadcast_address)
               for network in networks):
        raise ValueError('The project IP must belong to a connected IPv4 subnet on this bridge')
    if any(item['address'] == data['address'] for item in summaries()):
        raise ValueError('This IP is already reserved by another Docker project')
    if any(item['node'] == current_node() and item['name'] == data['name'] for item in summaries()):
        raise ValueError('A project with this name already exists on this node')
    probe = subprocess.run(['arping', '-D', '-I', data['bridge'], '-c', '3', '-w', '4', data['address']],
                           text=True, capture_output=True, timeout=10)
    if probe.returncode != 0:
        raise ValueError('The IP responded to duplicate-address detection; choose an unused address')


def ensure_address(info):
    existing = json.loads(capture(['ip', '-j', '-4', 'address', 'show', 'dev', info['bridge']]))
    if any(entry.get('local') == info['address'] for interface in existing for entry in interface.get('addr_info', [])):
        return
    run(['ip', 'address', 'add', info['address'] + '/32', 'dev', info['bridge']])


def current_node():
    return os.uname().nodename.split('.')[0]


def create(data, node):
    if node != current_node():
        raise ValueError('The request was sent to the wrong node')
    identifier = uuid.uuid4().hex
    with locked():
        network_preflight(data)
        directory = ROOT / identifier
        try:
            config = prepare(data, identifier, directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        write_json(directory / 'request.json', data)
        info = {'id': identifier, 'node': node, 'name': data['name'], 'bridge': data['bridge'],
                'address': data['address'], 'status': 'creating', 'created': int(time.time()),
                'expected': sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())}
        write_json(METADATA / (identifier + '.json'), info)
        try:
            ensure_address(info)
            run(compose_base(directory, identifier) + ['up', '-d'], timeout=1800)
            refresh(node)
        except Exception:
            info['status'] = 'error'
            write_json(METADATA / (identifier + '.json'), info)
            print(f'Project {identifier} retained for diagnosis; open its Docker screen.', file=sys.stderr)
            raise
    print(f"Created {data['name']} at {data['address']}")


def detail(identifier, node):
    identifier = project_id(identifier)
    info = json.loads((METADATA / (identifier + '.json')).read_text())
    if info['node'] != node or node != current_node():
        raise ValueError('Project belongs to another node')
    return dict(info, deployment=json.loads((ROOT / identifier / 'request.json').read_text()))


def refresh(node):
    entries = json.loads('[' + ','.join(capture(['docker', 'ps', '-a', '--filter', 'label=betterdocker.id',
                                              '--format', '{{json .}}']).splitlines()) + ']')
    for info in summaries():
        if info['node'] != node:
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


def action(identifier, node, operation):
    with locked():
        info = detail(identifier, node)
        directory = ROOT / identifier
        commands = {'start': ['up', '-d'], 'stop': ['stop'], 'restart': ['restart'], 'delete': ['down']}
        if operation not in commands:
            raise ValueError('Unsupported operation')
        if operation in ('start', 'restart'):
            ensure_address(info)
        run(compose_base(directory, identifier) + commands[operation], timeout=1800)
        if operation == 'delete':
            # Reserve the metadata until the owned alias is successfully released.
            run(['ip', 'address', 'del', info['address'] + '/32', 'dev', info['bridge']])
            (METADATA / (identifier + '.json')).unlink()
            shutil.rmtree(directory)
            print('Project removed. Docker volumes are retained.')
        else:
            info.pop('deployment', None)
            if operation == 'stop':
                info['status'] = 'stopped'
                write_json(METADATA / (identifier + '.json'), info)
            refresh(node)


def update(identifier, node, data):
    with locked():
        existing = detail(identifier, node)
        for key in ('bridge', 'address', 'name'):
            if data[key] != existing[key]:
                raise ValueError('Project name, bridge and IP cannot be changed during redeployment')
        directory = ROOT / identifier
        with tempfile.TemporaryDirectory(prefix='betterdocker-update-', dir=ROOT) as folder:
            staging = Path(folder)
            config = prepare(data, identifier, staging)
            # Compose resolves relative bind paths against staging. Reject relative bind mounts
            # in both creation and editing; uploaded build/config files are not available.
            for filename in ('compose.source.yaml', '.env', 'compose.runtime.json'):
                os.replace(staging / filename, directory / filename)
        write_json(directory / 'request.json', data)
        info = {key: value for key, value in existing.items() if key != 'deployment'}
        info['expected'] = sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())
        info['status'] = 'creating'
        write_json(METADATA / (identifier + '.json'), info)
        try:
            ensure_address(info)
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
                ensure_address(info)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['validate', 'create', 'detail', 'action', 'logs', 'restore', 'refresh', 'update'])
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    values = args.arguments
    if args.operation in ('validate', 'create'):
        data = validate(json.loads(Path(values[0]).read_text()))
        if args.operation == 'create':
            create(data, values[1])
    elif args.operation == 'update':
        update(values[0], values[1], validate(json.loads(Path(values[2]).read_text())))
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
