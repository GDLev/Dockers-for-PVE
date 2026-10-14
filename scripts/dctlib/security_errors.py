"""Keep deployment credentials out of helper errors and task progress."""
import re
import subprocess

_values = set()


def remember_environment(environment):
    if isinstance(environment, str):
        environment = dict(line.split('=', 1) for line in environment.splitlines()
                           if '=' in line and not line.lstrip().startswith('#'))
    for value in (environment or {}).values():
        if isinstance(value, str) and len(value.strip('"\'')) >= 4:
            _values.add(value.strip('"\''))


def redact(text):
    text = str(text)
    for value in sorted(_values, key=len, reverse=True):
        text = text.replace(value, '[redacted]')
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', text)
    return re.sub(r'(?i)\b(password|passwd|token|secret|authorization)\s*[:=]\s*([^\s,;]+)',
                  r'\1=[redacted]', text)


def public_error(error):
    if isinstance(error, subprocess.CalledProcessError):
        return 'External command failed with exit code ' + str(error.returncode)
    if isinstance(error, subprocess.TimeoutExpired):
        return 'External command timed out'
    return redact(error)

