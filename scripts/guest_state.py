#!/usr/bin/env python3
"""Portable DCT state used by backups, independent clones and node transfers."""
import argparse
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import quote

from dctlib import projects as worker

CONFIG_FILES = ('request.json', 'compose.source.yaml', '.env', 'compose.runtime.json',
                'images.json', 'settings.json', 'pending.json', 'pending-network.json', 'notes.txt', 'limits.json')


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    worker.write_json(Path(path), value)
    os.chmod(path, 0o600)


@contextlib.contextmanager
def operation(identifier, name, lock_fd=None):
    if lock_fd is None:
        with worker.operation(identifier, name):
            yield
        return
    # The native vzdump plugin lends its locked descriptor to its child helper.
    path = worker.ROOT.parent / 'operations' / (identifier + '.lock')
    stat, expected = os.fstat(int(lock_fd)), path.stat()
    if (stat.st_dev, stat.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('Invalid inherited DCT operation lock')
    fcntl.flock(int(lock_fd), fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield


def journal_path(identifier):
    return worker.ROOT / worker.project_id(identifier) / 'export-transaction.json'


def owned_mounts(identifier, containers):
    sources = {}
    owned = {c['Id'] for c in containers}
    base = worker.ROOT / identifier
    for container in containers:
        for mount in container.get('Mounts', []):
            if mount['Type'] == 'tmpfs':
                continue
            path = Path(mount['Source'])
            if mount['Type'] == 'bind':
                if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()) or path.resolve() == base.resolve():
                    raise ValueError('Portable state requires project-owned bind mounts')
                if path.name in CONFIG_FILES + ('storage.json', 'storage.ext4'):
                    raise ValueError('Deployment files cannot be application mounts')
            elif mount['Type'] == 'volume':
                volume = json.loads(worker.capture(['docker','volume','inspect',mount['Name']]))[0]
                labels = volume.get('Labels') or {}
                anonymous = (not labels or set(labels) == {'com.docker.volume.anonymous'}) and re.fullmatch(r'[0-9a-f]{64}', mount['Name'])
                if volume.get('Driver') != 'local' or volume.get('Options') or not (anonymous or labels.get('com.docker.compose.project') == 'bd-'+identifier):
                    raise ValueError('Portable state requires owned local Docker volumes')
                users = set(worker.capture(['docker','ps','--no-trunc','-aq','--filter','volume='+mount['Name']]).split())
                if users - owned or path.resolve() != Path(volume['Mountpoint']).resolve():
                    raise ValueError('A backup volume is shared or its ownership changed')
                worker.node_runtime.check_volume_path(path, mount['Name'])
            else:
                raise ValueError('Unsupported DCT backup mount type')
            if not path.is_dir() and not path.is_file():
                raise ValueError('Backup mount source is not a regular file or directory')
            sources[str(path)] = {'source':str(path),'type':mount['Type'],'volume':mount.get('Name',''),'file':path.is_file()}
    mounts = [m for source,m in sources.items() if not any(Path(source).is_relative_to(Path(other)) for other in sources if other != source)]
    settings = worker.storage_config(identifier)
    if settings:
        point = worker.ROOT / identifier / 'storage-data'
        worker.storage_mount(identifier)
        mounts = [m for m in mounts if not Path(m['source']).is_relative_to(point)]
        mounts.append({'source': str(point), 'type': 'bind', 'volume': ''})
    mounted = json.loads(worker.capture(['findmnt', '-J', '-l', '-o', 'TARGET']))['filesystems']
    for mount in mounts:
        path = Path(mount['source']).resolve()
        if any(Path(item['target']).is_relative_to(path) and Path(item['target']) != path for item in mounted):
            raise ValueError('Nested filesystems cannot be included in a DCT backup')
    return mounts


def cleanup_capture(identifier, resume=True):
    path = journal_path(identifier)
    if not path.exists():
        return
    state = read(path)
    owned = {r['id']: r for r in worker.container_records(identifier)}
    if resume:
        for cid in state.get('paused', []):
            if cid in owned and owned[cid]['state'] == 'paused':
                worker.run(['docker', 'unpause', cid], capture_output=True)
        for cid in state.get('restart', []):
            if cid in owned and owned[cid]['state'] in ('created', 'exited'):
                worker.run(['docker', 'start', cid], capture_output=True)
    for tag in state.get('tags', []):
        if not re.fullmatch('betterdocker-export/' + identifier + ':[0-9a-f]{32}', tag):
            raise ValueError('Invalid export image ownership')
        worker.run(['docker', 'image', 'rm', '--force', tag], check=False, capture_output=True)
    path.unlink()


def quiesce(identifier, node, mode, lock_fd=None):
    if mode not in ('stop', 'suspend'):
        raise ValueError('Invalid backup quiescence mode')
    with operation(identifier, 'backup', lock_fd), worker.locked(identifier):
        worker.detail(identifier, node)
        containers = worker.snapshot_containers(identifier, allow_running=True)
        path = journal_path(identifier)
        state = read(path) if path.exists() else {'paused': [], 'restart': [], 'tags': []}
        for container in containers:
            if container['State']['Status'] != 'running':
                continue
            key = 'paused' if mode == 'suspend' else 'restart'
            if container['Id'] not in state[key]:
                state[key].append(container['Id'])
            write(path, state)
            worker.run(['docker', 'pause' if mode == 'suspend' else 'stop', container['Id']],
                       capture_output=True, timeout=86400)


def recover(identifier):
    """Recover only after acquiring the abandoned operation's actual kernel lock."""
    if worker.operation_state(identifier).get('active'):
        return
    if not journal_path(identifier).exists() and not (worker.ROOT.parent / 'imports' / (identifier+'.json')).exists():
        return
    try:
        with worker.operation(identifier, 'recovery'):
            cleanup_capture(identifier)
            cleanup_import(identifier)
    except ValueError as error:
        if str(error) != 'Another operation is running for this project':
            raise


def check_portable_networks(runtime):
    # Reusing an explicit Docker subnet would collide with the source during cloning.
    for network in runtime.get('networks', {}).values():
        if isinstance(network, dict) and network.get('ipam'):
            raise ValueError('Portable DCT state does not support custom Docker IPAM')
    for service in runtime.get('services', {}).values():
        networks = service.get('networks') or {}
        if isinstance(networks, dict):
            for endpoint in networks.values():
                if isinstance(endpoint, dict) and any(endpoint.get(key) for key in
                        ('ipv4_address', 'ipv6_address', 'link_local_ips')):
                    raise ValueError('Portable DCT state does not support fixed internal Docker addresses')


def capture_state(identifier, node, folder, lock_fd=None, paused=False, keep_journal=False, digest=None):
    folder = Path(folder)
    with operation(identifier, 'backup', lock_fd), worker.locked(identifier):
        info = worker.detail(identifier, node)
        if digest:
            worker.assert_configuration_digest(identifier, node, digest)
        worker.recover_network_transaction(identifier, node)
        worker.recover_storage_transaction(identifier)
        worker.recover_snapshot_transaction(identifier)
        worker.recover_guest_hook(info)
        if folder.exists() or folder.resolve().is_relative_to((worker.ROOT / identifier).resolve()):
            raise ValueError('Export directory must be new and separate from the DCT')
        folder.mkdir(parents=True, mode=0o700)
        containers = worker.snapshot_containers(identifier, allow_running=True, quiesced=paused)
        runtime = read(worker.ROOT / identifier / 'compose.runtime.json')
        check_portable_networks(runtime)
        expected = sum(s.get('deploy', {}).get('replicas', 1) for s in runtime['services'].values())
        counts = {s: sum((c['Config'].get('Labels') or {}).get('com.docker.compose.service') == s for c in containers) for s in runtime['services']}
        if len(containers) != expected or any(counts[s] != value.get('deploy', {}).get('replicas', 1) for s, value in runtime['services'].items()):
            raise ValueError('Deploy all DCT instances before creating a backup or clone')
        mounts = owned_mounts(identifier, containers)
        network_names = sorted({n for c in containers for n in c.get('NetworkSettings', {}).get('Networks', {})})
        networks = json.loads(worker.capture(['docker', 'network', 'inspect', *network_names])) if network_names else []
        if any((n.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier or n['Driver'] != 'bridge' for n in networks):
            raise ValueError('Portable DCT state requires project-owned Docker bridge networks')
        path = journal_path(identifier)
        state = read(path) if path.exists() else {'paused': [], 'restart': [], 'tags': []}
        state['folder'] = str(folder)
        write(path, state)
        try:
            for container in containers:
                if container['State']['Status'] == 'running' and not paused:
                    state['paused'].append(container['Id'])
                    write(path, state)
                    worker.run(['docker', 'pause', container['Id']], capture_output=True)
            os.sync()
            config = folder / 'configuration'
            config.mkdir(mode=0o700)
            for filename in CONFIG_FILES:
                source = worker.ROOT / identifier / filename
                if source.exists():
                    shutil.copy2(source, config / filename)
            write(config / 'metadata.json', {key: value for key, value in info.items() if key not in ('deployment', 'digest', 'metrics')})
            firewall = Path('/etc/pve/firewall') / (str(info['vmid']) + '.fw')
            if firewall.exists():
                (config / 'firewall.fw').write_text(firewall.read_text())
            settings = worker.storage_config(identifier)
            if settings:
                write(config / 'storage.json', settings)
            (folder / 'data').mkdir(mode=0o700)
            for index, mount in enumerate(mounts):
                mount['copy'] = 'data/' + str(index)
                worker.node_runtime.copy_data(mount['source'], folder / mount['copy'], runner=worker.run)
                worker.node_runtime.remap_tree(folder / mount['copy'], worker.node_runtime.load(), worker.node_runtime.Runtime())
            for container in containers:
                tag = 'betterdocker-export/' + identifier + ':' + uuid.uuid4().hex
                state['tags'].append(tag)
                write(path, state)
                worker.run(['docker', 'commit', '--pause=false', container['Id'], tag], timeout=86400, capture_output=True)
                container['SnapshotImage'] = worker.capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag]).strip()
                container['SnapshotTag'] = tag
            if not paused:
                for cid in state['paused']:
                    worker.run(['docker', 'unpause', cid], capture_output=True)
                state['paused'] = []
            write(path, state)
            from dctlib.images import saved_images
            worker.run(['docker', 'image', 'save', '-o', str(folder / 'images.tar'), *state['tags'], *saved_images(worker.ROOT / identifier)], timeout=86400)
            os.chmod(folder / 'images.tar', 0o600)
            write(folder / 'containers.json', containers)
            if worker.node_runtime.load().rootless:
                for network in networks:
                    (network.get('Options') or {}).pop('com.docker.network.bridge.gateway_mode_ipv4', None)
            write(folder / 'networks.json', networks)
            selected = worker.node_runtime.load()
            write(folder / 'manifest.json', {'version': 2 if selected.rootless else 1, 'runtime': selected.identity(), 'kind': 'dct-state', 'id': identifier,
                  'vmid': int(info['vmid']), 'name': info['name'], 'created': int(time.time()), 'mounts': mounts})
            write(folder / 'checksums.json', worker.snapshot_digest(folder))
        finally:
            if keep_journal:
                # Native vzdump owns stop/resume recovery until its cleanup phase.
                for tag in state['tags']:
                    worker.run(['docker', 'image', 'rm', '--force', tag], check=False, capture_output=True)
                state['tags'] = []
                write(path, state)
            else:
                cleanup_capture(identifier)
    return read(folder / 'manifest.json')


