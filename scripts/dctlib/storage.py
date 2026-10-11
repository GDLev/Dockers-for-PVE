"""Managed PVE disks and writable-layer quotas."""
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import runtime as node_runtime


def rootfs_quota_support():
    """Do not advertise Docker size quotas on an unsupported backing filesystem."""
    if node_runtime.load().rootless:
        return {'supported': False, 'reason': 'Writable-layer quotas are not supported by the rootless runtime'}
    cache = worker.ROOT.parent / 'rootfs-quota.json'
    if cache.exists() and time.time() - cache.stat().st_mtime < 60:
        return json.loads(cache.read_text())
    result = {'supported': False, 'reason': 'Requires overlay2 on XFS with project quotas'}
    try:
        info = json.loads(worker.capture(['docker', 'info', '--format', '{{json .}}']))
        fs = json.loads(worker.capture(['findmnt', '-J', '-T', info['DockerRootDir'], '-o', 'FSTYPE,OPTIONS']))['filesystems'][0]
        options = set(fs['options'].split(','))
        result['supported'] = info.get('Driver') == 'overlay2' and fs['fstype'] == 'xfs' and bool(options & {'pquota', 'prjquota'})
        result['driver'] = info.get('Driver', '')
        if result['supported']:
            result['reason'] = ''
        worker.write_json(cache, result)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return result


