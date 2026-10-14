"""PVE storage bandwidth limits for streamed guest state."""
import json
import subprocess
import time
import os
from .operation_capacity import bounded, descriptor, ENV


def bandwidth(operation, storages):
    from . import projects as w
    program = ('use PVE::Storage; use JSON; '
               'my $ids=decode_json($ARGV[1]); '
               'print encode_json(PVE::Storage::get_bandwidth_limit($ARGV[0],$ids));')
    limit = json.loads(w.capture(['perl', '-e', program, operation, json.dumps([value for value in storages if value])]))
    return int(limit or 0) * 1024


@bounded
def send(command, archive, rate=0, timeout=86400):
    slot = descriptor()
    child_options = {'pass_fds': (slot,), 'env': dict(os.environ, **{ENV: str(slot)})}
    if not rate:
        return subprocess.run(command, stdin=archive, check=True, timeout=timeout, **child_options)
    started = time.monotonic()
    sent = 0
    with subprocess.Popen(command, stdin=subprocess.PIPE, **child_options) as child:
        try:
            while True:
                block = archive.read(min(65536, rate))
                if not block:
                    break
                child.stdin.write(block)
                child.stdin.flush()
                sent += len(block)
                while True:
                    elapsed = time.monotonic() - started
                    delay = sent / rate - elapsed
                    if delay <= 0:
                        break
                    if elapsed >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    time.sleep(min(.2, delay))
            child.stdin.close()
            code = child.wait(timeout=max(1, timeout - (time.monotonic() - started)))
            if code:
                raise subprocess.CalledProcessError(code, command)
        except BaseException:
            child.kill()
            child.wait()
            raise
