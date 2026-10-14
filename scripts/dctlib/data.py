"""Explicit deletion of project-owned data and unused native PVE disks."""
from .secure_io import read_json
import json
import os
from pathlib import Path
import shutil
import re
from .policy import owned_local_volume
from .disks import volume_ids, source, records


def remember_volumes(identifier, containers):
    """Keep anonymous ownership when a service or replica is later removed."""
    with worker.locked():
        names = {mount['Name'] for row in containers for mount in row.get('Mounts', []) if mount.get('Type') == 'volume'}
        ledger = worker.ROOT / identifier / 'owned-volumes.json'
        if ledger.exists():
            names.update(read_json(ledger)['volumes'])
        verified = []
        for name in sorted(names):
            volume = worker.run(['docker', 'volume', 'inspect', name], check=False, capture_output=True)
            if volume.returncode:
                if 'no such volume' not in (volume.stderr or '').lower():
                    raise ValueError('Cannot inspect retained volume ownership')
                continue
            if owned_local_volume(json.loads(volume.stdout)[0], name, identifier):
                check_volume_claim(name, identifier)
                verified.append(name)
        if verified:
            worker.write_json(ledger, {'volumes': verified})


def storage_owner(vmid, node):
    vmid = str(vmid)
    if not vmid.isdecimal() or not 100 <= int(vmid) <= 999999999:
        raise ValueError('Invalid guest ID')
    owner = read_json(worker.METADATA / 'ids' / (vmid + '.json'))
    identifier = worker.project_id(owner['id'])
    if owner['node'] != node:
        settings = worker.storage_config(identifier)
        if settings.get('node') != node or settings.get('vmid') != int(vmid):
            raise ValueError('DCT disk belongs to another node')
    return identifier


def check_data_dependencies(identifier, info=None, allow_snapshots=False):
    if info:
        worker.check_protection(info)
        if info.get('replication') or info.get('ha', {}).get('managed'):
            raise ValueError('Remove HA and replication before deleting data')
    if not allow_snapshots and any(worker.snapshot_root(identifier).glob('*/manifest.json')):
        raise ValueError('Remove snapshots before deleting data')
    folder = worker.ROOT / identifier
    if (folder / 'replication.json').exists():
        raise ValueError('Remove replication state before deleting data')
    if (folder / 'ha.json').exists():
        raise ValueError('Remove DCT from HA before deleting data')
    for name in ('storage-transaction.json', 'allocated-volume.json', 'export-transaction.json', 'snapshot-transaction.json'):
        if (folder / name).exists():
            raise ValueError('Finish the interrupted data operation before deleting data')
    if (worker.ROOT.parent / 'imports' / (identifier + '.json')).exists():
        raise ValueError('Finish the interrupted import before deleting data')


def check_volume_claim(name, identifier):
    """An unlabelled anonymous volume may still belong to a deleted guest."""
    for ledger in worker.ROOT.glob('*/owned-volumes.json'):
        if ledger.parent.name == identifier:
            continue
        if name in read_json(ledger).get('volumes', []):
            raise ValueError('Docker volume is retained by another DCT')


def owned_data_plan(identifier, info, native_snapshots=None):
    check_data_dependencies(identifier, info, allow_snapshots=native_snapshots is not None)
    folder = worker.ROOT / identifier
    if folder.is_symlink() or folder.resolve().parent != worker.ROOT.resolve():
        raise ValueError('Invalid project data directory')
    volumes = worker.capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier]).split()
    config = read_json(folder / 'compose.runtime.json')
    external = {value.get('name', name) for name, value in config.get('volumes', {}).items() if value.get('external')}
    container_ids = {row['id'] for row in worker.container_records(identifier)}
    if container_ids:
        containers = json.loads(worker.capture(['docker', 'inspect', *sorted(container_ids)]))
        if {row['Id'] for row in containers} != container_ids:
            raise ValueError('Container ownership changed while planning data removal')
        volumes.extend(mount['Name'] for row in containers for mount in row.get('Mounts', [])
                       if mount.get('Type') == 'volume')
    ledger = folder / 'owned-volumes.json'
    if ledger.exists():
        saved = read_json(ledger)['volumes']
        if any(not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}', name) for name in saved):
            raise ValueError('Invalid retained volume ownership')
        existing = set(worker.capture(['docker', 'volume', 'ls', '-q']).split())
        volumes.extend(name for name in saved if name in existing)
    owned = []
    for name in sorted(set(volumes)):
        if name in external:
            continue
        volume = json.loads(worker.capture(['docker', 'volume', 'inspect', name]))[0]
        if volume.get('Driver') != 'local' or volume.get('Options'):
            continue  # External storage remains outside the deletion scope.
        if not owned_local_volume(volume, name, identifier):
            raise ValueError('Docker volume ownership changed')
        if volume.get('Name', name) != name:
            raise ValueError('Docker volume identity changed')
        check_volume_claim(name, identifier)
        users = set(worker.capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + name]).split())
        if users - container_ids:
            raise ValueError('A DCT volume is shared with another container')
        owned.append(name)
    # Refuse foreign mountpoints within the project tree, even when no container is running.
    mountpoints = json.loads(worker.capture(['findmnt', '--json', '--list', '--output', 'TARGET']))['filesystems']
    for row in mountpoints:
        point = Path(row['target'])
        if point.is_relative_to(folder) and point not in {folder / 'storage-data', *[source(identifier, key) for key in records(worker.storage_config(identifier))]}:
            raise ValueError('Unmount foreign filesystems before deleting DCT data')
    settings = worker.storage_config(identifier)
    for volid in volume_ids(settings):
        options = {'snapshots': native_snapshots.get(volid, [])} if native_snapshots is not None else {}
        worker.pve_storage(identifier, 'free-check', volid=volid, **options)
    if owned:
        worker.write_json(ledger, {'volumes': owned})
    return owned


