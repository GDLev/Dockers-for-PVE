"""Additional managed disks share the primary disk's ownership and lifecycle."""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil


def records(settings):
    return settings.get('additional', {})


def volume_ids(settings):
    return list(dict.fromkeys(([settings['volid']] if settings.get('volid') else []) +
            [disk['volid'] for disk in records(settings).values()] + settings.get('unused', [])))


def source(identifier, key):
    if not re.fullmatch(r'mp(?:[1-9]|1[0-5])', key):
        raise ValueError('Additional disk names range from mp1 to mp15')
    from . import projects as w
    return w.ROOT / w.project_id(identifier) / ('mount-' + key)


def validate(value):
    if not isinstance(value, dict) or set(value) - {'service', 'target', 'storage', 'size_mib', 'read_only', 'backup', 'detach'}:
        raise ValueError('Invalid disk configuration')
    target = PurePosixPath(value.get('target', ''))
    if not target.is_absolute() or '..' in target.parts or str(target) == '/' or any(
            target == PurePosixPath(path) or target.is_relative_to(path) for path in ('/proc', '/sys', '/dev')):
        raise ValueError('Choose an application mount point outside /proc, /sys and /dev')
    size = value.get('size_mib')
    if type(size) is not int or not 256 <= size <= 1048576:
        raise ValueError('Disk size must be between 256 and 1048576 MiB')
    for field in ('read_only', 'backup', 'detach'):
        if value.get(field, False) not in (False, True, 0, 1):
            raise ValueError('Invalid disk ' + field)
    return dict(value, target=str(target), read_only=bool(value.get('read_only')), backup=bool(value.get('backup', True)))


def mount(identifier, key, disk, enable=True):
    from . import projects as w
    point = source(identifier, key)
    path = Path(w.pve_storage(identifier, 'inspect', volid=disk['volid'])['path'])
    options = 'nodev,nosuid' if path.is_block_device() else 'loop,nodev,nosuid'
    if os.path.ismount(point):
        device = w.capture(['findmnt', '-n', '-M', str(point), '-o', 'SOURCE']).strip()
        actual = device if path.is_block_device() else w.capture(['losetup', '-n', '-O', 'BACK-FILE', device]).strip()
        if Path(actual).resolve() != path.resolve():
            raise ValueError('Additional disk mount ownership changed')
    else:
        if point.is_symlink():
            raise ValueError('Additional disk mount crosses a symbolic link')
        point.mkdir(mode=0o700, exist_ok=True)
        w.run(['mount', '-o', options, str(path), str(point)])
    w.node_runtime.empty_data_root(point)
    unit = w.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
    if not re.fullmatch(r'[a-zA-Z0-9_.\\-]+\.mount', unit):
        raise ValueError('Invalid disk mount unit')
    (Path('/etc/systemd/system') / unit).write_text('[Unit]\nDescription=BetterDocker ' + key + '\nDefaultDependencies=no\nConflicts=umount.target\nBefore=umount.target '
        + w.node_runtime.load().system_unit + '\nAfter=betterdocker-addresses.service\n\n[Mount]\nWhat=' + str(path) + '\nWhere=' + str(point)
        + '\nType=ext4\nOptions=' + options + '\n\n[Install]\nRequiredBy=' + w.node_runtime.load().system_unit + '\n')
    w.run(['systemctl', 'daemon-reload'], capture_output=True)
    if enable:
        w.run(['systemctl', 'enable', unit], capture_output=True)


def unmount(identifier, key):
    from . import projects as w
    point = source(identifier, key)
    unit = w.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
    w.run(['systemctl', 'disable', unit], capture_output=True, check=False)
    w.run(['systemctl', 'daemon-reload'], capture_output=True)
    if os.path.ismount(point):
        w.run(['umount', str(point)])


