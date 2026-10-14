"""Node-wide admission for expensive work, held by kernel locks."""
import contextlib
import contextvars
import fcntl
from functools import wraps
import os
from pathlib import Path
import stat
import uuid

from .secure_io import anchored, read_json, trusted

POLICY = Path('/etc/betterdocker/operation-policy.json')
ROOT = Path('/run/betterdocker/operation-slots')
MAXIMUM = 64
ENV = 'BETTERDOCKER_OPERATION_SLOT_FD'
_current = contextvars.ContextVar('betterdocker_operation_slot', default=None)


def maximum():
    value = read_json(POLICY) if POLICY.exists() else {'max_concurrent': 2}
    if not isinstance(value, dict) or set(value) != {'max_concurrent'} or type(value['max_concurrent']) is not int or not 1 <= value['max_concurrent'] <= MAXIMUM:
        raise ValueError('Operation policy requires max_concurrent between 1 and 64')
    return value['max_concurrent']


def descriptor():
    current = _current.get()
    return current[1] if current and current[0] == os.getpid() else None


def identity():
    token = os.pread(descriptor(), 32, 0).decode()
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Invalid operation reservation identity')
    return token


def open_lock(directory, name):
    # Darwin can report ENOENT when two openat(O_CREAT|O_NOFOLLOW)
    # calls create the same file. Retry only while the pinned directory exists.
    for attempt in range(3):
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            break
        except FileNotFoundError:
            if attempt == 2 or not os.fstat(directory).st_nlink:
                raise
    try:
        info = os.fstat(fd)
        trusted(info)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Invalid operation reservation file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def inherited(directory):
    raw = os.environ.get(ENV)
    if raw is None and _current.get() is not None:
        raw = str(_current.get()[1])
    if raw is None:
        return None
    try:
        fd = int(raw)
        info = os.fstat(fd)
        for index in range(MAXIMUM):
            expected = os.stat(str(index) + '.lock', dir_fd=directory, follow_symlinks=False)
            if (expected.st_dev, expected.st_ino) == (info.st_dev, info.st_ino):
                trusted(info)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    break
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
    except (ValueError, OSError):
        pass
    raise ValueError('Invalid inherited operation reservation')


@contextlib.contextmanager
def reserve():
    if descriptor() is not None:
        yield
        return
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with anchored(ROOT, directory=True) as directory:
        fd = inherited(directory)
        owned = fd is None
        if owned:
            ceiling = maximum()
            gate = open_lock(directory, 'allocation.lock')
            available = []
            fd = None
            try:
                fcntl.flock(gate, fcntl.LOCK_EX)
                occupied = 0
                # Scan all slots so reducing the policy also counts older work.
                for index in range(MAXIMUM):
                    candidate = open_lock(directory, str(index) + '.lock')
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        occupied += 1
                        os.close(candidate)
                    else:
                        available.append(candidate)
                if occupied >= ceiling:
                    raise ValueError('DCT operation limit reached on this node; retry after another task finishes')
                fd = available.pop(0)
                os.ftruncate(fd, 0)
                os.pwrite(fd, uuid.uuid4().hex.encode(), 0)
            finally:
                for unused in available:
                    os.close(unused)
                os.close(gate)
        token = _current.set((os.getpid(), fd))
        try:
            yield
        finally:
            _current.reset(token)
            if owned:
                # Do not explicitly unlock: inherited children retain admission
                # until their last descriptor closes, even if the worker exits.
                os.close(fd)


def bounded(function):
    @wraps(function)
    def call(*args, **kwargs):
        with reserve():
            return function(*args, **kwargs)
    return call


def expensive(args):
    return bool(args and args[0] == 'docker' and (
        len(args) > 1 and args[1] in ('pull', 'commit', 'save', 'load', 'build', 'import', 'export') or
        len(args) > 2 and args[1] == 'image' and args[2] in ('save', 'load', 'pull', 'build') or
        len(args) > 1 and args[1] == 'compose' and any(word in ('up', 'pull', 'build', 'create') for word in args[2:])))


def bounded_command(function):
    @wraps(function)
    def call(args, **kwargs):
        with reserve() if expensive(args) else contextlib.nullcontext():
            return function(args, **kwargs)
    return call