def disk_mib(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(?:i?b)?', str(value), re.I)
    if not match:
        return 0
    return int(float(match[1]) * (1024 ** (' kmgt'.index(match[2].lower()))) / 1048576)


def storage_config(identifier):
    path = worker.ROOT / worker.project_id(identifier) / 'storage.json'
    return json.loads(path.read_text()) if path.exists() else {}


def pve_storage(identifier, action, **values):
    payload = dict(values, id=worker.project_id(identifier), action=action)
    if worker.METADATA != Path('/etc/pve/betterdocker'):
        payload['metadata'] = str(worker.METADATA)
    try:
        result = worker.run(['perl', str(worker.HELPERS / 'storage.pl')], input=json.dumps(payload),
                     capture_output=True, timeout=1800)
    except subprocess.CalledProcessError as error:
        raise ValueError('PVE storage: ' + (error.stderr or 'operation failed').strip()) from error
    return json.loads(result.stdout.strip().splitlines()[-1])


def storage_device(identifier, settings=None):
    settings = settings or worker.storage_config(identifier)
    if settings.get('volid'):
        return Path(worker.pve_storage(identifier, 'inspect', volid=settings['volid'])['path'])
    return worker.ROOT / identifier / 'storage.ext4'


def storage_unit(identifier, settings):
    point = worker.ROOT / identifier / 'storage-data'
    image = worker.storage_device(identifier, settings)
    unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
    if not re.fullmatch(r'[a-zA-Z0-9_.\\-]+\.mount', unit):
        raise ValueError('Invalid storage mount unit')
    options = 'nodev,nosuid' if image.is_block_device() else 'loop,nodev,nosuid'
    (Path('/etc/systemd/system') / unit).write_text(
        '[Unit]\nDescription=BetterDocker data disk\nDefaultDependencies=no\nConflicts=umount.target\nBefore=umount.target '
        + node_runtime.load().system_unit + '\nAfter=betterdocker-addresses.service\n'
        '\n[Mount]\nWhat=' + str(image) + '\nWhere=' + str(point) + '\nType=ext4\nOptions=' + options +
        '\n\n[Install]\nRequiredBy=docker.service\n'.replace('docker.service', node_runtime.load().system_unit))
    worker.run(['systemctl', 'daemon-reload'], capture_output=True)
    worker.run(['systemctl', 'enable', unit], capture_output=True)


def storage_unmount(identifier):
    point = worker.ROOT / identifier / 'storage-data'
    unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
    # Drop this disk's Requires link before stopping its mount unit.
    # Otherwise systemd can stop the node's Docker daemon as a dependency.
    worker.run(['systemctl', 'disable', unit], check=False, capture_output=True)
    worker.run(['systemctl', 'daemon-reload'], capture_output=True)
    worker.run(['systemctl', 'stop', unit], check=False, capture_output=True)
    if os.path.ismount(point):
        worker.run(['umount', str(point)])


def recover_storage_transaction(identifier):
    folder = worker.ROOT / identifier
    journal = folder / 'storage-transaction.json'
    if not journal.exists():
        if (folder / 'allocated-volume.json').exists():
            raise ValueError('Interrupted allocation: inspect allocated-volume.json before retrying')
        return
    state = json.loads(journal.read_text())
    daemon_active = node_runtime.service('is-active', runner=worker.run).returncode == 0
    if daemon_active:
        worker.require_stopped(worker.container_records(identifier))
    if state['operation'] == 'grow':
        settings = state['before']
        actual = worker.pve_storage(identifier, 'inspect', volid=settings['volid'])
        worker.storage_mount(identifier)
        device = worker.capture(['findmnt', '-n', '-M', str(folder / 'storage-data'), '-o', 'SOURCE']).strip()
        if not Path(actual['path']).is_block_device():
            worker.run(['losetup', '-c', device])
        worker.run(['resize2fs', device], timeout=1800)
        settings['size_mib'] = actual['size_mib']
        worker.write_json(folder / 'storage.json', settings)
    elif state.get('committed'):
        worker.storage_unit(identifier, worker.storage_config(identifier))
        info = json.loads((worker.METADATA / (identifier + '.json')).read_text())
        if state.get('reconcile'):
            (folder / 'snapshot-restored.json').unlink(missing_ok=True)
            worker.write_json(folder / 'network-reconcile.json', {'storage': True})
            if daemon_active:
                worker.reconcile_network_runtime(info)
    else:
        transfer = folder / 'storage-transfer'
        if os.path.ismount(transfer):
            worker.run(['umount', str(transfer)])
        if transfer.exists() and not any(transfer.iterdir()):
            transfer.rmdir()
        # Copy operations never alter the source disk. Restore its config first.
        worker.storage_unmount(identifier)
        if state['before']:
            worker.write_json(folder / 'storage.json', state['before'])
            worker.storage_mount(identifier)
            worker.storage_unit(identifier, state['before'])
        else:
            (folder / 'storage.json').unlink(missing_ok=True)
            point = folder / 'storage-data'
            if point.exists() and not any(point.iterdir()):
                point.rmdir()
        (folder / 'compose.runtime.json').write_text(state['runtime'])
        allocation = folder / 'allocated-volume.json'
        if allocation.exists():
            volid = json.loads(allocation.read_text())['volid']
            worker.pve_storage(identifier, 'free', volid=volid)
    (folder / 'allocated-volume.json').unlink(missing_ok=True)
    journal.unlink()


def project_pve_storage(identifier, node, size_mib, digest, storage):
    size_mib = int(size_mib)
    if not 256 <= size_mib <= 1048576:
        raise ValueError('Data disk must be between 256 and 1048576 MiB')
    with worker.locked(identifier):
        info = worker.detail(identifier, node)
        worker.require_stopped(worker.container_records(identifier))
        worker.recover_storage_transaction(identifier)
        worker.assert_configuration_digest(identifier, node, digest)
        worker.check_protection(info, "can't change DCT data disk")
        if any(worker.snapshot_root(identifier).glob('*/manifest.json')):
            raise ValueError('Remove snapshots before resizing or moving the data disk')
        folder = worker.ROOT / identifier
        before = worker.storage_config(identifier)
        source_storage = before.get('storage', '')
        storage = storage or source_storage
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', storage):
            raise ValueError('Select a PVE storage')
        journal = folder / 'storage-transaction.json'
        if before.get('volid') and storage == source_storage:
            if size_mib <= before['size_mib']:
                raise ValueError('Disks may only grow')
            worker.write_json(journal, {'operation': 'grow', 'before': before})
            worker.pve_storage(identifier, 'resize', volid=before['volid'], size_mib=size_mib)
            worker.recover_storage_transaction(identifier)
            return
        if before and size_mib < before['size_mib']:
            raise ValueError('Disks cannot shrink during a move')
        runtime = folder / 'compose.runtime.json'
        original = runtime.read_text()
        config = json.loads(original)
        if before:
            worker.storage_mount(identifier)
            sources = {key: Path(value) for key, value in before['mapping'].items()}
        else:
            sources = {}
            for service in config['services'].values():
                for mount in service.get('volumes', []):
                    if mount.get('read_only') or mount.get('type') == 'tmpfs':
                        continue
                    kind, source = mount.get('type'), mount.get('source', '')
                    if kind == 'volume' and not source:
                        raise ValueError('Use named volumes before moving application data to a managed disk')
                    if kind == 'bind':
                        path = Path(source)
                        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or path.resolve() == folder.resolve() or path.exists() and not path.is_dir():
                            raise ValueError('Only private project data directories can be migrated')
                    elif kind == 'volume':
                        definition = config.get('volumes', {}).get(source, {})
                        name = definition.get('name', 'bd-' + identifier + '_' + source)
                        volume = json.loads(worker.capture(['docker', 'volume', 'inspect', name]))[0]
                        owned = {row['id'] for row in worker.container_records(identifier)}
                        users = set(worker.capture(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'volume=' + name]).split())
                        if definition.get('external') or definition.get('driver_opts') or volume.get('Driver') != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier or users - owned:
                            raise ValueError('External or shared volumes cannot be migrated')
                        path = Path(volume['Mountpoint'])
                    else:
                        raise ValueError('Unsupported writable mount')
                    sources[kind + ':' + source] = path
        if not sources:
            raise ValueError('No writable data directories or volumes')
        mounted = json.loads(worker.capture(['findmnt', '-J', '-l', '-o', 'TARGET']))['filesystems']
        for source in sources.values():
            if any(Path(item['target']).is_relative_to(source.resolve()) and Path(item['target']) != source.resolve() for item in mounted):
                raise ValueError('Nested filesystems cannot be migrated')
        needed = sum(int(worker.capture(['du', '-sx', '-B1', str(path)]).split()[0]) for path in set(sources.values()) if path.exists())
        if needed + 64 * 1048576 > size_mib * 1048576 * .9:
            raise ValueError('Data disk is too small for existing data')
        # Copy a mounted source into a private staging directory before switching mounts.
        staging = folder / 'storage-transfer'
        if staging.exists():
            raise ValueError('An unfinished disk copy exists')
        staging.mkdir(mode=0o700)
        state = {'operation': 'copy', 'before': before, 'runtime': original}
        worker.write_json(journal, state)
        try:
            allocated = worker.pve_storage(identifier, 'allocate', storage=storage, size_mib=size_mib)
            state['target'] = allocated
            worker.write_json(journal, state)
            device = Path(allocated['path'])
            worker.run(['mkfs.ext4', '-q', '-m', '0', str(device)])
            options = 'nodev,nosuid' if device.is_block_device() else 'loop,nodev,nosuid'
            worker.run(['mount', '-o', options, str(device), str(staging)])
            node_runtime.empty_data_root(staging)
            mapping = {}
            for index, (key, source) in enumerate(sources.items()):
                target = staging / ('data-' + str(index))
                if node_runtime.load().rootless and source.exists():
                    node_runtime.copy_data(source, target)
                elif node_runtime.load().rootless:
                    node_runtime.data_directory(target)
                else:
                    target.mkdir()
                if source.exists() and not node_runtime.load().rootless:
                    worker.run(['cp', '-a', '--', str(source) + '/.', str(target)], timeout=86400)
                mapping[key] = str(folder / 'storage-data' / target.name)
            worker.run(['umount', str(staging)])
            worker.storage_unmount(identifier)
            allocated.pop('path', None)
            allocated.update(vmid=info['vmid'], node=node, mapping=mapping, unused=before.get('unused', []) + ([before['volid']] if before.get('volid') else []))
            worker.write_json(folder / 'storage.json', allocated)
            worker.storage_mount(identifier)
            # Existing runtime contains mapped source paths, so replace those explicitly.
            for service in config['services'].values():
                for mount in service.get('volumes', []):
                    key = mount.get('type', '') + ':' + mount.get('source', '')
                    if before:
                        key = next((k for k, v in before['mapping'].items() if v == mount.get('source')), key)
                    if key in mapping:
                        mount.update(type='bind', source=mapping[key])
                        mount.pop('volume', None); mount.pop('bind', None)
            worker.write_json(runtime, config)
            state.update(committed=True, reconcile=config != json.loads(original))
            worker.write_json(journal, state)
            worker.storage_unit(identifier, allocated)
            if state['reconcile']:
                (folder / 'snapshot-restored.json').unlink(missing_ok=True)
                worker.write_json(folder / 'network-reconcile.json', {'storage': True})
                worker.reconcile_network_runtime(info)
            (folder / 'allocated-volume.json').unlink(missing_ok=True)
            journal.unlink()
        except Exception:
            if os.path.ismount(staging):
                worker.run(['umount', str(staging)])
            worker.recover_storage_transaction(identifier)
            raise
        finally:
            if staging.exists() and not os.path.ismount(staging):
                staging.rmdir()
        print('Data disk ready: ' + allocated['volid'] + '. Previous disk and original sources retained.')


