#!/usr/bin/env python3
"""Install a fixed node runtime. Workload conversion is a separate operation."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time

import runtime
from dctlib.secure_io import anchored

ACCOUNT = 'betterdocker'
HOME = '/var/lib/betterdocker/runtime'


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def save(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    os.fchmod(fd, mode)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def select(requested, interactive=False):
    if runtime.CONFIG.exists():
        selected = runtime.load().mode
    elif Path('/usr/share/perl5/PVE/API2/BetterDocker.pm').exists() or any(Path('/var/lib/betterdocker/projects').glob('*/request.json')):
        selected = 'rootful'
    else:
        selected = requested
        if selected is None and interactive:
            try:
                with open('/dev/tty', 'r+') as terminal:
                    terminal.write('Docker runtime [rootful/rootless] (default rootless): ')
                    terminal.flush()
                    selected = terminal.readline().strip() or 'rootless'
            except OSError:
                pass
        if selected is None:
            raise ValueError('Fresh unattended installation requires --runtime=rootful or --runtime=rootless')
    if selected not in ('rootful', 'rootless'):
        raise ValueError('Choose rootful or rootless')
    if requested and selected != requested:
        raise ValueError('Runtime mode is fixed. Use migrate-runtime.py for an explicit conversion')
    return selected


def subordinate(account, filename):
    path = Path(filename)
    entries = [line.split(':') for line in path.read_text().splitlines() if line and not line.startswith('#')]
    own = [e for e in entries if e[0] == account]
    if own:
        if len(own) != 1 or int(own[0][2]) != 65536:
            raise ValueError('Runtime account needs one stable range of 65536 subordinate IDs')
        base = int(own[0][1])
    else:
        base = 1048576
        while any(base < int(e[1]) + int(e[2]) and int(e[1]) < base + 65536 for e in entries):
            base += 65536
    if any(e[0] != account and base < int(e[1]) + int(e[2]) and int(e[1]) < base + 65536 for e in entries):
        raise ValueError('Subordinate ID ranges overlap')
    # PVE may have administrator-defined LXC mappings outside /etc/subuid.
    for config in Path('/etc/pve/nodes').glob('*/lxc/*.conf'):
        for line in config.read_text().splitlines():
            if line.startswith('lxc.idmap:'):
                fields = line.split()
                kind = 'u' if filename.endswith('subuid') else 'g'
                if len(fields) == 5 and fields[1] == kind:
                    start, count = int(fields[3]), int(fields[4])
                    if base < start + count and start < base + 65536:
                        raise ValueError('Subordinate IDs overlap a configured LXC mapping')
    if not own:
        with path.open('a') as handle:
            handle.write(account + ':' + str(base) + ':65536\n')
    return base


def preflight(mode):
    if mode != 'rootless':
        return
    for name in ('docker', 'dockerd-rootless.sh', 'rootlesskit', 'newuidmap', 'newgidmap', 'loginctl', 'nsenter', 'nft', 'ip'):
        if not shutil.which(name):
            raise ValueError('Rootless dependency missing: ' + name)
    controllers = Path('/sys/fs/cgroup/cgroup.controllers')
    if not controllers.is_file() or not {'cpu', 'memory', 'pids'} <= set(controllers.read_text().split()):
        raise ValueError('Rootless requires cgroup v2 with CPU, memory and PID controllers')
    links = json.loads(command(['ip', '-j', 'route', 'show'], capture_output=True).stdout)
    import ipaddress
    for item in links:
        destination = item.get('dst', 'default')
        if destination == 'default' or item.get('dev') == 'bd-rootless':
            continue
        network = ipaddress.ip_network(destination, strict=False)
        if network.version == 4 and any(network.overlaps(ipaddress.ip_network(n)) for n in ('10.240.0.0/16', '169.254.240.0/30')):
            raise ValueError('Rootless network range conflicts with an existing host route: ' + destination)


def support_files(uid=None):
    paths = [Path(HOME) / name for name in ('.config/docker/daemon.json', '.config/systemd/user/docker.service',
                                          '.config/systemd/user/default.target.wants/docker.service')]
    paths += [Path('/etc/systemd/system/betterdocker-rootless.service'),
              Path('/etc/systemd/system/betterdocker-lifecycle.service'),
              Path('/etc/systemd/system/betterdocker-firewall.service'),
              Path('/etc/systemd/system/betterdocker-lifecycle.service.d/runtime.conf')]
    if uid is not None:
        paths.append(Path('/etc/systemd/system') / ('user@' + str(uid) + '.service.d/betterdocker.conf'))
    return paths


def snapshot_support(folder):
    try:
        uid = pwd.getpwnam(ACCOUNT).pw_uid
    except KeyError:
        uid = None
    entries = {}
    for path in support_files(uid):
        if path.is_symlink():
            entries[str(path)] = {'link': os.readlink(path)}
        elif path.exists():
            stat = path.stat()
            entries[str(path)] = dict(text=path.read_text(), mode=stat.st_mode & 0o777, uid=stat.st_uid, gid=stat.st_gid)
    save(Path(folder) / 'runtime-support.json', json.dumps(dict(files=entries, uid=uid,
         linger=Path('/var/lib/systemd/linger/betterdocker').exists())), mode=0o600)


def restore_support(folder):
    state = json.loads((Path(folder) / 'runtime-support.json').read_text())
    try:
        uid = pwd.getpwnam(ACCOUNT).pw_uid
    except KeyError:
        uid = None
    for path in support_files(uid):
        entry = state['files'].get(str(path))
        if entry is None:
            path.unlink(missing_ok=True)
        elif 'link' in entry:
            path.unlink(missing_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(entry['link'])
        else:
            save(path, entry['text'], entry['mode'])
            os.chown(path, entry['uid'], entry['gid'])
    if uid is not None and not state['linger']:
        command(['loginctl', 'disable-linger', ACCOUNT])
        command(['systemctl', 'stop', 'user@' + str(uid) + '.service'])


def lifecycle(mode):
    # systemd dependency directives cannot be cleared in a drop-in. Write the
    # complete owned unit so rootless boot never pulls in the host Docker daemon.
    dependency = ('Requires=betterdocker-rootless.service\nAfter=betterdocker-rootless.service'
                  if mode == 'rootless' else 'BindsTo=docker.service\nPartOf=docker.service\nAfter=docker.service')
    save('/etc/systemd/system/betterdocker-lifecycle.service', '''[Unit]
Description=Coordinate DCT lifecycle with Docker and PVE guests
Requires=betterdocker-addresses.service
DEPENDENCY
After=betterdocker-addresses.service
Before=pve-guests.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/betterdocker/provision.py daemon-resume
ExecStop=/usr/local/lib/betterdocker/provision.py daemon-prepare
TimeoutStartSec=300
TimeoutStopSec=300

[Install]
WantedBy=TARGET
'''.replace('DEPENDENCY', dependency).replace('TARGET', 'multi-user.target' if mode == 'rootless' else 'docker.service'))
    Path('/etc/systemd/system/betterdocker-lifecycle.service.d/runtime.conf').unlink(missing_ok=True)
    unit = 'betterdocker-rootless.service' if mode == 'rootless' else 'docker.service'
    save('/etc/systemd/system/betterdocker-firewall.service', '''[Unit]
Description=Apply native PVE firewall policy to Docker guests
Requires=pve-cluster.service
After=pve-cluster.service RUNTIME

[Service]
Type=simple
ExecStart=/usr/local/lib/betterdocker/firewall.py watch
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
'''.replace('RUNTIME', unit))


def initialize(mode, conversion=False):
    if os.geteuid() != 0:
        raise ValueError('Root required')
    if runtime.CONFIG.exists():
        previous = runtime.load().mode
        if previous != mode and not conversion:
            raise ValueError('Runtime mode is fixed; use explicit runtime migration')
        if mode == previous == 'rootful':
            lifecycle(mode)
            return
    if mode == 'rootful':
        save(runtime.CONFIG, json.dumps({'mode': mode}) + '\n')
        lifecycle(mode)
        return
    preflight(mode)
    try:
        user = pwd.getpwnam(ACCOUNT)
    except KeyError:
        command(['useradd', '--system', '--home-dir', HOME, '--shell', '/usr/sbin/nologin', '--user-group', ACCOUNT])
        user = pwd.getpwnam(ACCOUNT)
    if user.pw_uid == 0 or user.pw_dir != HOME:
        raise ValueError('Existing betterdocker account has an incompatible identity')
    groups = command(['id', '-nG', ACCOUNT], capture_output=True).stdout.split()
    if any(g in groups for g in ('sudo', 'docker', 'root')):
        raise ValueError('Rootless account must not have administrative or root Docker groups')
    subuid = subordinate(ACCOUNT, '/etc/subuid')
    subgid = subordinate(ACCOUNT, '/etc/subgid')
    home = Path(HOME)
    home.mkdir(parents=True, exist_ok=True, mode=0o755)
    if home.is_symlink() or home.stat().st_uid != 0:
        raise ValueError('Runtime home must be a root-owned real directory')
    # The user manager must reach HOME; sibling control directories stay private.
    with anchored(home.parent, directory=True) as parent:
        if os.fstat(parent).st_uid != 0:
            raise ValueError('Runtime parent must be root-owned')
        os.fchmod(parent, 0o711)
    for name in ('docker', '.docker'):
        path = home / name
        path.mkdir(exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError('Runtime data directory cannot be a symbolic link')
        os.chown(path, user.pw_uid, user.pw_gid)
    data = dict(mode=mode, account=ACCOUNT, uid=user.pw_uid, gid=user.pw_gid, home=HOME,
                socket='/run/user/' + str(user.pw_uid) + '/docker.sock', data=HOME + '/docker',
                subuid=subuid, subgid=subgid, count=65536)
    save(runtime.CONFIG, json.dumps(data, indent=2) + '\n')
    selected = runtime.load()
    save(home / '.config/docker/daemon.json', json.dumps({
        'data-root': selected.data, 'userland-proxy': False, 'ip-masq': False, 'ip-forward-no-drop': True,
        'default-address-pools': [{'base': '10.240.0.0/16', 'size': 24}],
        'bip': '10.240.255.1/24', 'exec-opts': ['native.cgroupdriver=systemd'],
    }, indent=2) + '\n')
    save(home / '.config/systemd/user/docker.service', '''[Unit]
Description=BetterDocker rootless Docker Engine

[Service]
Type=notify
NotifyAccess=all
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_NET=none
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=none
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false
Environment=DOCKERD=/usr/local/lib/betterdocker/rootless-engine.sh
ExecStart=/usr/bin/dockerd-rootless.sh --config-file=/var/lib/betterdocker/runtime/.config/docker/daemon.json
Restart=on-failure
RestartSec=2
TimeoutStartSec=300
TimeoutStopSec=300
Delegate=yes
KillMode=mixed
TasksMax=infinity
LimitNOFILE=infinity
LimitNPROC=infinity

[Install]
WantedBy=default.target
''')
    # Do not allow a workload account to replace the service or daemon settings.
    for item in (home, home / '.config', home / '.config/systemd', home / '.config/systemd/user', home / '.config/docker'):
        os.chown(item, 0, user.pw_gid)
        os.chmod(item, 0o755)
    save(Path('/etc/systemd/system') / ('user@' + str(user.pw_uid) + '.service.d/betterdocker.conf'),
         '[Unit]\nRequires=betterdocker-firewall-restore.service betterdocker-addresses.service\nAfter=betterdocker-firewall-restore.service betterdocker-addresses.service\n\n[Service]\nDelegate=cpu memory pids\n')
    save('/etc/systemd/system/betterdocker-rootless.service', '''[Unit]
Description=BetterDocker rootless host network
Requires=user@UID.service betterdocker-addresses.service
After=user@UID.service betterdocker-addresses.service
Before=betterdocker-lifecycle.service pve-guests.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/betterdocker/host.py setup
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
'''.replace('UID', str(user.pw_uid)))
    lifecycle(mode)
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'disable', 'betterdocker-lifecycle.service'])
    command(['systemctl', 'enable', 'betterdocker-lifecycle.service'])
    command(['loginctl', 'enable-linger', ACCOUNT])
    command(['systemctl', 'start', 'user@' + str(user.pw_uid) + '.service'])
    wants = home / '.config/systemd/user/default.target.wants'
    wants.mkdir(exist_ok=True, mode=0o755)
    with anchored(wants, directory=True) as directory:
        if os.fstat(directory).st_uid != 0:
            raise ValueError('Rootless service enable directory must be root-owned')
        os.fchmod(directory, 0o755)
    target = wants / 'docker.service'
    if target.is_symlink():
        if os.readlink(target) != '../docker.service':
            raise ValueError('Rootless Docker enable link has an unexpected target')
    elif target.exists():
        raise ValueError('Rootless Docker enable link is occupied')
    else:
        target.symlink_to('../docker.service')
    for action in ('daemon-reload', 'start'):
        args = ['systemctl', '--user', action] + ([] if action == 'daemon-reload' else ['docker.service'])
        command(args, env=selected.environment(), **runtime.drop_options(selected))
    command(['/usr/local/lib/betterdocker/host.py', 'setup'])
    command(['systemctl', 'enable', '--now', 'betterdocker-rootless.service'])
    for _ in range(60):
        result = runtime.run(['docker', 'info', '--format', '{{json .}}'], capture_output=True, text=True)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            if not any('rootless' in option for option in info.get('SecurityOptions', [])) or info.get('CgroupVersion') != '2' or info.get('CgroupDriver') != 'systemd':
                raise ValueError('Selected daemon is not rootless with cgroup v2/systemd support')
            break
        time.sleep(1)
    else:
        raise ValueError('Rootless daemon did not become ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('select', 'preflight', 'initialize', 'snapshot-support', 'restore-support'))
    parser.add_argument('--backup-dir')
    parser.add_argument('--runtime', choices=('rootful', 'rootless'))
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Root required')
    if args.action in ('snapshot-support', 'restore-support'):
        if not args.backup_dir:
            raise ValueError('Backup directory required')
        (snapshot_support if args.action == 'snapshot-support' else restore_support)(args.backup_dir)
    elif args.action == 'select':
        print(select(args.runtime, args.interactive))
    else:
        if not args.runtime:
            raise ValueError('Runtime mode required')
        if args.action == 'preflight':
            preflight(args.runtime)
        else:
            initialize(args.runtime)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
