"""Opt-in node policy for bounded writes in hardened rootless guests."""
from pathlib import Path
import re
from .secure_io import read_json

POLICY = Path('/etc/betterdocker/filesystem-policy.json')


def required(selected, mode):
    value = read_json(POLICY) if POLICY.exists() else {'require_read_only_rootless': False}
    if not isinstance(value, dict) or set(value) != {'require_read_only_rootless'} or type(value['require_read_only_rootless']) is not bool:
        raise ValueError('Filesystem policy requires require_read_only_rootless as a boolean')
    return selected.rootless and mode == 'hardened' and value['require_read_only_rootless']


def positive_size(value):
    if type(value) is int:
        return value > 0
    return isinstance(value, str) and bool(re.fullmatch(r'[1-9][0-9]*(?:[kKmMgG](?:[iI]?[bB])?|[bB])?', value))


def managed(source, identifier, storage):
    from . import projects as w
    if not identifier or not isinstance(source, str):
        return False
    root = w.ROOT / w.project_id(identifier)
    path = Path(source)
    if not path.is_absolute() or '..' in path.parts:
        return False
    def bounded(disk):
        return isinstance(disk, dict) and bool(disk.get('volid')) and type(disk.get('size_mib')) is int and disk['size_mib'] > 0
    if bounded(storage) and path.is_relative_to(root / 'storage-data'):
        return True
    for key, disk in storage.get('additional', {}).items():
        if re.fullmatch(r'mp(?:[1-9]|1[0-5])', key) and bounded(disk) and path.is_relative_to(root / ('mount-' + key)):
            return True
    return False


def context(labels, storage):
    from . import projects as w
    identifier = (labels or {}).get('betterdocker.id')
    return identifier, storage if storage is not None else (w.storage_config(identifier) if identifier else {})


def mount(kind, source, readonly, size, identifier, storage, check_backing=False):
    if readonly:
        return
    if kind == 'tmpfs' and positive_size(size):
        return
    if kind == 'bind' and managed(source, identifier, storage):
        from . import projects as w
        w.safe_bind(source, identifier)
        if check_backing:
            backing(source, identifier, storage)
        return
    raise ValueError('Hardened rootless writes require a managed disk with a size limit or an explicitly sized tmpfs')


def config(value, selected, mode, storage=None, live=False):
    if not required(selected, mode):
        return value
    for service in value.get('services', {}).values():
        identifier, disks = context(service.get('labels'), storage)
        if service.get('read_only') is False:
            raise ValueError('Node policy requires a read-only root filesystem for hardened rootless DCTs')
        service['read_only'] = True
        service['pull_policy'] = 'never'
        for item in service.get('volumes', []):
            mount(item.get('type'), item.get('source'), item.get('read_only', False), (item.get('tmpfs') or {}).get('size'), identifier, disks, check_backing=live)
        tmpfs = service.get('tmpfs') or []
        for item in tmpfs if isinstance(tmpfs, list) else [tmpfs]:
            options = item.partition(':')[2]
            size = next((option[5:] for option in options.split(',') if option.startswith('size=')), None)
            mount('tmpfs', '', False, size, identifier, disks)
    return value