def check_state(folder):
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('DCT backup state does not exist')
    for name in ('configuration', 'data'):
        if (folder / name).is_symlink() or not (folder / name).is_dir():
            raise ValueError('Invalid DCT backup directory')
    for name in ('manifest.json', 'checksums.json'):
        if (folder / name).is_symlink() or not (folder / name).is_file():
            raise ValueError('Invalid DCT backup manifest')
    manifest = read(folder / 'manifest.json')
    if manifest.get('version') not in (1, 2) or manifest.get('kind') != 'dct-state':
        raise ValueError('Unsupported DCT backup format')
    worker.project_id(manifest.get('id', ''))
    if worker.snapshot_digest(folder) != read(folder / 'checksums.json'):
        raise ValueError('DCT backup integrity check failed')
    for name in ('manifest.json', 'checksums.json', 'containers.json', 'networks.json', 'images.tar',
                 'configuration/request.json', 'configuration/compose.runtime.json', 'configuration/metadata.json'):
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid DCT backup configuration file')
    for name in CONFIG_FILES + ('storage.json',):
        path = folder / 'configuration' / name
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ValueError('Invalid DCT backup configuration file')
    containers = read(folder / 'containers.json')
    if not isinstance(containers, list) or not containers:
        raise ValueError('DCT backup has no containers')
    for container in containers:
        if (container['Config'].get('Labels') or {}).get('betterdocker.id') != manifest['id']:
            raise ValueError('Container ownership in DCT backup is invalid')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', container.get('SnapshotImage', '')) or not re.fullmatch(
                'betterdocker-export/' + manifest['id'] + ':[0-9a-f]{32}', container.get('SnapshotTag', '')):
            raise ValueError('Invalid DCT backup image identity')
    if not (folder / 'images.tar').is_file():
        raise ValueError('DCT backup images are missing')
    for mount in manifest.get('mounts', []):
        if not re.fullmatch(r'data/[0-9]+', mount.get('copy', '')) or not ((folder / mount['copy']).is_file() if mount.get('file') else (folder / mount['copy']).is_dir()):
            raise ValueError('Invalid DCT backup data mapping')
        if (folder / mount['copy']).is_symlink():
            raise ValueError('DCT backup mount root cannot be a link')
        if mount.get('type') not in ('bind', 'volume'):
            raise ValueError('Invalid DCT backup mount type')
        if mount['type'] == 'bind':
            path = Path(mount['source'])
            base = worker.ROOT / manifest['id']
            if not path.is_absolute() or not path.is_relative_to(base) or path == base or '..' in path.parts:
                raise ValueError('DCT backup refers to an external host path')
        elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', mount.get('volume', '')):
            raise ValueError('Invalid DCT backup volume name')
    return manifest


