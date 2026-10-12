"""PVE storage bandwidth limits for streamed guest state."""
import json
import subprocess
import time


def bandwidth(operation, storages):
    from . import projects as w
    program = ('use PVE::Storage; use JSON; '
               'my $ids=decode_json($ARGV[1]); '
               'print encode_json(PVE::Storage::get_bandwidth_limit($ARGV[0],$ids));')
    limit = json.loads(w.capture(['perl', '-e', program, operation, json.dumps([value for value in storages if value])]))
    return int(limit or 0) * 1024


def send(command, archive, rate=0, timeout=86400):
    if not rate:
        return subprocess.run(command, stdin=archive, check=True, timeout=timeout)
    started = time.monotonic()
    sent = 0
    with subprocess.Popen(command, stdin=subprocess.PIPE) as child:
        try:
            while True:
                block = archive.read(min(65536, rate))
                if not block:
                    break
                child.stdin.write(block)
                child.stdin.flush()
                sent += len(block)
                while sent / rate > time.monotonic() - started:
                    if time.monotonic() - started >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    time.sleep(min(.2, sent / rate - (time.monotonic() - started)))
            child.stdin.close()
            code = child.wait(timeout=max(1, timeout - (time.monotonic() - started)))
            if code:
                raise subprocess.CalledProcessError(code, command)
        except BaseException:
            child.kill()
            child.wait()
            raise
