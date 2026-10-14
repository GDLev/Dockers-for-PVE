"""Pinned, bounded reads of host control state. Application data uses runtime.anchored."""
import contextlib
import json
import hashlib
import hmac
import os
from pathlib import Path
import stat
import sys
import uuid

MAX_JSON = 32 * 1024 * 1024
KEY = Path('/etc/betterdocker/recovery.key')


def journal(path):
    path = Path(path)
    return path.name.endswith('-transaction.json') or path.name in (
        'network-reconcile.json', 'network-resume.json', 'transfer-cleanup.json') or path.parent.name == 'imports'


def seal(path, value):
    if not journal(path) or not KEY.exists():
        return value
    value = dict(value)
    value.pop('_integrity', None)
    payload = str(Path(path).absolute()) + '\n' + json.dumps(value, sort_keys=True, separators=(',', ':'))
    value['_integrity'] = hmac.new(read_text(KEY, 1024).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return value


def structure(value, maximum=100000, depth=64):
    pending = [(value, 0)]; count = 0
    while pending:
        item, level = pending.pop(); count += 1
        if count > maximum or level > depth:
            raise ValueError('Configuration exceeds object or nesting limits')
        if isinstance(item, dict):
            pending.extend((v, level + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, level + 1) for v in item)
    return value


def trusted(info, directory=False, owner=None):
    if info.st_uid not in (0, os.geteuid(), owner):
        raise ValueError('Host control path has an unexpected owner')
    if info.st_mode & 0o022 and not (directory and info.st_mode & stat.S_ISVTX):
        raise ValueError('Host control path is writable by others')


@contextlib.contextmanager
def anchored(path, directory=False, owner=None):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Host control path contains traversal')
    # macOS provides /var through a system-owned link. Linux production paths
    # have no such exception, including user-supplied links below /var/lib.
    if sys.platform == 'darwin' and path.parts[1:2] == ('var',):
        path = Path('/private/var').joinpath(*path.parts[2:])
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last or directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            trusted(info, directory=stat.S_ISDIR(info.st_mode), owner=owner)
        yield fd
    finally:
        os.close(fd)


def read_text(path, maximum=MAX_JSON, owner=None):
    with anchored(path, owner=owner) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('Invalid or oversized host control file')
        private = Path(path).name in ('.env', 'request.json', 'recovery.key') or (Path(path).name == 'config.json' and Path(path).parent.name in ('.docker', 'delegated-client'))
        if private and info.st_mode & 0o077:
            allowed = False
            if Path(path).name == '.env' and info.st_uid == 0 and info.st_mode & 0o077 == 0o040:
                import runtime
                selected = runtime.load()
                allowed = selected.rootless and info.st_gid == selected.gid
                if not allowed and info.st_gid == 0:
                    # Older snapshots copied the runtime-readable mode into
                    # a root-only configuration directory. Preserve readability
                    # for root while keeping the archived secrets inaccessible.
                    with anchored(Path(path).parent, directory=True) as parent:
                        parent_info = os.fstat(parent)
                        allowed = parent_info.st_uid == 0 and not parent_info.st_mode & 0o077
            if not allowed:
                raise ValueError('Credentials must be readable only by their owner or the selected runtime')
        chunks = []; size = 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
            if size > maximum:
                raise ValueError('Host control file exceeded its size limit')
        return b''.join(chunks).decode('utf-8')


def read_json(path, owner=None):
    try:
        value = structure(json.loads(read_text(path, owner=owner), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON number'))))
        if journal(path) and KEY.exists():
            if not isinstance(value, dict) or not isinstance(value.get('_integrity'), str):
                raise ValueError('Recovery journal is not authenticated; finish legacy recovery before upgrading')
            signature = value.pop('_integrity')
            if not hmac.compare_digest(signature, seal(path, value)['_integrity']):
                raise ValueError('Recovery journal integrity check failed')
        return value
    except (RecursionError, UnicodeError, json.JSONDecodeError):
        raise ValueError('Invalid host control JSON') from None


def atomic_text(path, text, public=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with anchored(path.parent, directory=True) as parent:
        temporary = '.betterdocker-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(text); output.flush()
                if not public:
                    os.fsync(output.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            if not public:
                os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