def pack_state(folder, archive):
    check_state(folder)
    archive = Path(archive)
    if archive.exists():
        raise ValueError('Backup archive already exists')
    worker.run(['tar', '--numeric-owner', '--xattrs', '--acls', '--sparse', '-cf', str(archive), '-C', str(folder), '.'], timeout=86400)
    os.chmod(archive, 0o600)


def unpack_state(archive, folder, history=False):
    folder = Path(folder)
    if folder.exists():
        raise ValueError('Restore staging directory already exists')
    folder.mkdir(parents=True, mode=0o700)
    try:
        with tarfile.open(archive, 'r:*') as handle:
            members = handle.getmembers()
            names = {}
            for member in members:
                name = PurePosixPath(member.name)
                if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise ValueError('Unsafe DCT archive member')
                normalized = str(name)
                if normalized in names:
                    raise ValueError('Duplicate DCT archive member')
                names[normalized] = member
            links = {name for name, member in names.items() if member.issym() or member.islnk()}
            def application_path(name):
                return name.startswith('data/') or history and bool(re.fullmatch(r'history/[A-Za-z][A-Za-z0-9_-]{0,39}/data/.+', name))
            for name, member in names.items():
                if any(str(parent) in links for parent in PurePosixPath(name).parents):
                    raise ValueError('DCT archive writes through a link')
                history_member = history and (name in ('history', 'history/tree.json') or bool(re.fullmatch(r'history/[A-Za-z][A-Za-z0-9_-]{0,39}(?:/.*)?', name)))
                if name != '.' and not history_member and not (name.startswith('data/') or name.startswith('configuration/') or name in
                        ('data', 'configuration', 'manifest.json', 'containers.json', 'networks.json', 'images.tar', 'checksums.json')):
                    raise ValueError('Unknown DCT archive member')
                if member.issym():
                    # Application symlinks are preserved, but never followed while extracting.
                    if not application_path(name):
                        raise ValueError('DCT configuration cannot contain links')
                if member.islnk():
                    target = PurePosixPath(member.linkname)
                    linked = names.get(str(target))
                    if target.is_absolute() or '..' in target.parts or not linked or not linked.isfile() or not application_path(name) or not application_path(str(target)):
                        raise ValueError('Unsafe DCT archive hard link')
        worker.run(['tar', '--numeric-owner', '--xattrs', '--acls', '-xf', str(archive), '-C', str(folder)], timeout=86400)
        return check_state(folder)
    except BaseException:
        shutil.rmtree(folder)
        raise