def host(body, selected, mode, storage=None, live=False, mounted=False):
    if not required(selected, mode):
        return body
    host = body.setdefault('HostConfig', {})
    if live and not host.get('ReadonlyRootfs'):
        raise ValueError('Container does not enforce the required read-only root filesystem; apply security policy before Start')
    host['ReadonlyRootfs'] = True
    identifier, disks = context(body.get('Labels') or body.get('Config', {}).get('Labels'), storage)
    targets = set()
    for item in host.get('Binds') or []:
        source, target, *options = item.split(':')
        mount('bind' if source.startswith('/') else 'volume', source, 'ro' in ','.join(options).split(',') and 'rw' not in ','.join(options).split(','), None, identifier, disks, check_backing=live or mounted)
        targets.add(target)
    for item in host.get('Mounts') or []:
        mount(item.get('Type'), item.get('Source'), item.get('ReadOnly', False), (item.get('TmpfsOptions') or {}).get('SizeBytes'), identifier, disks, check_backing=live or mounted)
        targets.add(item.get('Target'))
    for target, options in (host.get('Tmpfs') or {}).items():
        size = next((option[5:] for option in options.split(',') if option.startswith('size=')), None)
        mount('tmpfs', '', False, size, identifier, disks)
        targets.add(target)
    if live:
        for item in body.get('Mounts') or []:
            if item.get('Type') == 'tmpfs' and item.get('Destination') in targets:
                continue
            mount(item.get('Type'), item.get('Source'), not item.get('RW', True), None, identifier, disks, check_backing=True)
    volumes = body.get('Volumes', body.get('Config', {}).get('Volumes')) or {}
    if set(volumes) - targets:
        raise ValueError('Image-declared volumes require explicit bounded mounts under the rootless filesystem policy')
    return body


def compose_images(value, selected, mode):
    """Inspect image VOLUME declarations before Compose can allocate anonymous disks."""
    if not required(selected, mode):
        return {}
    config(value, selected, mode, live=True)
    import json
    from . import projects as w
    images = {}
    for name, service in value.get('services', {}).items():
        image = service.get('image')
        if not image or service.get('build'):
            raise ValueError('Rootless filesystem policy requires a prebuilt image')
        result = w.run(['docker', 'image', 'inspect', image], check=False, capture_output=True)
        if result.returncode:
            w.run(['docker', 'image', 'pull', image], timeout=1800, capture_output=True)
            result = w.run(['docker', 'image', 'inspect', image], capture_output=True)
        inspected = json.loads(result.stdout)[0]
        images[name] = image_id(inspected)
        volumes = inspected['Config'].get('Volumes') or {}
        targets = {item['target'] for item in service.get('volumes', [])}
        tmpfs = service.get('tmpfs') or []
        targets.update(item.split(':', 1)[0] for item in (tmpfs if isinstance(tmpfs, list) else [tmpfs]))
        if set(volumes) - targets:
            raise ValueError('Image-declared volumes require explicit bounded mounts under the rootless filesystem policy')
    return images


def image_id(inspected):
    value = inspected.get('Id', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('Inspected image identity is unavailable')
    return value


def engine_image(body, selected, mode):
    if not required(selected, mode):
        return
    import copy
    import json
    from . import projects as w
    image = json.loads(w.capture(['docker', 'image', 'inspect', body['Image']]))[0]
    candidate = copy.deepcopy(body)
    candidate['Volumes'] = dict(candidate.get('Volumes') or {}, **(image['Config'].get('Volumes') or {}))
    host(candidate, selected, mode, mounted=True)
    body['Image'] = image_id(image)


def backing(source, identifier, storage):
    """Reject an unmounted data directory instead of writing onto the host root disk."""
    import os
    from . import projects as w
    root = w.ROOT / identifier
    point = root / 'storage-data'
    disk = storage
    for key, candidate in storage.get('additional', {}).items():
        if re.fullmatch(r'mp(?:[1-9]|1[0-5])', key) and Path(source).is_relative_to(root / ('mount-' + key)):
            point, disk = root / ('mount-' + key), candidate
            break
    if not os.path.ismount(point):
        raise ValueError('Managed writable data disk is not mounted')
    expected = Path(w.pve_storage(identifier, 'inspect', volid=disk['volid'])['path'])
    device = w.capture(['findmnt', '-n', '-M', str(point), '-o', 'SOURCE']).strip()
    actual = device if expected.is_block_device() else w.capture(['losetup', '-n', '-O', 'BACK-FILE', device]).strip()
    if Path(actual).resolve() != expected.resolve():
        raise ValueError('Managed writable data disk has an unexpected backing device')