def remove_owned_data(identifier, volumes):
    worker.storage_unmount(identifier)
    for name in volumes:
        # Docker checks remaining users again at removal; never force-delete a shared volume.
        worker.run(['docker', 'volume', 'rm', name], capture_output=True)
    settings = worker.storage_config(identifier)
    for volid in volume_ids(settings):
        worker.pve_storage(identifier, 'free', volid=volid)
    mount_keys = set(records(settings)) | {path.name[len('mount-'):] for path in (worker.ROOT / identifier).glob('mount-mp*')}
    for key in mount_keys:
        unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(source(identifier, key))]).strip()
        (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
    point = worker.ROOT / identifier / 'storage-data'
    unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
    (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
    worker.run(['systemctl', 'daemon-reload'], capture_output=True)
    shutil.rmtree(worker.ROOT / identifier)


def retained_data(identifier, destroy=False):
    identifier = worker.project_id(identifier)
    with worker.operation(identifier, 'data-cleanup'), worker.locked(identifier):
        if (worker.METADATA / (identifier + '.json')).exists():
            raise ValueError('Remove the DCT before cleaning retained project data')
        if worker.container_records(identifier):
            raise ValueError('Retained project still has Docker containers')
        check_data_dependencies(identifier, allow_snapshots=True)
        snapshots = []
        for path in worker.snapshot_root(identifier).glob('*/manifest.json'):
            folder, manifest = worker.snapshot_check(identifier, path.parent.name)
            snapshots.append((folder, manifest))
        native = {}
        for _, manifest in snapshots:
            disk = manifest.get('native_storage')
            if disk:
                native.setdefault(disk['volid'], []).append(disk['name'])
        # Complete ownership checks before deleting even the first snapshot.
        volumes = owned_data_plan(identifier, {}, native_snapshots=native)
        plan = {'id': identifier, 'volumes': volumes, 'disks': volume_ids(worker.storage_config(identifier)),
                'snapshots': [manifest['name'] for _, manifest in snapshots],
                'directory': str(worker.ROOT / identifier), 'dry_run': not destroy}
        if not destroy:
            return plan
        for folder, manifest in snapshots:
            if manifest.get('native_storage'):
                worker.pve_storage(identifier, 'snapshot-delete', **manifest['native_storage'])
            shutil.rmtree(folder)
        if destroy:
            remove_owned_data(identifier, volumes)
            shutil.rmtree(worker.snapshot_root(identifier), ignore_errors=True)
            from .service_recreate import prune_images
            prune_images(identifier)
            worker.release_id(identifier)
        return plan


def storage_volume_state(identifier, node, vmid):
    if storage_owner(vmid, node) != identifier:
        raise ValueError('DCT disk ownership changed')
    return worker.storage_config(identifier)


def delete_storage_volume(vmid, node, volid):
    identifier = storage_owner(vmid, node)
    with worker.operation(identifier, 'diskdelete'), worker.locked(identifier):
        # Reservation and metadata are checked again after taking the guest lock.
        if storage_owner(vmid, node) != identifier:
            raise ValueError('DCT disk ownership changed')
        path = worker.METADATA / (identifier + '.json')
        public = read_json(path) if path.exists() else None
        local_guest = public is not None and public['node'] == node
        info = worker.detail(identifier, node) if local_guest else None
        check_data_dependencies(identifier, info)
        settings = worker.storage_config(identifier)
        retained = not local_guest
        allowed = volume_ids(settings) if retained else settings.get('unused', [])
        if volid not in allowed:
            raise ValueError('Cannot delete an attached or unowned DCT disk')
        worker.pve_storage(identifier, 'free-check', volid=volid)
        if volid == settings.get('volid'):
            worker.storage_unmount(identifier)
        for key, disk in records(settings).items():
            if disk['volid'] == volid:
                from .disks import unmount
                unmount(identifier, key)
        worker.pve_storage(identifier, 'free', volid=volid)
        if retained and volid == settings.get('volid'):
            unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(worker.ROOT / identifier / 'storage-data')]).strip()
            (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
            worker.run(['systemctl', 'daemon-reload'], capture_output=True)
        if volid in settings.get('unused', []):
            settings['unused'].remove(volid)
        elif volid == settings.get('volid'):
            settings.pop('volid')
            settings.pop('mapping', None)
        else:
            for key, disk in list(records(settings).items()):
                if disk['volid'] == volid:
                    settings['additional'].pop(key)
                    unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(source(identifier, key))]).strip()
                    (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
                    worker.run(['systemctl', 'daemon-reload'], capture_output=True)
        target = worker.ROOT / identifier / 'storage.json'
        if volume_ids(settings):
            worker.write_json(target, settings)
        else:
            target.unlink(missing_ok=True)
            if not path.exists():
                worker.release_id(identifier)
        print('DCT disk removed: ' + volid)


from . import projects as worker
