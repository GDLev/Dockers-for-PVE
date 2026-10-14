"""Capacity checks and writable-history accounting at host I/O boundaries."""
import json
from pathlib import Path
import re
import shutil

RESERVE = 64 * 1024 * 1024


def require_space(path, required=0):
    path = Path(path)
    while not path.exists():
        path = path.parent
    if required < 0:
        raise ValueError('Invalid required storage capacity')
    free = shutil.disk_usage(path).free
    if required + RESERVE > free:
        raise ValueError('Insufficient free space for this operation; leave at least 64 MiB free')
    return free - RESERVE


def docker_space(required=0):
    from . import projects as w
    root = w.capture(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip()
    if not root or not Path(root).is_absolute():
        raise ValueError('Cannot determine Docker storage capacity')
    return require_space(root, required)


def preserve_quota(row, body):
    """Committed writable history remains charged to a container's disk budget."""
    from . import projects as w
    size = (body['HostConfig'].get('StorageOpt') or {}).get('size')
    maximum = w.disk_mib(size) * 1048576 if size else None
    labels = row['Config'].get('Labels') or {}
    raw = labels.get('betterdocker.preserved-bytes', '0')
    if not re.fullmatch(r'[0-9]+', raw):
        raise ValueError('Invalid preserved filesystem accounting')
    history = int(raw)
    base = labels.get('com.docker.compose.image')
    if maximum and not history and base and row['Image'] != base:
        current = json.loads(w.capture(['docker', 'image', 'inspect', row['Image']]))[0]['Size']
        original = json.loads(w.capture(['docker', 'image', 'inspect', base]))[0]['Size']
        history = max(0, current - original)
    writable = row.get('SizeRw', 0) if not maximum else row.get('SizeRw')
    if not isinstance(writable, int) or writable < 0:
        raise ValueError('Cannot measure the writable layer before preserving its quota')
    history += writable
    if maximum and history + 4096 >= maximum:
        raise ValueError('Increase the container disk limit before preserving its filesystem')
    body.setdefault('Labels', {})['betterdocker.preserved-bytes'] = str(history)
    if maximum:
        body['HostConfig']['StorageOpt']['size'] = str(maximum - history)
