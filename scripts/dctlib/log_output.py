"""Limit Docker log output before collecting it into the API response."""
import resource
import signal
import json
import tempfile

MAX_BYTES = 2 * 1024 * 1024


def bounded(command):
    from . import projects as w
    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = w.run(command, stdout=stdout, stderr=stderr, preexec_fn=limit, check=False)
        stdout.seek(0); stderr.seek(0)
        first = stdout.read(MAX_BYTES)
        second = stderr.read(MAX_BYTES - len(first))
        truncated = len(first) + len(second) >= MAX_BYTES or result.returncode == -signal.SIGXFSZ
        if result.returncode and not truncated:
            raise ValueError('Cannot read container logs')
        lines = sorted((first + second).decode('utf-8', errors='replace').splitlines())
        text = ('\n'.join(lines) + '\n').encode('utf-8')
        truncated = truncated or len(text) > MAX_BYTES
        suffix = b'\n[Log output truncated at 2 MiB]\n' if truncated else b''
        output = text[:MAX_BYTES - len(suffix)].decode('utf-8', errors='ignore')
        # JSON escaping can expand control characters and non-ASCII text.
        budget = MAX_BYTES - 32 - len(json.dumps(suffix.decode()))
        if len(json.dumps(output)) > budget:
            truncated = True
            suffix = b'\n[Log output truncated at 2 MiB]\n'
            budget = MAX_BYTES - 32 - len(json.dumps(suffix.decode()))
            low, high = 0, len(output)
            while low < high:
                middle = (low + high + 1) // 2
                if len(json.dumps(output[:middle])) <= budget:
                    low = middle
                else:
                    high = middle - 1
            output = output[:low]
        return output + suffix.decode()
