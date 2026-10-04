#!/usr/bin/env python3
"""Root-only worker. Guest commands use argument arrays, with secrets over stdin."""
import argparse
import fcntl
import ipaddress
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Docker settings must be an object')
    if set(data) - {'mode', 'image', 'ports', 'environment', 'command', 'compose'}:
        raise ValueError('Unknown Docker setting')
    mode = data.get('mode', 'image')
    if mode not in ('image', 'compose', 'engine'):
        raise ValueError('Invalid deployment mode')
    if mode == 'compose':
        compose = data.get('compose', '')
        if not isinstance(compose, str) or not compose.strip() or len(compose.encode()) > 240000:
            raise ValueError('Compose YAML is empty or too large')
    if mode == 'image':
        if not isinstance(data.get('image'), str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}', data['image']):
            raise ValueError('Invalid image reference')
    ports = data.get('ports', [])
    if not isinstance(ports, list) or len(ports) > 100:
        raise ValueError('Ports must be an array (maximum 100)')
    bindings = set()
    for port in ports:
        if not isinstance(port, dict) or set(port) - {'host', 'container', 'protocol', 'ip'}:
            raise ValueError('Invalid port mapping')
        for key in ('host', 'container'):
            if type(port.get(key)) is not int or not 1 <= port[key] <= 65535:
                raise ValueError('Ports must be integers between 1 and 65535')
        if port.get('protocol', 'tcp') not in ('tcp', 'udp'):
            raise ValueError('Protocol must be tcp or udp')
        address = port.get('ip', '0.0.0.0')
        ipaddress.IPv4Address(address)
        binding = (address, port['host'], port.get('protocol', 'tcp'))
        if binding in bindings:
            raise ValueError('Duplicate published port')
        bindings.add(binding)
    env = data.get('environment', {})
    if not isinstance(env, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or not isinstance(v, str) for k, v in env.items()):
        raise ValueError('Environment must map variable names to strings')
    command = data.get('command', [])
    if not isinstance(command, list) or any(not isinstance(arg, str) for arg in command):
        raise ValueError('Command must be a JSON array of strings')
    return data


def image_compose(data):
    service = {'image': data['image'], 'restart': 'unless-stopped'}
    if data.get('ports'):
        service['ports'] = [{'target': p['container'], 'published': str(p['host']),
                             'host_ip': p.get('ip', '0.0.0.0'), 'protocol': p.get('protocol', 'tcp')}
                            for p in data['ports']]
    if data.get('environment'):
        service['environment'] = data['environment']
    if data.get('command'):
        service['command'] = data['command']
    # JSON is valid YAML; dollar escaping prevents Compose environment interpolation.
    def escape(value):
        if isinstance(value, str):
            return value.replace('$', '$$')
        if isinstance(value, dict):
            return {k: escape(v) for k, v in value.items()}
        if isinstance(value, list):
            return [escape(v) for v in value]
        return value
    return json.dumps({'services': {'app': escape(service)}}, indent=2)


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def wait_create(node, upid, timeout=1800):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run(['pvesh', 'get', f'/nodes/{node}/tasks/{upid}/status', '--output-format', 'json'], capture_output=True)
        status = json.loads(result.stdout)
        if status['status'] == 'stopped':
            if status.get('exitstatus') != 'OK':
                raise RuntimeError('LXC creation failed: ' + status.get('exitstatus', 'unknown'))
            return
        time.sleep(2)
    raise RuntimeError('LXC creation timed out; inspect the creation task before retrying')


def provision(data, vmid, node, upid):
    wait_create(node, upid)
    with open(f'/run/lock/betterdocker-{vmid}.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(['pct', 'start', vmid])
        try:
            print('Installing Docker Engine and Compose inside the guest', flush=True)
            script = Path(__file__).with_name('docker-install.sh').read_text()
            run(['pct', 'exec', vmid, '--', 'bash', '-se'], input=script, timeout=1800)
            if data.get('mode', 'image') != 'engine':
                yaml = data['compose'] if data.get('mode', 'image') == 'compose' else image_compose(data)
                run(['pct', 'exec', vmid, '--', 'install', '-d', '-m', '0700', '/opt/betterdocker'])
                # File contents never appear in command arguments or task output.
                run(['pct', 'exec', vmid, '--', 'sh', '-c',
                     'umask 077; cat > /opt/betterdocker/compose.yaml'], input=yaml)
                base = ['pct', 'exec', vmid, '--', 'docker', 'compose', '--project-name', 'betterdocker',
                        '--project-directory', '/opt/betterdocker', '-f', '/opt/betterdocker/compose.yaml']
                run(base + ['config', '--quiet'], timeout=60)
                run(base + ['up', '-d'], timeout=1800)
                run(base + ['ps'], timeout=60)
            print('Docker machine ready. Its IP belongs to the LXC network interface.', flush=True)
            run(['pct', 'exec', vmid, '--', 'hostname', '-I'])
        except Exception:
            print(f'Provisioning failed. Guest {vmid} retained for diagnosis; inspect its console and /opt/betterdocker.', file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['--validate', '--provision'])
    parser.add_argument('config')
    parser.add_argument('guest', nargs='*')
    # Operation names begin with --, so separate them before argparse.
    args = parser.parse_args(['--'] + sys.argv[1:])
    data = validate(json.loads(Path(args.config).read_text()))
    if args.operation == '--provision':
        if len(args.guest) != 3 or not args.guest[0].isdigit():
            raise ValueError('Expected VMID, node and creation UPID')
        provision(data, *args.guest)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.SubprocessError, OSError) as error:
        print(f'BetterDocker: {error}', file=sys.stderr)
        sys.exit(1)