def apply(config, identifier, settings):
    for key, disk in records(settings).items():
        if disk['service'] not in config['services']:
            raise ValueError('Detach disk ' + key + ' before removing its service')
        volumes = config['services'][disk['service']].setdefault('volumes', [])
        volumes[:] = [m for m in volumes if m.get('target') != disk['target']]
        volumes.append({'type': 'bind', 'source': str(source(identifier, key)), 'target': disk['target'], 'read_only': disk.get('read_only', False)})
    return config


def status(identifier, settings):
    result = []
    for key, disk in records(settings).items():
        point = source(identifier, key)
        item = dict(disk, name=key, mounted=os.path.ismount(point))
        if item['mounted']:
            fs = os.statvfs(point)
            item.update(used=(fs.f_blocks - fs.f_bfree) * fs.f_frsize, available=fs.f_bavail * fs.f_frsize)
        result.append(item)
    return result


def _configure(identifier, node, key, values, digest=None):
    from . import projects as w
    from .service_recreate import replace_bindings
    with w.locked(identifier):
        info = w.detail(identifier, node)
        w.assert_configuration_digest(identifier, node, digest)
        w.require_stopped(w.container_records(identifier))
        if info.get('ha', {}).get('managed') or info.get('template'):
            raise ValueError('Remove HA management and clone templates before editing disks')
        point = source(identifier, key)
        settings = w.storage_config(identifier)
        if not settings.get('volid'):
            raise ValueError('Create the project data disk before adding mount points')
        before = copy.deepcopy(settings)
        old = records(settings).get(key)
        if values.get('detach'):
            if not old:
                raise ValueError('Disk does not exist')
            w.check_protection(info)
            if any(w.snapshot_root(identifier).glob('*/manifest.json')):
                raise ValueError('Remove snapshots before detaching a disk')
            settings['additional'].pop(key)
            settings.setdefault('unused', []).append(old['volid'])
        else:
            values = validate(values)
            runtime = json.loads((w.ROOT / identifier / 'compose.runtime.json').read_text())
            if values.get('service') not in runtime['services']:
                raise ValueError('Select an existing service')
            if any(disk['service'] == values['service'] and disk['target'] == values['target'] for name, disk in records(settings).items() if name != key):
                raise ValueError('A disk already uses this mount point')
            if old and values['size_mib'] < old['size_mib']:
                raise ValueError('Disks cannot be shrunk')
            new = old is None or values.get('storage') and values['storage'] != old.get('storage')
            if new:
                previous_data = point if old else None
                if not old:
                    rows = w.snapshot_containers(identifier)
                    owned = {entry['source'] for entry in w.snapshot_mounts(identifier, rows)}
                    previous_data = next((Path(mount['Source']) for row in rows if row['Config']['Labels'].get('com.docker.compose.service') == values['service']
                        for mount in row.get('Mounts', []) if mount.get('Destination') == values['target'] and mount.get('RW', True) and mount['Source'] in owned), None)
                    if previous_data and not previous_data.is_dir():
                        raise ValueError('Managed disks require directory mount points')
                if previous_data:
                    needed = int(w.capture(['du', '-sx', '-B1', str(previous_data)]).split()[0])
                    if needed + 64 * 1048576 > values['size_mib'] * 1048576 * .9:
                        raise ValueError('Disk is too small for the current mount data')
                disk = w.pve_storage(identifier, 'allocate', storage=values['storage'], size_mib=values['size_mib'])
                w.run(['mkfs.ext4', '-q', '-m', '0', disk['path']])
                disk.pop('path', None)
                if previous_data:
                    import tempfile
                    with tempfile.TemporaryDirectory(prefix='disk-copy-', dir=w.ROOT.parent) as temporary:
                        staging = Path(temporary) / 'disk'; staging.mkdir()
                        device = w.pve_storage(identifier, 'inspect', volid=disk['volid'])['path']
                        w.run(['mount', '-o', 'nodev,nosuid' if Path(device).is_block_device() else 'loop,nodev,nosuid', device, str(staging)])
                        try:
                            from .transfer_io import bandwidth
                            w.node_runtime.copy_data(previous_data, staging, runner=w.run, contents=True,
                                rate_kib=bandwidth('move', [old.get('storage') if old else before.get('storage'), values['storage']]) // 1024)
                        finally:
                            w.run(['umount', str(staging)])
                    if old:
                        unmount(identifier, key)
                        settings.setdefault('unused', []).append(old['volid'])
            else:
                disk = dict(old)
                if values['size_mib'] > old['size_mib']:
                    inspected = w.pve_storage(identifier, 'resize', volid=old['volid'], size_mib=values['size_mib'])
                    device = w.capture(['findmnt', '-n', '-M', str(point), '-o', 'SOURCE']).strip()
                    if not Path(inspected['path']).is_block_device():
                        w.run(['losetup', '-c', device])
                    w.run(['resize2fs', device], timeout=1800)
            disk.update(values)
            settings.setdefault('additional', {})[key] = disk
            # Ownership is recorded before mount inspection, including a moved volume.
            w.write_json(w.ROOT / identifier / 'storage.json', settings)
            mount(identifier, key, disk)
            w.write_json(w.ROOT / identifier / 'storage.json', before)
        runtime = json.loads((w.ROOT / identifier / 'compose.runtime.json').read_text())
        for service in runtime['services'].values():
            service['volumes'] = [m for m in service.get('volumes', []) if m.get('source') != str(point)]
        apply(runtime, identifier, settings)
        journal = json.loads((w.ROOT / identifier / 'disk-transaction.json').read_text())
        journal['phase'] = 'committed'
        replace_bindings(info, candidate=runtime, storage=settings, updates={'storage.json': json.dumps(settings), 'disk-transaction.json': json.dumps(journal)})
        if values.get('detach'):
            unmount(identifier, key)
        (w.ROOT / identifier / 'allocated-volume.json').unlink(missing_ok=True)
        w.refresh(node)


def recover(identifier):
    from . import projects as w
    directory = w.ROOT / identifier
    marker = directory / 'disk-transaction.json'
    if not marker.exists():
        return
    state = json.loads(marker.read_text())
    key = state['key']
    source(identifier, key)
    if state['phase'] == 'committed':
        for name, disk in records(w.storage_config(identifier)).items():
            mount(identifier, name, disk)
        marker.unlink()
        return
    w.require_stopped(w.container_records(identifier))
    current = w.storage_config(identifier)
    pending = directory / 'allocated-volume.json'
    owned = set(volume_ids(current))
    if pending.exists():
        owned.add(json.loads(pending.read_text())['volid'])
    before = state['before']
    before.setdefault('unused', []).extend(sorted(owned - set(volume_ids(before))))
    unmount(identifier, key)
    w.write_json(directory / 'storage.json', before)
    if key in records(before):
        disk = before['additional'][key]
        actual = w.pve_storage(identifier, 'inspect', volid=disk['volid'])
        disk['size_mib'] = actual['size_mib']
        w.write_json(directory / 'storage.json', before)
        mount(identifier, key, disk)
        device = w.capture(['findmnt', '-n', '-M', str(source(identifier, key)), '-o', 'SOURCE']).strip()
        w.run(['resize2fs', device], timeout=1800)
    from .service_recreate import durable_text
    durable_text(directory / 'compose.runtime.json', state['runtime'])
    pending.unlink(missing_ok=True)
    marker.unlink()


def configure(identifier, node, key, values, digest=None):
    from . import projects as w
    with w.operation(identifier, 'settings'), w.locked(identifier):
        w.detail(identifier, node)
        w.assert_configuration_digest(identifier, node, digest)
        source(identifier, key)
        w.require_stopped(w.container_records(identifier))
        directory = w.ROOT / identifier
        w.write_json(directory / 'disk-transaction.json', {'key': key, 'phase': 'preparing',
            'before': w.storage_config(identifier), 'runtime': (directory / 'compose.runtime.json').read_text()})
        try:
            _configure(identifier, node, key, values, digest)
        except Exception:
            recover(identifier)
            raise
        recover(identifier)