def storage_mount(identifier):
    folder = worker.ROOT / worker.project_id(identifier)
    config = worker.storage_config(identifier)
    if not config:
        return
    mountpoint = folder / 'storage-data'
    image = worker.storage_device(identifier, config)
    # Mount by exact owned image; fail closed if an unrelated mount occupies the path.
    if os.path.ismount(mountpoint):
        device = worker.capture(['findmnt', '-n', '-M', str(mountpoint), '-o', 'SOURCE']).strip()
        backing = device if image.is_block_device() else worker.capture(['losetup', '-n', '-O', 'BACK-FILE', device]).strip()
        if Path(backing).resolve() != image.resolve():
            raise ValueError('Project storage mount is occupied by another filesystem')
        node_runtime.empty_data_root(mountpoint)
        return
    mountpoint.mkdir(mode=0o700, exist_ok=True)
    options = 'nodev,nosuid' if image.is_block_device() else 'loop,nodev,nosuid'
    worker.run(['mount', '-o', options, str(image), str(mountpoint)])
    node_runtime.empty_data_root(mountpoint)


def apply_storage(config, identifier):
    settings = worker.storage_config(identifier)
    if settings:
        worker.storage_mount(identifier)
        mapping = settings['mapping']
        for service in config.get('services', {}).values():
            for mount in service.get('volumes', []):
                key = mount.get('type', '') + ':' + mount.get('source', '')
                if key in mapping:
                    mount.update(type='bind', source=mapping[key])
                    mount.pop('volume', None)
                    mount.pop('bind', None)
                elif not mount.get('read_only') and mount.get('type') != 'tmpfs' and mount.get('source') not in mapping.values():
                    raise ValueError('New writable mounts require a separate data migration; existing data disk cannot be bypassed')
    return config


