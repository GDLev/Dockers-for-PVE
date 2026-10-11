"""Carry snapshot files and native data-disk snapshots with a stopped DCT."""
import json
import os
import hashlib
import shutil

from dctlib import projects as worker


def capture(identifier, folder):
    history = folder / 'history'
    history.mkdir(mode=0o700)
    for source in sorted(worker.snapshot_root(identifier).glob('*/manifest.json')):
        name = worker.snapshot_name(source.parent.name)
        original, manifest = worker.snapshot_check(identifier, name)
        import guest_state
        guest_state.check_portable_networks(json.loads((original / 'configuration/compose.runtime.json').read_text()))
        target = history / name
        shutil.copytree(original, target, symlinks=True)
        native = manifest.get('native_storage')
        if native:
            worker.pve_storage(identifier, 'snapshot-export', **native, output=str(target / 'disk.raw'))
        worker.write_json(target / 'checksums.json', worker.snapshot_digest(target))
    tree = worker.ROOT / identifier / 'snapshot-tree.json'
    if tree.exists():
        shutil.copy2(tree, history / 'tree.json')
    worker.write_json(folder / 'checksums.json', worker.snapshot_digest(folder))


def restore(identifier, node, folder):
    history = folder / 'history'
    if not history.exists():
        return
    disk = worker.storage_config(identifier)
    names = [worker.snapshot_name(p.name) for p in history.iterdir() if p.is_dir()]
    native = [name for name in names if json.loads((history / name / 'manifest.json').read_text()).get('native_storage')]
    if native and not disk.get('native_snapshots'):
        raise ValueError('Destination storage must support native snapshots to retain DCT history')
    root = worker.snapshot_root(identifier)
    if root.exists() and any(root.iterdir()):
        raise ValueError('Destination snapshot history already exists')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = folder.parent / 'current-disk.raw'
    device = None
    if native:
        worker.storage_unmount(identifier)
        device = worker.pve_storage(identifier, 'inspect', volid=disk['volid'])['path']
        worker.run(['dd', 'if=' + device, 'of=' + str(current), 'bs=4M', 'conv=sparse', 'status=none'], timeout=86400)
        os.chmod(current, 0o600)
    try:
        for name in sorted(names, key=lambda n: json.loads((history / n / 'manifest.json').read_text())['created']):
            source, target = history / name, root / name
            manifest = json.loads((source / 'manifest.json').read_text())
            if manifest.get('id') != identifier or manifest.get('name') != name or worker.snapshot_digest(source) != json.loads((source / 'checksums.json').read_text()):
                raise ValueError('Invalid migration snapshot history')
            shutil.copytree(source, target, symlinks=True)
            if name in native:
                if (target / 'disk.raw').stat().st_size > disk['size_mib'] * 1048576:
                    raise ValueError('Snapshot disk size differs from destination disk')
                manifest['native_storage']['volid'] = disk['volid']
                worker.write_json(target / 'manifest.json', manifest)
                worker.run(['dd', 'if=' + str(target / 'disk.raw'), 'of=' + device, 'bs=4M', 'conv=fsync,notrunc', 'status=none'], timeout=86400)
                worker.pve_storage(identifier, 'snapshot', volid=disk['volid'], name=name)
                manifest['native_storage']['volid'] = disk['volid']
                (target / 'disk.raw').unlink()
            saved_disk = target / 'configuration/storage.json'
            if saved_disk.exists() and disk:
                old = json.loads(saved_disk.read_text())
                worker.write_json(saved_disk, dict(disk, mapping=old.get('mapping', {})))
            metadata_path = target / 'configuration/metadata.json'
            metadata = json.loads(metadata_path.read_text())
            metadata.update(node=node, vmid=disk.get('vmid', metadata['vmid']))
            metadata.pop('migration', None)
            worker.write_json(metadata_path, metadata)
            # The destination runtime may use another UID mapping and volume root.
            containers_path = target / 'containers.json'
            containers = json.loads(containers_path.read_text())
            mounts = {entry['volume'] for entry in manifest['mounts'] if entry['type'] == 'volume'}
            paths = {}
            for volume in mounts:
                if not volume.startswith('bd-' + identifier + '_'):
                    raise ValueError('Unowned snapshot volume')
                existing = worker.run(['docker', 'volume', 'inspect', volume], check=False, capture_output=True)
                if existing.returncode == 0 and (json.loads(existing.stdout)[0].get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                    raise ValueError('Snapshot volume belongs to another deployment')
                worker.run(['docker', 'volume', 'create', '--label', 'com.docker.compose.project=bd-' + identifier, volume], capture_output=True)
                paths[volume] = json.loads(worker.capture(['docker', 'volume', 'inspect', volume]))[0]['Mountpoint']
            for entry in manifest['mounts']:
                if entry['type'] == 'volume':
                    entry['source'] = paths[entry['volume']]
            for container in containers:
                for mount in container.get('Mounts', []):
                    if mount.get('Name') in paths:
                        mount['Source'] = paths[mount['Name']]
            worker.write_json(containers_path, containers)
            rehome_networks(identifier, target)
            worker.write_json(target / 'manifest.json', manifest)
            worker.write_json(target / 'checksums.json', worker.snapshot_digest(target))
        if (history / 'tree.json').exists():
            shutil.copy2(history / 'tree.json', worker.ROOT / identifier / 'snapshot-tree.json')
    finally:
        if device and current.exists():
            worker.run(['dd', 'if=' + str(current), 'of=' + device, 'bs=4M', 'conv=fsync,notrunc', 'status=none'], timeout=86400)
            worker.storage_mount(identifier)
            current.unlink()


def remove(identifier):
    volumes = set()
    for path in worker.snapshot_root(identifier).glob('*/manifest.json'):
        manifest = json.loads(path.read_text())
        volumes.update(entry['volume'] for entry in manifest.get('mounts', []) if entry['type'] == 'volume')
        native = manifest.get('native_storage')
        if native:
            worker.pve_storage(identifier, 'snapshot-delete', **native)
    for name in volumes:
        existing = worker.run(['docker', 'volume', 'inspect', name], check=False, capture_output=True)
        if existing.returncode:
            continue
        volume = json.loads(existing.stdout)[0]
        if (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
            raise ValueError('Snapshot volume ownership changed')
        worker.run(['docker', 'volume', 'rm', name], capture_output=True)
    shutil.rmtree(worker.snapshot_root(identifier), ignore_errors=True)


def cleanup_exports(identifier, folder):
    for journal in (folder / 'state/history').glob('*/disk.raw.snapdev'):
        name = worker.snapshot_name(journal.parent.name)
        _, manifest = worker.snapshot_check(identifier, name)
        worker.pve_storage(identifier, 'snapshot-export-cleanup', **manifest['native_storage'], output=str(journal.parent / 'disk.raw'))


def rehome_networks(identifier, folder):
    """Use destination-owned Docker networks, without carrying old daemon IPAM."""
    ids = worker.capture(['docker', 'network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier]).split()
    current = json.loads(worker.capture(['docker', 'network', 'inspect', *ids])) if ids else []
    networks = json.loads((folder / 'networks.json').read_text())
    mapping = {}
    for network in networks:
        label = (network.get('Labels') or {}).get('com.docker.compose.network')
        matching = next((row for row in current if (row.get('Labels') or {}).get('com.docker.compose.network') == label and label), None)
        old = network['Name']
        name = matching['Name'] if matching else 'bd-' + identifier + '_history_' + hashlib.sha256(old.encode()).hexdigest()[:12]
        mapping[old] = name
        network['Name'] = name
        network.pop('IPAM', None)
        network.pop('Id', None)
        network.pop('Containers', None)
        network['Options'] = {key: value for key, value in (network.get('Options') or {}).items() if key != 'com.docker.network.bridge.name'}
    worker.write_json(folder / 'networks.json', networks)
    path = folder / 'containers.json'
    containers = json.loads(path.read_text())
    for row in containers:
        mode = row['HostConfig'].get('NetworkMode')
        row['HostConfig']['NetworkMode'] = mapping.get(mode, mode)
        row['NetworkSettings']['Networks'] = {mapping.get(key, key): {k: v for k, v in value.items() if k in ('Aliases', 'Links', 'DriverOpts')} for key, value in row['NetworkSettings'].get('Networks', {}).items()}
    worker.write_json(path, containers)
    runtime_path = folder / 'configuration/compose.runtime.json'
    runtime = json.loads(runtime_path.read_text())
    for value in runtime.get('networks', {}).values():
        if value.get('name') in mapping:
            value['name'] = mapping[value['name']]
        value.pop('ipam', None)
    worker.write_json(runtime_path, runtime)
    request_path = folder / 'configuration/request.json'
    request = json.loads(request_path.read_text())
    request.update(mode='compose', compose=json.dumps(runtime))
    worker.write_json(request_path, request)
    (folder / 'configuration/compose.source.yaml').write_text(request['compose'])
    worker.write_json(folder / 'checksums.json', worker.snapshot_digest(folder))
