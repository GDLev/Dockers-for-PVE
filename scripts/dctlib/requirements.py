"""Supported host and Docker versions for installation and repair."""
import argparse
import json
import platform
import re
import subprocess
import sys


PVE_MINIMUM = (9, 2, 21)
KERNEL_MINIMUM = (7, 0, 14, 23)
DOCKER_MINIMUM = (29, 9, 1)
CONTAINERD_MINIMUM = (2, 4, 2)


def stable_version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:\+[A-Za-z0-9.-]+)?', value)
    return tuple(map(int, match.groups())) if match else ()


def supported_pve(value):
    match = re.match(r'^pve-manager/(\d+)\.(\d+)\.(\d+)(?:/|$)', value)
    version = tuple(map(int, match.groups())) if match else ()
    return version[:2] == (9, 2) and version >= PVE_MINIMUM


def require_host(version, kernel):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)-(\d+)-pve', kernel)
    running = tuple(map(int, match.groups())) if match else ()
    if not supported_pve(version) or running < KERNEL_MINIMUM:
        raise ValueError('Fully updated PVE 9.2 is required: pve-manager >= 9.2.21 and running PVE kernel >= 7.0.14-23-pve. Update PVE and reboot before installation or repair.')


def require_runtime(engine, containerd):
    docker_version = stable_version(engine)
    containerd_version = stable_version(containerd)
    if (docker_version[:1] != (29,) or docker_version < DOCKER_MINIMUM or
            containerd_version[:2] != (2, 4) or containerd_version < CONTAINERD_MINIMUM):
        raise ValueError('Docker >= 29.9.1 within 29.x and containerd >= 2.4.2 within 2.4.x are required. Update the Docker packages and restart the selected daemon during a maintenance window, then retry. Detected Docker ' + engine + ', containerd ' + containerd + '.')


def require_binaries():
    engine = subprocess.check_output(['dockerd', '--version'], text=True, timeout=15)
    containerd = subprocess.check_output(['containerd', '--version'], text=True, timeout=15)
    docker_match = re.fullmatch(r'Docker version ([^,\s]+), build \S+\s*', engine)
    containerd_match = re.fullmatch(r'containerd\s+\S+\s+(\S+)\s+\S+\s*', containerd)
    require_runtime(docker_match[1] if docker_match else engine.strip(),
                    containerd_match[1] if containerd_match else containerd.strip())


def require_daemon():
    import runtime
    result = runtime.run(['docker', 'version', '--format', '{{json .Server}}'],
                         check=True, text=True, capture_output=True, timeout=30)
    server = json.loads(result.stdout)
    components = {item['Name']: item['Version'] for item in server.get('Components', [])}
    require_runtime(server.get('Version', ''), components.get('containerd', 'unknown'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=('host', 'binaries', 'daemon'))
    args = parser.parse_args()
    if args.target == 'host':
        require_host(subprocess.check_output(['pveversion'], text=True, timeout=15).strip(), platform.release())
    elif args.target == 'binaries':
        require_binaries()
    else:
        require_daemon()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
