"""Finite subprocess output, clean executable lookup and bounded child lifetime."""
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from .secure_io import anchored

PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
MAX_OUTPUT = 32 * 1024 * 1024


def executable(name):
    target = shutil.which(name, path=PATH) if '/' not in name else name
    if not target:
        raise FileNotFoundError('Required executable is unavailable: ' + Path(name).name)
    target = Path(target).absolute()
    with anchored(target.resolve()):
        pass
    # Multi-call programs such as iptables select their command from argv[0].
    # Validate the executable inode, but retain its installed invocation name.
    for path in (target, *target.parents):
        if path.is_symlink() and path.lstat().st_uid not in (0, os.geteuid()):
            raise ValueError('Executable link has an unexpected owner')
        with anchored(path.parent.resolve(), directory=True):
            pass
    return str(target)


def run(args, **kwargs):
    args = [executable(str(args[0])), *map(str, args[1:])]
    kwargs.setdefault('env', {'PATH': PATH, 'HOME': '/nonexistent'})
    limit = kwargs.pop('output_limit', MAX_OUTPUT)
    if not kwargs.get('capture_output') and kwargs.get('stdout') != subprocess.PIPE and kwargs.get('stderr') != subprocess.PIPE:
        return subprocess.run(args, **kwargs)
    capture = kwargs.pop('capture_output', False)
    if capture:
        if 'stdout' in kwargs or 'stderr' in kwargs:
            raise ValueError('capture_output conflicts with explicit streams')
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    check = kwargs.pop('check', False)
    timeout = kwargs.pop('timeout', 60)
    text = kwargs.pop('text', kwargs.pop('universal_newlines', False))
    encoding = kwargs.pop('encoding', None) or 'utf-8'
    errors = kwargs.pop('errors', None) or 'replace'
    data = kwargs.pop('input', None)
    source = None
    if data is not None:
        if 'stdin' in kwargs:
            raise ValueError('input conflicts with stdin')
        source = tempfile.TemporaryFile()
        source.write(data.encode(encoding) if isinstance(data, str) else data)
        source.seek(0); kwargs['stdin'] = source
    kwargs.setdefault('start_new_session', True)
    child = subprocess.Popen(args, **kwargs)
    selector = selectors.DefaultSelector(); output = {'stdout': [], 'stderr': []}; size = 0
    for key in output:
        stream = getattr(child, key)
        if stream is not None:
            selector.register(stream, selectors.EVENT_READ, key)
    deadline = time.monotonic() + timeout
    completed = False
    try:
        while selector.get_map() or child.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            for event, _ in selector.select(min(remaining, 0.2)):
                part = os.read(event.fileobj.fileno(), 65536)
                if not part:
                    selector.unregister(event.fileobj); event.fileobj.close(); continue
                size += len(part)
                if size > limit:
                    raise ValueError('External command exceeded its output limit')
                output[event.data].append(part)
        code = child.wait()
        completed = True
        values = {key: (b''.join(parts).decode(encoding, errors) if text else b''.join(parts))
                  if kwargs.get(key) == subprocess.PIPE else None for key, parts in output.items()}
        result = subprocess.CompletedProcess(args, code, **values)
        if check and code:
            raise subprocess.CalledProcessError(code, args, output=result.stdout, stderr=result.stderr)
        return result
    finally:
        if kwargs['start_new_session']:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif not completed and child.poll() is None:
            child.kill()
        if child.poll() is None:
            child.wait()
        selector.close()
        for stream in (child.stdout, child.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if source:
            source.close()
