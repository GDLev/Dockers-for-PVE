#!/usr/bin/env python3
"""Root-only host plumbing for the configured rootless Docker namespace."""
# Check the installed code tree before importing non-frozen modules.
import os as _boot_os
import sys as _boot_sys
if __file__.startswith('/usr/local/lib/betterdocker/'):
    _boot_root = '/usr/local/lib/betterdocker'
    for _boot_dir, _boot_dirs, _boot_files in _boot_os.walk(_boot_root, followlinks=False):
        for _boot_name in [_boot_dir, *(_boot_os.path.join(_boot_dir, n) for n in _boot_dirs + _boot_files)]:
            try:
                _boot_info = _boot_os.stat(_boot_name, follow_symlinks=False)
            except FileNotFoundError:
                # CPython replaces these temporary files with canonical bytecode.
                if _boot_os.path.basename(_boot_dir) == '__pycache__' and '.pyc.' in _boot_name and _boot_name.rsplit('.pyc.', 1)[1].isdigit():
                    continue
                raise
            if _boot_info.st_uid != 0 or _boot_info.st_mode & 0o022 or _boot_os.path.islink(_boot_name):
                _boot_sys.stderr.write('BetterDocker: unsafe installed helper ownership or permissions\n')
                raise SystemExit(1)
from dctlib.secure_io import read_json
import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

import runtime

LINK = 'bd-rootless'
PEER = 'bd-rl-peer'
UPLINK = 'bd-uplink'
OWNER = 'betterdocker:rootless'
POOL = ipaddress.ip_network('10.240.0.0/16')
STATE = Path('/run/betterdocker/rootless-network.json')