def remap_labels(labels, old, new):
    labels = dict(labels or {})
    labels['betterdocker.id'] = new
    labels['com.docker.compose.project'] = 'bd-' + new
    for key in ('com.docker.compose.project.config_files', 'com.docker.compose.project.working_dir'):
        if key in labels:
            labels[key] = labels[key].replace(str(worker.ROOT / old), str(worker.ROOT / new))
    return labels


def adapted_state(folder, identifier, address):
    manifest = check_state(folder)
    old = manifest['id']
    old_base, new_base = worker.ROOT / old, worker.ROOT / identifier
    runtime = read(Path(folder) / 'configuration' / 'compose.runtime.json')
    runtime['name'] = 'bd-' + identifier
    volumes = {}
    for container in read(Path(folder) / 'containers.json'):
        if (container['Config'].get('Labels') or {}).get('betterdocker.id') != old:
            raise ValueError('Container ownership in DCT backup is invalid')
        for mount in container.get('Mounts', []):
            if mount['Type'] == 'volume':
                volumes.setdefault(mount['Name'], 'bd-' + identifier + '_restored_' + str(len(volumes)))
    networks = {n['Name']: 'bd-' + identifier + '_restored_' + str(i) for i, n in enumerate(read(Path(folder) / 'networks.json'))}
    def source(value):
        if value in volumes:
            return volumes[value]
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(old_base) and '..' not in path.parts:
            return str(new_base / path.relative_to(old_base))
        raise ValueError('DCT backup contains an external mount source')
    for service in runtime['services'].values():
        service['labels'] = remap_labels(service.get('labels'), old, identifier)
        for port in service.get('ports', []):
            port['host_ip'] = address
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind':
                mount['source'] = source(mount['source'])
        # Names in the top-level volume/network declarations are updated below.
    for value in runtime.get('volumes', {}).values():
        if value.get('name') in volumes:
            value['name'] = volumes[value['name']]
    for value in runtime.get('networks', {}).values():
        if value.get('name') in networks:
            value['name'] = networks[value['name']]
            value.pop('ipam', None)
    return manifest, runtime, volumes, networks, source