def project_storage_status(identifier):
    settings = worker.storage_config(identifier)
    result = {'limit_mib': settings.get('size_mib', 0), 'rootfs': worker.rootfs_quota_support(), 'mounted': False, 'volid': settings.get('volid', ''), 'storage': settings.get('storage', ''),
              'native_snapshots': settings.get('native_snapshots', False), 'unused': settings.get('unused', [])}
    if settings:
        point = worker.ROOT / identifier / 'storage-data'
        result['mounted'] = os.path.ismount(point)
        if result['mounted']:
            fs = os.statvfs(point)
            result.update(used=(fs.f_blocks - fs.f_bfree) * fs.f_frsize, available=fs.f_bavail * fs.f_frsize)
    return result


def purge_retained_storage(identifier):
    """Explicit root maintenance after guest deletion; never called by DELETE DCT."""
    worker.project_id(identifier)
    with worker.locked(identifier):
        if (worker.METADATA / (identifier + '.json')).exists():
            raise ValueError('Remove the DCT before permanently freeing retained disks')
        if any(worker.snapshot_root(identifier).glob('*/manifest.json')):
            raise ValueError('Remove snapshots before freeing retained disks')
        settings = worker.storage_config(identifier)
        if not settings.get('volid'):
            raise ValueError('No retained PVE disks')
        worker.storage_unmount(identifier)
        for volid in settings.get('unused', []) + [settings['volid']]:
            worker.pve_storage(identifier, 'free', volid=volid)
            if volid in settings.get('unused', []):
                settings['unused'].remove(volid)
                worker.write_json(worker.ROOT / identifier / 'storage.json', settings)
        unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(worker.ROOT / identifier / 'storage-data')]).strip()
        (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
        worker.run(['systemctl', 'daemon-reload'], capture_output=True)
        (worker.ROOT / identifier / 'storage.json').unlink()
        worker.release_id(identifier)
        print('Retained PVE disks permanently removed. Original migration sources remain.')


def project_storage_limit(identifier, node, size_mib, digest=None, storage=''):
    if storage or worker.storage_config(identifier).get('volid'):
        return worker.project_pve_storage(identifier, node, size_mib, digest, storage)
    size_mib = int(size_mib)
    if not 256 <= size_mib <= 1048576:
        raise ValueError('Project data disk must be between 256 and 1048576 MiB')
    with worker.locked(identifier):
        worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        if any(item['state'] not in ('created', 'exited', 'dead') for item in worker.container_records(identifier)):
            raise ValueError('Stop all project services before changing the data disk')
        folder = worker.ROOT / identifier
        previous = worker.storage_config(identifier)
        if not previous:
            worker.check_protection(worker.detail(identifier, node), "can't replace DCT data mounts")
        if previous and size_mib <= previous['size_mib']:
            raise ValueError('Data disks can only be enlarged; shrinking or removing a disk is not supported')
        image, point = folder / 'storage.ext4', folder / 'storage-data'
        if previous:
            worker.storage_mount(identifier)
            with image.open('r+b') as handle:
                handle.truncate(size_mib * 1048576)
            device = worker.capture(['findmnt', '-n', '-M', str(point), '-o', 'SOURCE']).strip()
            worker.run(['losetup', '-c', device])
            worker.run(['resize2fs', device], timeout=1800)
            previous['size_mib'] = size_mib
            worker.write_json(folder / 'storage.json', previous)
            return
        runtime = folder / 'compose.runtime.json'
        original = runtime.read_text()
        config = json.loads(original)
        mapping, sources = {}, {}
        for service in config['services'].values():
            for mount in service.get('volumes', []):
                if mount.get('read_only') or mount.get('type') == 'tmpfs':
                    continue
                kind, source = mount.get('type'), mount.get('source', '')
                key = str(kind) + ':' + source
                if kind == 'bind':
                    path = Path(source)
                    # Do not migrate arbitrary host paths, sockets or configuration files.
                    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or path.resolve() == folder.resolve():
                        raise ValueError('Data disk requires project-owned directory mounts; external host paths are not migrated')
                    if path.exists() and not path.is_dir():
                        raise ValueError('Data disk supports directory mounts only')
                elif kind == 'volume':
                    definition = config.get('volumes', {}).get(source, {})
                    if definition.get('external') or definition.get('driver_opts'):
                        raise ValueError('External or custom-driver volumes cannot be migrated to the data disk')
                    volume_name = definition.get('name', 'bd-' + identifier + '_' + source)
                    volume = json.loads(worker.capture(['docker', 'volume', 'inspect', volume_name]))[0]
                    if volume.get('Driver') != 'local' or volume.get('Options') or volume.get('Labels', {}).get('com.docker.compose.project') != 'bd-' + identifier:
                        raise ValueError('Only local volumes owned by this Compose project can be migrated')
                    path = Path(volume['Mountpoint'])
                    users = worker.capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + volume_name]).split()
                    owned = {item['id'] for item in worker.container_records(identifier)}
                    if any(item not in owned for item in users):
                        raise ValueError('A volume is shared with another project')
                else:
                    raise ValueError('Unsupported writable mount')
                sources[key] = path
        if not sources:
            raise ValueError('Project has no writable data directory or local volume')
        if image.exists() or point.exists():
            raise ValueError('An unfinished storage migration exists; inspect it before retrying')
        needed = sum(int(worker.capture(['du', '-sx', '-B1', str(path)]).split()[0]) for path in set(sources.values()) if path.exists())
        if needed + 64 * 1048576 > size_mib * 1048576 * 0.9:
            raise ValueError('Data disk is too small for the current data and filesystem overhead')
        for command in ('mkfs.ext4', 'mount', 'losetup', 'resize2fs', 'findmnt', 'cp', 'du', 'systemd-escape', 'systemctl'):
            if not shutil.which(command):
                raise ValueError('Missing storage tool: ' + command)
        # Original sources remain intact as a recovery copy. Runtime is switched only after copying succeeds.
        with image.open('xb') as handle:
            os.chmod(image, 0o600)
            handle.truncate(size_mib * 1048576)
        mounted = False
        try:
            worker.run(['mkfs.ext4', '-q', '-m', '0', str(image)])
            point.mkdir(mode=0o700)
            worker.run(['mount', '-o', 'loop,nodev,nosuid', str(image), str(point)])
            mounted = True
            node_runtime.empty_data_root(point)
            for index, (key, path) in enumerate(sources.items()):
                target = point / ('data-' + str(index))
                if node_runtime.load().rootless and path.exists():
                    node_runtime.copy_data(path, target)
                elif node_runtime.load().rootless:
                    node_runtime.data_directory(target)
                else:
                    target.mkdir()
                if path.exists() and not node_runtime.load().rootless:
                    worker.run(['cp', '-a', '--', str(path) + '/.', str(target)], timeout=1800)
                mapping[key] = str(target)
            worker.write_json(folder / 'storage.json', {'size_mib': size_mib, 'mapping': mapping})
            worker.apply_storage(config, identifier)
            worker.write_json(runtime, config)
            unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
            if not re.fullmatch(r'[a-zA-Z0-9_.\\-]+\.mount', unit):
                raise ValueError('Invalid storage mount unit')
            unit_path = Path('/etc/systemd/system') / unit
            unit_path.write_text('[Unit]\nDescription=BetterDocker project data disk\nBefore=' + node_runtime.load().system_unit + '\n\n[Mount]\nWhat=' + str(image) + '\nWhere=' + str(point) + '\nType=ext4\nOptions=loop,nodev,nosuid\n\n[Install]\nRequiredBy=docker.service\n'.replace('docker.service', node_runtime.load().system_unit))
            worker.run(['systemctl', 'daemon-reload'])
            worker.run(['systemctl', 'enable', unit])
        except Exception:
            runtime.write_text(original)
            (folder / 'storage.json').unlink(missing_ok=True)
            if 'unit_path' in locals():
                with contextlib.suppress(subprocess.SubprocessError):
                    worker.run(['systemctl', 'disable', unit])
                unit_path.unlink(missing_ok=True)
                worker.run(['systemctl', 'daemon-reload'])
            if mounted:
                worker.run(['umount', str(point)])
            # Preserve partially copied disk for diagnosis; never erase application data.
            raise
        (folder / 'snapshot-restored.json').unlink(missing_ok=True)
        print('Project data moved to a bounded disk. Original sources retained as a recovery copy.')


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