def command(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Host network operation failed')
    return result.stdout


@contextlib.contextmanager
def namespace():
    """Pin a mapped namespace; never enter the host network or an arbitrary PID."""
    selected = runtime.load()
    if not selected.rootless or os.geteuid() != 0:
        raise ValueError('Rootless host administration requires root and a configured runtime')
    expected_uid = [[0, selected.uid, 1], [1, selected.subuid, selected.count]]
    expected_gid = [[0, selected.gid, 1], [1, selected.subgid, selected.count]]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(selected.socket)
            pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        if uid != selected.uid:
            raise ValueError('Rootless Docker socket ownership changed')
    except OSError as error:
        raise ValueError('Configured rootless Docker namespace is unavailable') from error
    for folder in (Path('/proc') / str(pid),):
        try:
            if folder.stat().st_uid != selected.uid or (folder / 'comm').read_text().strip() != 'dockerd':
                continue
            executable = folder / 'exe'
            trusted = shutil.which('dockerd', path=runtime.PATH)
            if not trusted or Path(os.readlink(executable).removesuffix(' (deleted)')).resolve() != Path(trusted).resolve() or executable.stat().st_uid != 0:
                raise ValueError('Rootless Docker executable is not the trusted host daemon')
            if [[int(v) for v in line.split()] for line in (folder / 'uid_map').read_text().splitlines()] != expected_uid or [[int(v) for v in line.split()] for line in (folder / 'gid_map').read_text().splitlines()] != expected_gid:
                continue
            user = os.open(folder / 'ns/user', os.O_RDONLY)
            net = None
            try:
                net = os.open(folder / 'ns/net', os.O_RDONLY)
                mount = os.open(folder / 'ns/mnt', os.O_RDONLY)
            except BaseException:
                os.close(user)
                if net is not None:
                    os.close(net)
                raise
            if os.fstat(net).st_ino == Path('/proc/self/ns/net').stat().st_ino:
                os.close(user); os.close(net); os.close(mount)
                raise ValueError('Refusing host network namespace')
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        try:
            prefix = ['nsenter', '--user=/proc/self/fd/' + str(user), '--net=/proc/self/fd/' + str(net),
                      '--mount=/proc/self/fd/' + str(mount), '--setuid=0', '--setgid=0']
            yield int(folder.name), os.fstat(net).st_ino, prefix, (user, net, mount)
            return
        finally:
            os.close(user); os.close(net); os.close(mount)
    raise ValueError('Configured rootless Docker namespace is unavailable')


def in_namespace(args, **kwargs):
    with namespace() as (_, _, prefix, descriptors):
        return command(prefix + args, pass_fds=descriptors, **kwargs)


def forwarding(interface, prefix=(), descriptors=()):
    args = list(prefix) + ['iptables', '-w']
    rules = [['FORWARD', direction, interface, '-j', 'ACCEPT'] for direction in ('-i', '-o')]
    current = command(args + ['-S', 'FORWARD'], pass_fds=descriptors).splitlines()
    head = [line for line in current if line.startswith('-A ')][:2]
    if set(head) == {'-A ' + ' '.join(rule) for rule in rules}:
        return
    for rule in rules:
        while subprocess.run(args + ['-C', *rule], pass_fds=descriptors, capture_output=True).returncode == 0:
            command(args + ['-D', *rule], pass_fds=descriptors)
        command(args + ['-I', *rule], pass_fds=descriptors)


@contextlib.contextmanager
def lock():
    if os.geteuid() != 0 or not runtime.load().rootless:
        raise ValueError('Rootless host administration requires root and a configured runtime')
    STATE.parent.mkdir(exist_ok=True, mode=0o700)
    with (STATE.parent / 'rootless-network.lock').open('a') as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def setup():
    with lock():
        setup_locked()


def setup_locked():
    with namespace() as (pid, inode, prefix, descriptors):
        links = json.loads(command(['ip', '-j', 'link', 'show']))
        link = next((item for item in links if item['ifname'] == LINK), None)
        saved = read_json(STATE) if STATE.exists() else {}
        if link and link.get('ifalias') != OWNER:
            raise ValueError('Rootless host interface is occupied by another owner')
        inner = json.loads(command(prefix + ['ip', '-j', 'link', 'show'], pass_fds=descriptors))
        if link and (saved.get('inode') != inode or not any(i['ifname'] == UPLINK for i in inner)):
            command(['ip', 'link', 'delete', LINK])
            link = None
        if not link:
            if any(i['ifname'] == PEER for i in links):
                raise ValueError('Rootless peer interface is already occupied')
            # Keep a new namespace closed until its current guest rules are loaded.
            guard = 'table inet betterdocker_rootless {\nchain guard {\ntype filter hook forward priority -20; policy accept;\niifname "bd-rootless" drop\noifname "bd-rootless" drop\n}\n}\n'
            current = subprocess.run(['nft', 'list', 'table', 'inet', 'betterdocker_rootless'], capture_output=True)
            command(['nft', '-f', '-'], input=('delete table inet betterdocker_rootless\n' if current.returncode == 0 else '') + guard)
            command(['ip', 'link', 'add', LINK, 'type', 'veth', 'peer', 'name', PEER])
            command(['ip', 'link', 'set', LINK, 'alias', OWNER])
            try:
                command(['ip', 'link', 'set', PEER, 'netns', str(pid)])
                if Path('/proc/' + str(pid) + '/ns/net').stat().st_ino != inode:
                    raise ValueError('Rootless namespace changed during network setup')
                command(prefix + ['ip', 'link', 'set', PEER, 'name', UPLINK], pass_fds=descriptors)
            except BaseException:
                command(['ip', 'link', 'delete', LINK])
                raise
        command(['ip', 'addr', 'replace', '169.254.240.1/30', 'dev', LINK])
        command(['ip', 'link', 'set', LINK, 'up'])
        command(prefix + ['ip', 'addr', 'replace', '169.254.240.2/30', 'dev', UPLINK], pass_fds=descriptors)
        command(prefix + ['ip', 'link', 'set', UPLINK, 'up'], pass_fds=descriptors)
        command(prefix + ['ip', 'route', 'replace', 'default', 'via', '169.254.240.1', 'dev', UPLINK], pass_fds=descriptors)
        command(prefix + ['sysctl', '-q', '-w', 'net.ipv4.ip_forward=1'], pass_fds=descriptors)
        command(['ip', 'route', 'replace', str(POOL), 'via', '169.254.240.2', 'dev', LINK])
        # Host forwarding is needed by the PVE-managed route, never by a user proxy.
        command(['sysctl', '-q', '-w', 'net.ipv4.ip_forward=1'])
        forwarding(LINK)
        forwarding(UPLINK, prefix, descriptors)
        STATE.parent.mkdir(exist_ok=True, mode=0o700)
        STATE.write_text(json.dumps({'pid': pid, 'inode': inode}) + '\n')
        STATE.chmod(0o600)


def ready():
    selected = runtime.load()
    if not selected.rootless:
        return
    for _ in range(120):
        try:
            setup()
            return
        except ValueError as error:
            if str(error) != 'Configured rootless Docker namespace is unavailable':
                raise
            time.sleep(0.5)
    raise ValueError('Rootless Docker namespace did not become available')


def valid_address(address):
    value = ipaddress.ip_address(address)
    if value not in POOL:
        raise ValueError('Rootless container address is outside the reserved runtime network')
    return str(value)


def remove():
    with lock():
        remove_locked()


def remove_locked():
    if os.geteuid() != 0 or not runtime.load().rootless:
        raise ValueError('Rootless host administration requires root and a configured runtime')
    links = json.loads(command(['ip', '-j', 'link', 'show']))
    link = next((i for i in links if i['ifname'] == LINK), None)
    if link:
        if link.get('ifalias') != OWNER:
            raise ValueError('Rootless host interface ownership changed')
        command(['ip', 'link', 'delete', LINK])
    for direction in ('-i', '-o'):
        rule = ['FORWARD', direction, LINK, '-j', 'ACCEPT']
        if subprocess.run(['iptables', '-w', '-C', *rule], capture_output=True).returncode == 0:
            command(['iptables', '-w', '-D', *rule])
    if subprocess.run(['nft', 'list', 'table', 'inet', 'betterdocker_rootless'], capture_output=True).returncode == 0:
        command(['nft', 'delete', 'table', 'inet', 'betterdocker_rootless'])
    STATE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'remove'))
    args = parser.parse_args()
    remove() if args.action == 'remove' else ready()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