def restore_state(folder, identifier, node, overrides, storage='', lock_fd=None, template=False, trusted_hook=False, live_firewall=None):
    folder = Path(folder)
    manifest = check_state(folder)
    import service_security
    service_security.check(read(folder / 'configuration' / 'compose.runtime.json'), worker.node_runtime.load())
    check_portable_networks(read(folder / 'configuration' / 'compose.runtime.json'))
    worker.project_id(identifier)
    if not worker.trusted_management():
        worker.validate_delegated_config(read(folder / 'configuration' / 'compose.runtime.json'), manifest['id'])
        worker.validate_delegated_containers(read(folder / 'containers.json'), manifest['id'])
        worker.validate_delegated_networks(read(folder / 'networks.json'))
        for mount in manifest['mounts']:
            if mount['type'] == 'bind':
                worker.safe_bind(mount['source'], manifest['id'])
    if node != worker.current_node():
        raise ValueError('Restore was sent to the wrong node')
    with operation(identifier, 'restore', lock_fd), worker.locked(identifier):
        project = worker.ROOT / identifier
        metadata_path = worker.METADATA / (identifier + '.json')
        if project.exists() or metadata_path.exists():
            raise ValueError('Restore requires a new DCT; existing deployment was not changed')
        vmid = next((number for number, owner in worker.reserved_ids().items() if owner['id'] == identifier and owner['node'] == node), None)
        if not vmid:
            raise ValueError('Reserve the destination DCT ID before restoring')
        request = read(folder / 'configuration' / 'request.json')
        if not isinstance(overrides, dict) or set(overrides) - {'name', 'bridge', 'address', 'cidr', 'ipv4mode', 'hwaddr', 'mtu', 'interfaces'}:
            raise ValueError('Unsupported DCT restore override')
        request.update(overrides, vmid=vmid)
        if 'interfaces' in request and 'interfaces' not in overrides:
            from dctlib.endpoints import KEYS
            request['interfaces']['net0'].update({key: value for key, value in overrides.items() if key in KEYS})
            if overrides.get('address') and not overrides.get('cidr'):
                request['interfaces']['net0']['cidr'] = overrides['address']
        if overrides.get('address') and not overrides.get('cidr'):
            request.pop('cidr', None)
        if overrides.get('ipv4mode') == 'dhcp':
            request.update(address='', cidr='')
        elif not overrides.get('address') and not overrides.get('cidr'):
            raise ValueError('Choose an independent IPv4 address for the restored or cloned DCT')
        request['hwaddr'] = overrides.get('hwaddr', '')
        saved_info = read(folder / 'configuration' / 'metadata.json')
        # Migration/HA retain the current cluster policy, not an older backup.
        request['firewall'] = live_firewall if live_firewall is not None else saved_info.get('firewall', request.get('firewall', 0))
        request = worker.validate(request)
        worker.network_preflight(request)
        info = {key: saved_info[key] for key in ('tags', 'notes', 'onboot', 'startup', 'hookscript', 'firewall') if key in saved_info}
        info['firewall'] = request.get('firewall', 0)
        if info.get('hookscript'):
            if not trusted_hook and os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam') != 'root@pam':
                raise ValueError('Restoring a hookscript is only allowed for root@pam')
            worker.pve_hook(info['hookscript'])
        info.update(id=identifier, vmid=vmid, node=node, name=request['name'], bridge=request['bridge'],
                    address=request['address'], cidr=request['cidr'], ipv4mode=request['ipv4mode'], mtu=request['mtu'],
                    hwaddr=request['hwaddr'] or '02:' + ':'.join(identifier[i:i+2] for i in range(0, 10, 2)),
                    interface='bd' + identifier[:12], status='restoring', created=int(time.time()), template=bool(template))
        if 'interfaces' in request:
            info['interfaces'] = request['interfaces']
        info['runtime_mode'] = worker.node_runtime.load().mode
        project.mkdir(parents=True, mode=0o700)
        worker.node_runtime.read_access(project)
        imports = worker.ROOT.parent / 'imports'
        imports.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = imports / (identifier + '.json')
        state = {'id': identifier, 'node': node, 'phase': 'allocating', 'containers': [], 'volumes': [], 'networks': [],
                 'source': manifest['id'], 'images': [], 'vmid': vmid, 'restore_firewall': live_firewall is None,
                 'firewall_before': (Path('/etc/pve/firewall') / (str(vmid) + '.fw')).read_text() if (Path('/etc/pve/firewall') / (str(vmid) + '.fw')).exists() else None}
        write(journal, state)
        try:
            worker.write_json(metadata_path, info)
            for filename in CONFIG_FILES:
                path = folder / 'configuration' / filename
                if path.exists():
                    shutil.copy2(path, project / filename)
            worker.ensure_address(info)
            manifest, runtime, volume_names, network_names, remap_source = adapted_state(folder, identifier, info['address'])
            if 'interfaces' in info:
                from dctlib.endpoints import bind_runtime
                runtime = bind_runtime(runtime, info)
            containers = read(folder / 'containers.json')
            worker.project_limits.check_directory(project, runtime)
            worker.project_limits.check_containers(containers, worker.project_limits.load(project))
            info['expected'] = len(containers)
            worker.write_json(metadata_path, info)
            (project / 'pending-network.json').unlink(missing_ok=True)
            saved_disk = folder / 'configuration' / 'storage.json'
            if saved_disk.exists():
                disk = read(saved_disk)
                allocated = worker.pve_storage(identifier, 'allocate', storage=storage or disk.get('storage', 'local-lvm'), size_mib=disk['size_mib'])
                state['volid'] = allocated['volid']; write(journal, state)
                worker.run(['mkfs.ext4', '-q', '-m', '0', allocated['path']])
                allocated.pop('path', None)
                allocated.update(vmid=vmid, node=node, unused=[], mapping={k.replace(str(worker.ROOT / manifest['id']), str(project)): remap_source(v) for k, v in disk.get('mapping', {}).items()})
                write(project / 'storage.json', allocated)
                worker.storage_mount(identifier)
                worker.storage_unit(identifier, allocated)
            for old, name in volume_names.items():
                state['volumes'].append(name); write(journal, state)
                worker.docker_engine('POST', '/volumes/create', {'Name': name, 'Driver':'local', 'Labels':{'com.docker.compose.project':'bd-'+identifier}})
            for mount in manifest['mounts']:
                if mount['type'] == 'volume':
                    name = volume_names[mount['volume']]
                    target = Path(json.loads(worker.capture(['docker', 'volume', 'inspect', name]))[0]['Mountpoint'])
                else:
                    target = Path(remap_source(mount['source']))
                if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                    raise ValueError('DCT restore target crosses a symbolic link')
                if mount.get('file'):
                    worker.node_runtime.data_directory(target.parent)
                else:
                    worker.node_runtime.data_directory(target)
                worker.node_runtime.restore_data(folder / mount['copy'], target, file=bool(mount.get('file')), runner=worker.run)
            for network in read(folder / 'networks.json'):
                name = network_names[network['Name']]
                body = {'Name':name, 'Driver':'bridge', 'Internal':network.get('Internal',False), 'Attachable':network.get('Attachable',False),
                        'EnableIPv6':network.get('EnableIPv6',False), 'Labels':remap_labels(network.get('Labels'), manifest['id'], identifier)}
                if network.get('EnableIPv6'):
                    raise ValueError('Portable DCT state currently supports IPv4 Docker networks')
                body['Options'] = {key: value for key, value in (network.get('Options') or {}).items()
                                   if key != 'com.docker.network.bridge.name'}
                if 'com.docker.network.bridge.host_binding_ipv4' in body['Options']:
                    body['Options']['com.docker.network.bridge.host_binding_ipv4'] = info['address']
                state['networks'].append(name); write(journal, state)
                worker.docker_engine('POST', '/networks/create', body)
            state['images'] = [{'tag': c['SnapshotTag'], 'id': c['SnapshotImage']} for c in containers]
            write(journal, state)
            worker.run(['docker', 'image', 'load', '-i', str(folder / 'images.tar')], timeout=86400, capture_output=True)
            for container in containers:
                body = copy.deepcopy(container['Config'])
                body['Image'] = container['SnapshotImage']
                body['Labels'] = remap_labels(body.get('Labels'), manifest['id'], identifier)
                host = copy.deepcopy(container['HostConfig'])
                host['Binds'] = [remap_source(b.split(':', 1)[0]) + ':' + b.split(':', 1)[1] for b in host.get('Binds') or []]
                for mount in host.get('Mounts') or []:
                    if mount.get('Type') in ('bind','volume'):
                        source = mount.get('Source')
                        if not source and mount['Type'] == 'volume':
                            actual = next((entry for entry in container.get('Mounts', [])
                                           if entry['Type'] == 'volume' and entry['Destination'] == mount['Target']), None)
                            if actual is None:
                                raise ValueError('Anonymous volume mount is missing from the backup')
                            source = actual['Name']
                        mount['Source'] = remap_source(source)
                # Docker's anonymous volumes are absent from Binds/Mounts; explicitly
                # attach their copied replacements instead of silently allocating empty ones.
                destinations = {entry.split(':')[1] for entry in host['Binds']}
                destinations.update(entry['Target'] for entry in host.get('Mounts') or [])
                for mount in container.get('Mounts', []):
                    if mount['Type'] == 'volume' and mount['Destination'] not in destinations:
                        host.setdefault('Mounts', [])
                        if host['Mounts'] is None:
                            host['Mounts'] = []
                        host['Mounts'].append({'Type': 'volume', 'Source': volume_names[mount['Name']],
                                              'Target': mount['Destination'], 'ReadOnly': not mount.get('RW', True)})
                for bindings in (host.get('PortBindings') or {}).values():
                    for binding in bindings or []:
                        binding['HostIp'] = info['address']
                if 'interfaces' in info and not worker.node_runtime.load().rootless:
                    from dctlib.endpoints import port_bindings
                    service = body['Labels']['com.docker.compose.service']
                    host['PortBindings'] = port_bindings(runtime['services'][service].get('ports', []))
                host['NetworkMode'] = network_names.get(host.get('NetworkMode'), host.get('NetworkMode'))
                if manifest.get('runtime', {}).get('mode') == 'rootless' and not worker.node_runtime.load().rootless:
                    service = body['Labels']['com.docker.compose.service']
                    host['PortBindings'] = worker.node_runtime.port_bindings(runtime['services'][service].get('ports', []), info['address'])
                    body['ExposedPorts'] = dict(body.get('ExposedPorts') or {}, **{key: {} for key in host['PortBindings']})
                # A restored/clone guest remains stopped independently of daemon restarts.
                host['RestartPolicy'] = {'Name':'no', 'MaximumRetryCount':0}
                body['HostConfig'] = host
                body['NetworkingConfig'] = {'EndpointsConfig':{network_names[n]:{'Aliases':[a for a in e.get('Aliases') or [] if a != container['Id'][:12]]}
                    for n,e in container.get('NetworkSettings',{}).get('Networks',{}).items()}}
                service = body['Labels'].get('com.docker.compose.service')
                number = body['Labels'].get('com.docker.compose.container-number','1')
                name = 'bd-' + identifier + '-' + service + '-' + number
                result = worker.docker_engine('POST','/containers/create?name='+quote(name,safe=''),body)
                state['containers'].append(result['Id']); write(journal,state)
            # Store a canonical source so future deployment edits use the independent mounts.
            request.update(mode='compose', compose=json.dumps(runtime), address=info['address'], cidr=info['cidr'], hwaddr=info['hwaddr'])
            write(project / 'request.json', request)
            write(project / 'compose.runtime.json', runtime)
            (project / 'compose.source.yaml').write_text(request['compose']); os.chmod(project / 'compose.source.yaml',0o600)
            write(project / 'snapshot-restored.json', {'name':'backup','containers':state['containers'],'address':info['address']})
            policies = worker.ROOT.parent / 'restart-paused'
            policies.mkdir(parents=True, exist_ok=True, mode=0o700)
            write(policies / (identifier + '.json'), {'paused': True})
            info['status'] = 'stopped'; worker.write_json(metadata_path,info)
            firewall = folder / 'configuration' / 'firewall.fw'
            if live_firewall is None:
                worker.run(['perl', '-MPVE::Firewall', '-e', 'PVE::Firewall::lock_vmfw_conf($ARGV[0],10,sub { if (-f $ARGV[1]) { PVE::Tools::file_set_contents("/etc/pve/firewall/$ARGV[0].fw",PVE::Tools::file_get_contents($ARGV[1])); } else { unlink "/etc/pve/firewall/$ARGV[0].fw"; } });', str(vmid), str(firewall)])
            (project / 'allocated-volume.json').unlink(missing_ok=True)
            state['phase']='committed'; write(journal,state)
            journal.unlink()
            for container in containers:
                worker.run(['docker','image','rm','--force',container['SnapshotTag']],check=False,capture_output=True)
            worker.refresh(node)
        except BaseException:
            cleanup_import(identifier)
            raise
    return info


