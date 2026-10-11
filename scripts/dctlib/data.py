"""Explicit deletion of project-owned data and unused native PVE disks."""
import json
import os
from pathlib import Path
import shutil


def storage_owner(vmid, node):
    vmid = str(vmid)
    if not vmid.isdecimal() or not 100 <= int(vmid) <= 999999999:
        raise ValueError('Invalid guest ID')
    owner = json.loads((worker.METADATA / 'ids' / (vmid + '.json')).read_text())
    identifier = worker.project_id(owner['id'])
    if owner['node'] != node:
        settings = worker.storage_config(identifier)
        if settings.get('node') != node or settings.get('vmid') != int(vmid):
            raise ValueError('DCT disk belongs to another node')
    return identifier


def check_data_dependencies(identifier, info=None):
    if info:
        worker.check_protection(info)
        if info.get('replication') or info.get('ha', {}).get('managed'):
            raise ValueError('Remove HA and replication before deleting data')
    if any(worker.snapshot_root(identifier).glob('*/manifest.json')):
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


def owned_data_plan(identifier, info):
    check_data_dependencies(identifier, info)
    folder = worker.ROOT / identifier
    if folder.is_symlink() or folder.resolve().parent != worker.ROOT.resolve():
        raise ValueError('Invalid project data directory')
    volumes = worker.capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier]).split()
    config = json.loads((folder / 'compose.runtime.json').read_text())
    external = {value.get('name', name) for name, value in config.get('volumes', {}).items() if value.get('external')}
    owned = []
    records = {row['id'] for row in worker.container_records(identifier)}
    for name in volumes:
        if name in external:
            continue
        volume = json.loads(worker.capture(['docker', 'volume', 'inspect', name]))[0]
        if volume.get('Driver') != 'local' or volume.get('Options'):
            continue  # External storage remains outside the deletion scope.
        if (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
            raise ValueError('Docker volume ownership changed')
        users = set(worker.capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + name]).split())
        if users - records:
            raise ValueError('A DCT volume is shared with another container')
        owned.append(name)
    # Refuse foreign mountpoints within the project tree, even when no container is running.
    mountpoints = json.loads(worker.capture(['findmnt', '--json', '--list', '--output', 'TARGET']))['filesystems']
    for row in mountpoints:
        point = Path(row['target'])
        if point.is_relative_to(folder) and point != folder / 'storage-data':
            raise ValueError('Unmount foreign filesystems before deleting DCT data')
    settings = worker.storage_config(identifier)
    for volid in settings.get('unused', []) + ([settings['volid']] if settings.get('volid') else []):
        worker.pve_storage(identifier, 'free-check', volid=volid)
    return owned


def remove_owned_data(identifier, volumes):
    worker.storage_unmount(identifier)
    for name in volumes:
        # Docker checks remaining users again at removal; never force-delete a shared volume.
        worker.run(['docker', 'volume', 'rm', name], capture_output=True)
    settings = worker.storage_config(identifier)
    for volid in settings.get('unused', []) + ([settings['volid']] if settings.get('volid') else []):
        worker.pve_storage(identifier, 'free', volid=volid)
    point = worker.ROOT / identifier / 'storage-data'
    unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(point)]).strip()
    (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
    worker.run(['systemctl', 'daemon-reload'], capture_output=True)
    shutil.rmtree(worker.ROOT / identifier)


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
        public = json.loads(path.read_text()) if path.exists() else None
        local_guest = public is not None and public['node'] == node
        info = worker.detail(identifier, node) if local_guest else None
        check_data_dependencies(identifier, info)
        settings = worker.storage_config(identifier)
        retained = not local_guest
        allowed = settings.get('unused', []) + ([settings['volid']] if retained and settings.get('volid') else [])
        if volid not in allowed:
            raise ValueError('Cannot delete an attached or unowned DCT disk')
        worker.pve_storage(identifier, 'free-check', volid=volid)
        if volid == settings.get('volid'):
            worker.storage_unmount(identifier)
        worker.pve_storage(identifier, 'free', volid=volid)
        if retained and volid == settings.get('volid'):
            unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(worker.ROOT / identifier / 'storage-data')]).strip()
            (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
            worker.run(['systemctl', 'daemon-reload'], capture_output=True)
        if volid in settings.get('unused', []):
            settings['unused'].remove(volid)
        else:
            settings.pop('volid')
            settings.pop('mapping', None)
        target = worker.ROOT / identifier / 'storage.json'
        if settings.get('volid') or settings.get('unused'):
            worker.write_json(target, settings)
        else:
            target.unlink(missing_ok=True)
            if not path.exists():
                worker.release_id(identifier)
        print('DCT disk removed: ' + volid)


from . import projects as worker
