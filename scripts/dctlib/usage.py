"""Guest storage categories; logical image sizes are never presented as disk quotas."""
from .secure_io import read_json
import json
import os
from pathlib import Path
import time


def measure(identifier, containers):
    from . import projects as w
    folder = w.ROOT / identifier
    path = folder / 'storage-usage.json'
    ids = sorted(row['id'] for row in containers)
    saved = read_json(path) if path.exists() else {}
    if saved.get('ids') == ids and time.time() - saved.get('time', 0) < 60:
        return saved['usage']
    rows = json.loads(w.capture(['docker', 'inspect', '--size', *ids])) if ids else []
    root = Path(w.capture(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip())
    images = sorted({row['Image'] for row in rows})
    image_rows = json.loads(w.capture(['docker', 'image', 'inspect', *images])) if images else []
    logs = 0
    for row in rows:
        log = Path(row.get('LogPath') or '/')
        if log.is_relative_to(root) and log.parent.is_dir():
            logs += sum(item.stat().st_blocks * 512 for item in log.parent.glob(log.name + '*') if item.is_file() and not item.is_symlink())
    snapshots = sum(read_json(path).get('size', 0) for path in w.snapshot_root(identifier).glob('*/manifest.json'))
    usage = {'writable': sum(row.get('SizeRw', 0) for row in rows),
        'preserved': sum(int((row['Config'].get('Labels') or {}).get('betterdocker.preserved-bytes', '0')) for row in rows),
        'images_logical': sum(image['Size'] for image in image_rows), 'logs': logs, 'snapshots': snapshots}
    w.write_json(path, {'time': int(time.time()), 'ids': ids, 'usage': usage})
    return usage