def cleanup_import(identifier, keep_reservation=False):
    journal = worker.ROOT.parent / 'imports' / (worker.project_id(identifier)+'.json')
    if not journal.exists():
        return
    state = read(journal)
    if state.get('id') != identifier:
        raise ValueError('Invalid DCT import journal')
    if state.get('phase') == 'committed':
        journal.unlink(); return
    if state.get('vmid') and state.get('restore_firewall'):
        firewall = Path('/etc/pve/firewall') / (str(int(state['vmid'])) + '.fw')
        if state.get('firewall_before') is None:
            firewall.unlink(missing_ok=True)
        else:
            firewall.write_text(state['firewall_before'])
    owned = {c['id'] for c in worker.container_records(identifier)}
    if owned:
        worker.run(['docker','rm','-f',*sorted(owned)],capture_output=True)
    for name in state['networks']:
        inspected = worker.run(['docker','network','inspect',name],capture_output=True,check=False)
        if inspected.returncode:
            continue
        network = json.loads(inspected.stdout)[0]
        if (network.get('Labels') or {}).get('com.docker.compose.project') != 'bd-'+identifier or network.get('Containers'):
            raise ValueError('Cannot remove a foreign or used import network')
        worker.run(['docker','network','rm',name],capture_output=True)
    for name in state['volumes']:
        inspected = worker.run(['docker','volume','inspect',name],capture_output=True,check=False)
        if inspected.returncode:
            continue
        volume = json.loads(inspected.stdout)[0]
        if (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-'+identifier:
            raise ValueError('Cannot remove a foreign import volume')
        worker.run(['docker','volume','rm',name],capture_output=True)
    for image in state.get('images', []):
        source = worker.project_id(state['source'])
        if not re.fullmatch('betterdocker-export/' + source + ':[0-9a-f]{32}', image['tag']):
            raise ValueError('Invalid import image ownership')
        inspected = worker.run(['docker','image','inspect','--format','{{.Id}}',image['tag']],capture_output=True,check=False)
        if not inspected.returncode and inspected.stdout.strip() == image['id']:
            worker.run(['docker','image','rm','--force',image['tag']],capture_output=True,check=False)
    path = worker.METADATA / (identifier+'.json')
    if path.exists():
        worker.remove_network(read(path))
        if worker.storage_config(identifier) or (worker.ROOT / identifier / 'allocated-volume.json').exists():
            worker.storage_unmount(identifier)
            volid = state.get('volid') or read(worker.ROOT / identifier / 'allocated-volume.json')['volid']
            worker.pve_storage(identifier,'free',volid=volid)
        unit=worker.capture(['systemd-escape','--path','--suffix=mount',str(worker.ROOT / identifier / 'storage-data')]).strip()
        worker.run(['systemctl','disable',unit],check=False,capture_output=True)
        (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
        worker.run(['systemctl','daemon-reload'],capture_output=True)
        path.unlink()
    shutil.rmtree(worker.ROOT / identifier)
    if not keep_reservation:
        worker.release_id(identifier)
    (worker.ROOT.parent / 'restart-paused' / (identifier + '.json')).unlink(missing_ok=True)
    journal.unlink()


def clone(identifier, node, destination, options):
    with tempfile.TemporaryDirectory(prefix='betterdocker-clone-') as temporary:
        folder = Path(temporary) / 'state'
        capture_state(identifier, node, folder, digest=options.get('digest'))
        return restore_state(folder, destination, node, options.get('deployment', {}), options.get('storage', ''), trusted_hook=True)


def make_template(identifier, node, digest=None):
    with operation(identifier, 'template'), worker.locked(identifier):
        info = worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        worker.check_protection(info, "can't convert protected DCT to a template")
        if worker.load_pending(worker.ROOT / identifier) or worker.load_pending_network(worker.ROOT / identifier):
            raise ValueError('Apply or revert pending settings before converting the DCT to a template')
        if any(c['state'] in ('running', 'paused', 'restarting') for c in worker.container_records(identifier)):
            raise ValueError('Stop the DCT before converting it to a template')
        # Validate portability before making the guest non-bootable.
        owned_mounts(identifier, worker.snapshot_containers(identifier))
        info.pop('deployment', None)
        info.pop('digest', None)
        info['template'] = True
        info['onboot'] = 0
        worker.write_json(worker.METADATA / (identifier + '.json'), info)
        records = worker.container_records(identifier)
        if records:
            worker.run(['docker', 'update', '--restart=no', *[c['id'] for c in records]], capture_output=True)
        return info


def main():
    if os.geteuid():
        raise SystemExit('Root required')
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('capture','restore','pack','unpack','quiesce','cleanup-capture','cleanup-import','clone','template'))
    parser.add_argument('values',nargs='*')
    parser.add_argument('--lock-fd',type=int)
    parser.add_argument('--keep-journal',action='store_true')
    parser.add_argument('--paused',action='store_true')
    args=parser.parse_args()
    if args.action=='capture':
        capture_state(*args.values,lock_fd=args.lock_fd,keep_journal=args.keep_journal,paused=args.paused)
    elif args.action=='restore':
        folder,identifier,node,options=args.values
        value=read(options); restore_state(folder,identifier,node,value.get('deployment',{}),value.get('storage',''),lock_fd=args.lock_fd)
    elif args.action=='quiesce': quiesce(*args.values,lock_fd=args.lock_fd)
    elif args.action=='clone':
        identifier,node,destination,options=args.values
        clone(identifier,node,destination,read(options))
    elif args.action=='template': make_template(*args.values)
    elif args.action=='pack': pack_state(*args.values)
    elif args.action=='unpack': unpack_state(*args.values)
    elif args.action=='cleanup-capture': cleanup_capture(*args.values)
    elif args.action=='cleanup-import': cleanup_import(*args.values)

if __name__=='__main__': main()
