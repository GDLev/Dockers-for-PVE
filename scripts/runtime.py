#!/usr/bin/env python3
"""Node runtime selection and the unprivileged Docker process boundary."""
import argparse
import contextlib
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/betterdocker/runtime.json')
PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


@dataclass(frozen=True)
class Runtime:
    mode: str = 'rootful'
    account: str = 'root'
    uid: int = 0
    gid: int = 0
    home: str = '/root'
    socket: str = '/var/run/docker.sock'
    data: str = '/var/lib/docker'
    subuid: int = 0
    subgid: int = 0
    count: int = 0

    @property
    def rootless(self):
        return self.mode == 'rootless'

    @property
    def system_unit(self):
        return 'user@' + str(self.uid) + '.service' if self.rootless else 'docker.service'

    def environment(self, delegated=False):
        client = str(Path(self.home) / '.docker')
        if delegated:
            client = '/var/lib/betterdocker/delegated-client'
        env = {'PATH': PATH, 'HOME': self.home, 'DOCKER_HOST': 'unix://' + self.socket,
               'DOCKER_CONFIG': client}
        if self.rootless:
            env.update(XDG_RUNTIME_DIR='/run/user/' + str(self.uid),
                       DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/' + str(self.uid) + '/bus')
        return env

    def identity(self):
        return {'mode': self.mode, 'uid': self.uid, 'gid': self.gid,
                'subuid': self.subuid, 'subgid': self.subgid, 'count': self.count}

    def host_id(self, value, group=False):
        if not self.rootless:
            return value
        if value == 0:
            return self.gid if group else self.uid
        if not 0 < value <= self.count:
            raise ValueError('Container ownership exceeds the rootless UID/GID range')
        return (self.subgid if group else self.subuid) + value - 1

    def container_id(self, value, group=False):
        if not self.rootless:
            return value
        if value == (self.gid if group else self.uid):
            return 0
        base = self.subgid if group else self.subuid
        if not base <= value < base + self.count:
            raise ValueError('File ownership is outside the recorded rootless UID/GID range')
        return value - base + 1


def load():
    if not CONFIG.exists() and not CONFIG.is_symlink():
        if Path('/etc/systemd/system/betterdocker-rootless.service').exists():
            raise ValueError('Runtime configuration is missing on a rootless-enabled node')
        return Runtime()
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise ValueError('Runtime configuration must be a root-owned, non-writable regular file')
        for parent in CONFIG.parents:
            value = parent.lstat()
            if not stat.S_ISDIR(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
                raise ValueError('Runtime configuration directory is not trusted')
        with os.fdopen(fd) as handle:
            fd = None
            data = json.load(handle)
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(data, dict) or data.get('mode') not in ('rootful', 'rootless'):
        raise ValueError('Invalid node runtime configuration')
    if data['mode'] == 'rootful':
        if set(data) - {'mode'}:
            raise ValueError('Rootful configuration cannot override the root socket or account')
        return Runtime()
    if set(data) != {'mode', 'account', 'uid', 'gid', 'home', 'socket', 'data', 'subuid', 'subgid', 'count'}:
        raise ValueError('Incomplete rootless runtime configuration')
    if data['account'] != 'betterdocker' or any(type(data[k]) is not int or data[k] <= 0 for k in ('uid', 'gid', 'subuid', 'subgid', 'count')):
        raise ValueError('Invalid rootless runtime identity')
    if data['count'] != 65536 or data['home'] != '/var/lib/betterdocker/runtime' or data['data'] != data['home'] + '/docker' or data['socket'] != '/run/user/' + str(data['uid']) + '/docker.sock':
        raise ValueError('Invalid rootless runtime paths or mapping')
    user = pwd.getpwnam(data['account'])
    if (user.pw_uid, user.pw_gid, user.pw_dir) != (data['uid'], data['gid'], data['home']):
        raise ValueError('Rootless runtime account changed')
    for filename, field in (('/etc/subuid', 'subuid'), ('/etc/subgid', 'subgid')):
        ranges = [line.split(':') for line in Path(filename).read_text().splitlines() if line.startswith(data['account'] + ':')]
        if ranges != [[data['account'], str(data[field]), str(data['count'])]]:
            raise ValueError('Rootless subordinate UID/GID mapping changed')
    return Runtime(**data)


def drop_options(runtime):
    if not runtime.rootless:
        return {}
    if os.geteuid() == 0:
        return {'user': runtime.uid, 'group': runtime.gid, 'extra_groups': []}
    if os.geteuid() != runtime.uid or os.getegid() != runtime.gid:
        raise ValueError('Docker operation requires the selected runtime account')
    return {}


def read_access(path, runtime=None):
    """Keep control files root-owned; grant only read/traverse to the runtime."""
    runtime = runtime or load()
    if not runtime.rootless:
        return
    path = Path(path).absolute()
    for item in reversed([path, *path.parents]):
        if str(item) in ('/', '/tmp', '/var', '/var/lib'):
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('Runtime input crosses a symbolic link')
        if info.st_uid != 0:
            if stat.S_ISDIR(info.st_mode) and info.st_mode & 0o005 == 0o005:
                continue
            raise ValueError('Runtime control input must remain root-owned')
        mode = stat.S_IMODE(info.st_mode)
        os.chown(item, 0, runtime.gid)
        os.chmod(item, mode | (0o050 if stat.S_ISDIR(info.st_mode) else 0o040))


def check_volume_path(path, name, runtime=None):
    runtime = runtime or load()
    if runtime.rootless:
        expected = Path(runtime.data) / 'volumes' / name / '_data'
        if Path(path) != expected or any(p.is_symlink() for p in [expected, *expected.parents]):
            raise ValueError('Docker volume path is outside the selected runtime storage')


def prepare_compose(args, runtime):
    for index, word in enumerate(args[:-1]):
        if word in ('-f', '--file', '--env-file', '--project-directory'):
            path = Path(args[index + 1])
            if path.exists():
                read_access(path, runtime)
    # Compose can create missing bind paths as namespace root, but ancestors must
    # already belong to the host coordinator, not a workload.
    if '--project-directory' not in args:
        return
    project = Path(args[args.index('--project-directory') + 1])
    config = project / 'compose.runtime.json'
    if config.exists():
        value = json.loads(config.read_text())
        check_resources(value, runtime)
        for service in value.get('services', {}).values():
            for mount in service.get('volumes', []):
                if mount.get('type') != 'bind':
                    continue
                path = Path(mount['source'])
                if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(project) or path == project:
                    raise ValueError('Rootless DCT requires project-owned bind mounts')
                if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                    raise ValueError('Rootless bind path crosses a symbolic link')
                if not path.exists():
                    if mount.get('bind', {}).get('create_host_path', True) is False:
                        raise ValueError('Rootless bind mount source does not exist')
                    path.mkdir(parents=True)
                    os.chown(path, runtime.uid, runtime.gid)
                for parent in path.parents:
                    if parent == project:
                        break
                    info = parent.stat()
                    if info.st_uid == 0:
                        os.chown(parent, 0, runtime.gid)
                        os.chmod(parent, stat.S_IMODE(info.st_mode) | 0o050)


def check_resources(config, selected=None):
    selected = selected or load()
    if not selected.rootless:
        return
    path = Path('/sys/fs/cgroup/user.slice') / ('user-' + str(selected.uid) + '.slice') / ('user@' + str(selected.uid) + '.service/cgroup.controllers')
    controllers = set(path.read_text().split()) if path.is_file() else set()
    for service in config.get('services', {}).values():
        limits = service.get('deploy', {}).get('resources', {}).get('limits', {})
        required = set()
        if any(service.get(k) for k in ('cpus', 'cpu_shares', 'cpu_period', 'cpu_quota')) or limits.get('cpus'):
            required.add('cpu')
        if service.get('mem_limit') or service.get('memswap_limit') or limits.get('memory'):
            required.add('memory')
        if service.get('pids_limit') or limits.get('pids'):
            required.add('pids')
        if not required <= controllers:
            raise ValueError('Rootless resource controllers are not delegated: ' + ', '.join(sorted(required - controllers)))
        if service.get('storage_opt', {}).get('size'):
            raise ValueError('Writable-layer quotas are not supported by the rootless runtime')


def run(args, *, delegated=False, **kwargs):
    runtime = load()
    if not args or args[0] != 'docker':
        return subprocess.run(args, **kwargs)
    args = list(args)
    kwargs['env'] = runtime.environment(delegated)
    if runtime.rootless:
        if delegated:
            client = Path(kwargs['env']['DOCKER_CONFIG'])
            client.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(client, runtime.uid, runtime.gid)
        if len(args) > 1 and args[1] == 'compose':
            prepare_compose(args, runtime)
            if 'config' not in args:
                for index, word in enumerate(args[:-1]):
                    if word in ('-f', '--file') and Path(args[index + 1]).name == 'compose.runtime.json':
                        original = Path(args[index + 1])
                        value = json.loads(original.read_text())
                        for service in value.get('services', {}).values():
                            if service.get('ports') and service.get('deploy', {}).get('replicas', 1) != 1:
                                raise ValueError('Published ports require one service instance')
                            service.pop('ports', None)
                        networks = value.setdefault('networks', {'default': {}})
                        for name, network in networks.items():
                            if network is None:
                                network = networks[name] = {}
                            network.setdefault('driver_opts', {})['com.docker.network.bridge.gateway_mode_ipv4'] = 'nat-unprotected'
                        actual = original.with_name('.rootless-compose.json')
                        fd = os.open(actual, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'w') as output:
                            json.dump(value, output)
                        read_access(actual, runtime)
                        args[index + 1] = str(actual)
        kwargs.update(drop_options(runtime))
    with contextlib.ExitStack() as stack:
        # Root-owned backup paths are deliberately not made runtime-writable.
        if runtime.rootless and args[1:3] in (['image', 'save'], ['image', 'load']):
            option = '-o' if args[2] == 'save' else '-i'
            if option in args:
                index = args.index(option)
                filename = args[index + 1]
                del args[index:index + 2]
                descriptor = stack.enter_context(open(filename, 'wb' if option == '-o' else 'rb'))
                kwargs['stdout' if option == '-o' else 'stdin'] = descriptor
                if option == '-o' and kwargs.pop('capture_output', False):
                    kwargs['stderr'] = subprocess.PIPE
        return subprocess.run(args, **kwargs)


def exec_docker(args, delegated=False):
    runtime = load()
    if runtime.rootless and os.geteuid() == 0:
        os.setgroups([])
        os.setgid(runtime.gid)
        os.setuid(runtime.uid)
    elif runtime.rootless and os.geteuid() != runtime.uid:
        raise ValueError('Wrong runtime account')
    os.execvpe(args[0], args, runtime.environment(delegated))


def service(action, runner=subprocess.run):
    runtime = load()
    if runtime.rootless and action == 'restart':
        if os.geteuid() != 0:
            raise ValueError('Controlled runtime restart requires the host administrator')
        subprocess.run(['systemctl', 'stop', 'betterdocker-lifecycle.service'], check=True)
        result = service('stop')
        if result.returncode:
            return result
        result = service('start')
        if result.returncode:
            return result
        import host
        host.ready()
        subprocess.run([str(Path(__file__).with_name('firewall.py')), 'apply'], check=True)
        subprocess.run(['systemctl', 'start', 'betterdocker-lifecycle.service'], check=True)
        return result
    args = ['systemctl'] + (['--user'] if runtime.rootless else []) + [action, 'docker.service']
    return runner(args, env=runtime.environment(), text=True, capture_output=True, check=False,
                          **drop_options(runtime))


def request(method, path, body):
    runtime = load()
    if runtime.rootless and method == 'POST' and path.startswith('/containers/create'):
        body = dict(body, HostConfig=dict(body.get('HostConfig') or {}))
        body['HostConfig']['PortBindings'] = {}
    if method == 'POST' and path.startswith('/networks/create'):
        body = dict(body, Options=dict(body.get('Options') or {}))
        if runtime.rootless:
            body['Options']['com.docker.network.bridge.gateway_mode_ipv4'] = 'nat-unprotected'
    if runtime.rootless and os.geteuid() == 0:
        result = subprocess.run([sys.executable, str(Path(__file__)), 'engine', method, path],
                                input=json.dumps(body), text=True, capture_output=True, check=True,
                                env=runtime.environment(), **drop_options(runtime))
        return json.loads(result.stdout)
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(1800)
            self.sock.connect(runtime.socket)
    connection = Connection('localhost', timeout=1800)
    try:
        connection.request(method, path, body=json.dumps(body), headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        payload = response.read()
        if response.status not in (200, 201, 204):
            raise ValueError('Docker operation failed (HTTP ' + str(response.status) + '); see Task History')
        return json.loads(payload) if payload else {}
    finally:
        connection.close()


def assert_compatible(identity):
    runtime = load()
    if identity.get('mode', 'rootful') != runtime.mode:
        raise ValueError('Source and destination Docker runtime modes differ; use explicit runtime migration')
    if runtime.rootless and identity.get('count') != runtime.count:
        raise ValueError('Source and destination rootless mapping capacities differ')


def port_bindings(ports, address):
    result = {}
    for port in ports:
        def expand(value):
            fields = str(value).split('-')
            start, end = int(fields[0]), int(fields[-1])
            if len(fields) > 2 or not 1 <= start <= end <= 65535:
                raise ValueError('Invalid published port range')
            return list(range(start, end + 1))
        published, targets = expand(port['published']), expand(port['target'])
        if len(published) != len(targets):
            raise ValueError('Port ranges must have equal lengths')
        for public, target in zip(published, targets):
            result.setdefault(str(target) + '/' + port.get('protocol', 'tcp'), []).append({'HostIp': address, 'HostPort': str(public)})
    return result


@contextlib.contextmanager
def anchored(path):
    """Open every ancestor without following a runtime-controlled symlink."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Data path contains parent traversal')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def copy_data(source, destination, runner=subprocess.run):
    if not load().rootless:
        runner(['cp', '-a', '--reflink=auto', '--', str(source), str(destination)], check=True, timeout=86400)
        return
    with anchored(source) as fd:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if directory:
            Path(destination).mkdir(mode=0o700)
        subprocess.run(['cp', '-a', '--reflink=auto'] + ([] if directory else ['--dereference']) + ['--',
                        '/proc/self/fd/' + str(fd) + ('/.' if directory else ''), str(destination)], pass_fds=(fd,), check=True)


def data_directory(path):
    selected = load()
    if not selected.rootless:
        Path(path).mkdir(parents=True, exist_ok=True)
        return
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Data path contains parent traversal')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            created = False
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if created:
                os.fchown(next_fd, selected.uid, selected.gid)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def restore_data(source, destination, file=False, runner=subprocess.run):
    selected = load()
    if not selected.rootless:
        runner(['cp', '-a', '--', str(source) + ('' if file else '/.'), str(destination)], check=True, timeout=86400)
        return
    import host
    # Workload data is copied in the mapped user namespace, never as host root.
    with tempfile.TemporaryDirectory(prefix='import-data-', dir='/var/lib/betterdocker') as temporary:
        staged = Path(temporary) / 'data'
        subprocess.run(['cp', '-a', '--', str(source), str(staged)], check=True)
        remap_tree(staged, Runtime(), selected)
        read_access(temporary, selected)
        with contextlib.ExitStack() as stack:
            if file:
                data_directory(Path(destination).parent)
                parent = stack.enter_context(anchored(Path(destination).parent))
                dst = os.open(Path(destination).name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                stack.callback(os.close, dst)
                os.fchown(dst, selected.uid, selected.gid)
            else:
                data_directory(destination)
                dst = stack.enter_context(anchored(destination))
            src = stack.enter_context(anchored(staged))
            _, _, prefix, descriptors = stack.enter_context(host.namespace())
            subprocess.run(prefix + ['cp', '-a'] + (['--dereference'] if file else ['--remove-destination']) + ['--',
                           '/proc/self/fd/' + str(src) + ('' if file else '/.'),
                           '/proc/self/fd/' + str(dst) + ('' if file else '/.')],
                           pass_fds=(*descriptors, src, dst), check=True)


def remap_tree(path, source, destination):
    """Translate copied data owners, ACL IDs and namespaced file capabilities."""
    if source.identity() == destination.identity():
        return
    path = Path(path)
    def change(parent, name, expected=None):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if expected and (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError('Data root changed during ownership conversion')
        uid = destination.host_id(source.container_id(info.st_uid))
        gid = destination.host_id(source.container_id(info.st_gid, True), True)
        if not stat.S_ISLNK(info.st_mode):
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError('Portable data contains an unsupported special file')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                value = os.fstat(fd)
                if (value.st_dev, value.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('Data changed during ownership conversion')
                attrs = {key: os.getxattr(fd, key) for key in os.listxattr(fd)}
                for key in ('system.posix_acl_access', 'system.posix_acl_default'):
                    if key not in attrs:
                        continue
                    raw = bytearray(attrs[key])
                    if len(raw) < 4 or struct.unpack_from('<I', raw)[0] != 2 or (len(raw) - 4) % 8:
                        raise ValueError('Unsupported POSIX ACL format')
                    for offset in range(4, len(raw), 8):
                        tag, _, identifier = struct.unpack_from('<HHI', raw, offset)
                        if tag in (2, 8):
                            group = tag == 8
                            identifier = destination.host_id(source.container_id(identifier, group), group)
                            struct.pack_into('<I', raw, offset + 4, identifier)
                    attrs[key] = bytes(raw)
                if 'security.capability' in attrs:
                    raw = attrs['security.capability']
                    if len(raw) == 24 and struct.unpack_from('<I', raw)[0] & 0xff000000 == 0x03000000:
                        root_id = destination.host_id(source.container_id(struct.unpack_from('<I', raw, 20)[0]))
                        attrs['security.capability'] = raw[:20] + struct.pack('<I', root_id)
                os.fchown(fd, uid, gid)
                os.fchmod(fd, stat.S_IMODE(info.st_mode))
                for key, value in attrs.items():
                    os.setxattr(fd, key, value)
            finally:
                os.close(fd)
        else:
            os.chown(name, uid, gid, dir_fd=parent, follow_symlinks=False)
    with anchored(path.parent) as parent, anchored(path) as root:
        change(parent, path.name, os.fstat(root))
        if stat.S_ISDIR(os.fstat(root).st_mode):
            for _, dirs, files, parent in os.fwalk('.', dir_fd=root, follow_symlinks=False):
                for name in dirs + files:
                    change(parent, name)


def empty_data_root(path):
    selected = load()
    if not selected.rootless:
        return
    path = Path(path)
    if path.stat().st_uid != 0:
        return
    children = list(path.iterdir())
    if any(child.name != 'lost+found' or not child.is_dir() or child.is_symlink() or any(child.iterdir()) for child in children):
        raise ValueError('Data disk has unmapped data; use explicit runtime migration')
    os.chown(path, selected.uid, selected.gid)
    for child in children:
        os.chown(child, selected.uid, selected.gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('engine', 'identity', 'service', 'docker'))
    parser.add_argument('values', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == 'engine':
        print(json.dumps(request(*args.values, json.load(sys.stdin))))
    elif args.action == 'identity':
        print(json.dumps(load().identity()))
    elif args.action == 'docker':
        exec_docker(['docker', *args.values])
    else:
        result = service(*args.values)
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
