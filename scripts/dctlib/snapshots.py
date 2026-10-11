"""Capture and restore DCT configuration and persistent data."""
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import runtime as node_runtime


def snapshot_name(value):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', value) or value in ('current', 'vzdump'):
        raise ValueError('Snapshot name must start with a letter and contain at most 40 letters, digits, underscores or hyphens')
    return value


def snapshot_root(identifier):
    return worker.ROOT.parent / 'snapshots' / worker.project_id(identifier)


def snapshot_tree_state(identifier, names=None):
    path = worker.ROOT / worker.project_id(identifier) / 'snapshot-tree.json'
    state = json.loads(path.read_text()) if path.exists() else {'parents': {}, 'descriptions': {}, 'current': ''}
    if names is None:
        names = {path.parent.name for path in worker.snapshot_root(identifier).glob('*/manifest.json') if not path.parent.name.startswith('.')}
    if names is not None:
        state['parents'] = {name: parent if parent in names and parent != name else ''
                            for name, parent in state.get('parents', {}).items() if name in names}
        state['descriptions'] = {name: value for name, value in state.get('descriptions', {}).items() if name in names}
        if not path.exists():
            restored = worker.ROOT / identifier / 'snapshot-restored.json'
            candidates = []
            for name in names:
                manifest = worker.snapshot_root(identifier) / name / 'manifest.json'
                if manifest.exists():
                    data = json.loads(manifest.read_text())
                    if not data.get('recovery'):
                        candidates.append((data['created'], name))
            latest = max(candidates, default=(0, ''))
            state['current'] = latest[1]
            if restored.exists() and restored.stat().st_mtime > latest[0]:
                state['current'] = json.loads(restored.read_text()).get('name', '')
        if state.get('current') not in names:
            state['current'] = ''
        # An invalid legacy sidecar must never make the tree recursive.
        for name in state['parents']:
            seen, parent = {name}, state['parents'][name]
            while parent:
                if parent in seen:
                    state['parents'][name] = ''
                    break
                seen.add(parent)
                parent = state['parents'].get(parent, '')
    return state


def snapshot_tree_write(identifier, state):
    worker.write_json(worker.ROOT / identifier / 'snapshot-tree.json', state)


def snapshot_list(identifier, node):
    import hashlib
    info = worker.detail(identifier, node)
    rows = []
    for path in worker.snapshot_root(identifier).glob('*/manifest.json'):
        try:
            data = json.loads(path.read_text())
            if data.get('version') in (1, 2) and data.get('id') == identifier and data.get('name') == path.parent.name:
                rows.append({key: data[key] for key in ('name', 'created', 'description', 'size', 'recovery')})
        except (OSError, ValueError, KeyError):
            continue
    rows.sort(key=lambda row: (row['created'], row['name']))
    state = worker.snapshot_tree_state(identifier, {row['name'] for row in rows})
    for row in rows:
        row.update(parent=state['parents'].get(row['name'], ''), snaptime=row['created'],
                   description=state['descriptions'].get(row['name'], row['description']))
    current = {'name': 'current', 'parent': state.get('current', ''), 'description': '',
               'running': info.get('status') in ('running', 'partial')}
    current['digest'] = hashlib.sha256(json.dumps([rows, current], sort_keys=True).encode()).hexdigest()
    return rows + [current]


def snapshot_config(identifier, node, name):
    worker.detail(identifier, node)
    folder = worker.snapshot_root(identifier) / worker.snapshot_name(name)
    if folder.is_symlink():
        raise ValueError('Invalid snapshot directory')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('id') != identifier or manifest.get('name') != name:
        raise ValueError('Invalid snapshot manifest')
    state = worker.snapshot_tree_state(identifier)
    metadata = json.loads((folder / 'configuration' / 'metadata.json').read_text())
    config = json.loads((folder / 'configuration' / 'compose.runtime.json').read_text())
    # Native snapshot details are an inventory, not a way to read deployment secrets.
    result = {'description': state.get('descriptions', {}).get(name, manifest['description']),
              'snaptime': manifest['created'], 'hostname': metadata.get('name', ''),
              'net0': 'bridge=' + metadata.get('bridge', '') + ',ip=' + metadata.get('address', ''),
              'protection': int(bool(metadata.get('protection')))}
    for service, settings in sorted(config['services'].items()):
        for key in ('image', 'cpus', 'cpu_shares', 'mem_limit', 'memswap_limit', 'pids_limit', 'restart'):
            if key in settings:
                result[service + '.' + key] = str(settings[key])
        result[service + '.replicas'] = str(settings.get('deploy', {}).get('replicas', 1))
        limits = settings.get('deploy', {}).get('resources', {}).get('limits', {})
        for key in ('cpus', 'memory', 'pids'):
            if key in limits:
                result[service + '.limit.' + key] = str(limits[key])
        for key in ('dns', 'dns_search', 'storage_opt'):
            if settings.get(key):
                result[service + '.' + key] = json.dumps(settings[key], sort_keys=True)
        for index, mount in enumerate(settings.get('volumes', [])):
            result[service + '.mount' + str(index)] = str(mount.get('source', '')) + ',mp=' + str(mount.get('target', '')) + (',ro=1' if mount.get('read_only') else '')
        for index, port in enumerate(settings.get('ports', [])):
            result[service + '.port' + str(index)] = str(port.get('host_ip', '')) + ':' + str(port.get('published', '')) + ':' + str(port.get('target', '')) + '/' + str(port.get('protocol', 'tcp'))
    storage = folder / 'configuration' / 'storage.json'
    if storage.exists():
        result['data_disk'] = str(json.loads(storage.read_text())['size_mib']) + ' MiB'
    result['digest'] = hashlib.sha256(json.dumps([identifier, name, result['description']], separators=(',', ':')).encode()).hexdigest()
    return result


def snapshot_edit(identifier, node, name, description, digest=None):
    worker.snapshot_name(name)
    if len(description) > 4096:
        raise ValueError('Snapshot description cannot exceed 4096 characters')
    with worker.locked(identifier):
        current = worker.snapshot_config(identifier, node, name)
        if digest and digest != current['digest']:
            raise ValueError('detected modified configuration - file checksum changed, please try again')
        state = worker.snapshot_tree_state(identifier)
        state.setdefault('descriptions', {})[name] = description
        worker.snapshot_tree_write(identifier, state)


def snapshot_containers(identifier, allow_running=False, quiesced=False):
    records = worker.container_records(identifier)
    allowed = {'created', 'exited'} | ({'running', 'paused'} if allow_running else set()) | ({'paused'} if quiesced else set())
    if any(row['state'] not in allowed for row in records):
        raise ValueError('DCT is changing state; wait until its services are stable')
    return json.loads(worker.capture(['docker', 'inspect', *[row['id'] for row in records]])) if records else []


@contextlib.contextmanager
def snapshot_quiesce(identifier):
    records = worker.container_records(identifier)
    if any(row['state'] not in ('running', 'created', 'exited') for row in records):
        raise ValueError('DCT is changing state or already paused; wait before taking a snapshot')
    paused = []
    def resume():
        failures = []
        for container in list(paused):
            try:
                worker.run(['docker', 'unpause', container], capture_output=True)
                paused.remove(container)
            except (OSError, subprocess.SubprocessError):
                failures.append(container)
        if failures:
            raise ValueError('Could not resume all DCT services after snapshot; check Task History')
    try:
        for row in records:
            if row['state'] == 'running':
                worker.run(['docker', 'pause', row['id']], capture_output=True)
                paused.append(row['id'])
        # Stopped containers can still have dirty filesystem buffers from shutdown.
        # Flush them too before the storage backend captures its block-level snapshot.
        os.sync()
        yield resume
    finally:
        resume()


def snapshot_mounts(identifier, containers, allow_missing=False):
    """Resolve actual Docker mounts, never assume that a Compose volume is private."""
    folder = worker.ROOT / identifier
    owned = {row['Id'] for row in containers}
    sources = {}
    for row in containers:
        for mount in row.get('Mounts', []):
            if not mount.get('RW', True) or mount['Type'] == 'tmpfs':
                continue
            source = Path(mount['Source'])
            if mount['Type'] == 'bind':
                if source.is_symlink() or not source.resolve().is_relative_to(folder.resolve()) or source.resolve() == folder.resolve():
                    raise ValueError('Snapshots require project-owned writable mounts; external host paths are not supported')
                if source.name in ('storage.ext4', 'request.json', 'compose.runtime.json', 'compose.source.yaml', '.env', 'settings.json', 'pending.json', 'pending-network.json', 'storage.json'):
                    raise ValueError('Deployment files cannot also be writable application mounts')
            elif mount['Type'] == 'volume':
                volume = json.loads(worker.capture(['docker', 'volume', 'inspect', mount['Name']]))[0]
                if volume.get('Driver') != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                    raise ValueError('Snapshots require local volumes owned by this DCT')
                users = set(worker.capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + mount['Name']]).split())
                if users - owned:
                    raise ValueError('A writable volume is shared with another project')
                if source.resolve() != Path(volume['Mountpoint']).resolve():
                    raise ValueError('Volume mount does not match Docker metadata')
                node_runtime.check_volume_path(source, mount['Name'])
            else:
                raise ValueError('Unsupported writable snapshot mount')
            if not source.is_dir() and not (allow_missing and not source.exists()):
                raise ValueError('Snapshots support writable directory mounts only')
            sources[str(source)] = {'source': str(source), 'type': mount['Type'], 'volume': mount.get('Name', '')}
    paths = sorted(sources, key=lambda value: len(Path(value).parts))
    # Copy overlapping bind mounts only once; retain the exact volume ownership checks above.
    return [sources[value] for value in paths if not any(Path(value).is_relative_to(Path(parent)) for parent in paths if parent != value)]


def snapshot_digest(folder):
    """Inventory hashes include symlink targets and empty directories, without following links."""
    import hashlib
    result = {}
    for base, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            key = str(path.relative_to(folder))
            if key == 'checksums.json':
                continue
            if path.is_symlink():
                result[key] = 'link:' + os.readlink(path)
            elif path.is_dir():
                result[key] = 'directory'
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                result[key] = digest.hexdigest()
            else:
                raise ValueError('Snapshot contains an unsupported special file')
    return result


def snapshot_check(identifier, name):
    folder = worker.snapshot_root(identifier) / worker.snapshot_name(name)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Snapshot does not exist')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('version') not in (1, 2) or manifest.get('id') != identifier or manifest.get('name') != name:
        raise ValueError('Invalid snapshot manifest')
    if worker.snapshot_digest(folder) != json.loads((folder / 'checksums.json').read_text()):
        raise ValueError('Snapshot integrity check failed; nothing was restored')
    return folder, manifest


def recover_snapshot_transaction(identifier, restoring=False):
    project = worker.ROOT / identifier
    journal = project / 'snapshot-transaction.json'
    if not journal.exists():
        return
    state = json.loads(journal.read_text())
    if state['action'] == 'restore':
        if not restoring:
            raise ValueError('Interrupted rollback: retry restoration of snapshot ' + state['name'])
        return
    owned = {row['id']: row for row in worker.container_records(identifier)}
    for cid in state.get('active', []):
        if cid in owned and owned[cid]['state'] == 'paused':
            worker.run(['docker', 'unpause', cid], capture_output=True)
    complete = (worker.snapshot_root(identifier) / state['name'] / 'manifest.json').exists()
    if complete and state.get('tree_state') is not None:
        tree = worker.snapshot_tree_state(identifier)
        tree.setdefault('parents', {}).setdefault(state['name'], state['tree_state'].get('current', ''))
        if not state.get('recovery'):
            tree['current'] = state['name']
        worker.snapshot_tree_write(identifier, tree)
    if not complete and state.get('native'):
        worker.pve_storage(identifier, 'snapshot-delete', volid=state['native']['volid'], name=state['name'])
    temporary = state.get('temporary')
    if temporary:
        path = Path(temporary)
        if path.parent != worker.snapshot_root(identifier) or not path.name.startswith('.creating-') or path.is_symlink():
            raise ValueError('Invalid snapshot recovery path')
        if path.exists():
            shutil.rmtree(path)
    for tag in state.get('tags', []):
        if not re.fullmatch('betterdocker-snapshot/' + identifier + ':[0-9a-f]{32}', tag):
            raise ValueError('Invalid snapshot recovery image')
        worker.run(['docker', 'image', 'rm', tag], check=False, capture_output=True)
    journal.unlink()


def snapshot_restore_network(identifier, node, configs, current, check_only=False):
    saved = json.loads((configs / 'metadata.json').read_text())
    if 'interfaces' in saved or 'interfaces' in current:
        from .endpoints import interfaces, validate, resolved
        from .endpoint_network import preflight
        candidate = dict(saved, id=identifier, vmid=current['vmid'], node=node, status='stopped', interfaces=validate(interfaces(saved)))
        preflight(candidate, identifier)
        if check_only:
            return candidate
        project = worker.ROOT / identifier
        journal = project / 'network-transaction.json'
        if not journal.exists():
            from .endpoint_network import re_dhcp_file
            files = {path.name: path.read_text() for path in project.iterdir()
                if path.name in ('request.json', 'compose.runtime.json', 'dhcp-address.json', 'dhclient.leases', 'dhclient.conf') or re_dhcp_file(path.name)}
            worker.write_json(journal, {'metadata': {key: value for key, value in current.items() if key not in ('deployment', 'digest')},
                'files': files, 'endpoints': True})
        worker.remove_network(current)
        worker.write_json(worker.METADATA / (identifier + '.json'), candidate)
        worker.ensure_address(candidate)
        return candidate
    saved.setdefault('firewall', 0)
    if not all(key in saved for key in worker.NETWORK_KEYS):
        return dict(saved, **{key: current[key] for key in ('id', 'vmid', 'node', 'address') if key in current})
    request = json.loads((configs / 'request.json').read_text())
    data = worker.validate(dict(request, **{key: saved[key] for key in worker.NETWORK_KEYS}, address=saved.get('cidr', '')))
    candidate = dict(saved, id=identifier, vmid=current['vmid'], node=node, interface=current['interface'], status='stopped')
    changed = any(candidate.get(key) != current.get(key) for key in worker.NETWORK_KEYS)
    if changed:
        _, subnets = worker.bridge_addresses(data['bridge'])
        if data['ipv4mode'] == 'static':
            address = ipaddress.IPv4Interface(data['cidr'])
            if not any(address.ip in subnet and address.network.prefixlen == subnet.prefixlen for subnet in subnets):
                raise ValueError('Snapshot address no longer matches the bridge subnet')
            worker.check_address(data['address'], data['bridge'], identifier, current.get('interface'), probe=True)
        if any(row.get('id') != identifier and row.get('hwaddr') == data['hwaddr'] for row in worker.summaries()):
            raise ValueError('Snapshot MAC address is already in use')
        bridge = json.loads(worker.capture(['ip', '-j', 'link', 'show', 'dev', data['bridge']]))[0]
        if data['mtu'] and data['mtu'] > bridge['mtu']:
            raise ValueError('Snapshot MTU exceeds the bridge MTU')
    if check_only:
        return candidate
    if changed:
        project = worker.ROOT / identifier
        journal = project / 'network-transaction.json'
        if not journal.exists():
            worker.write_json(journal, {'metadata': {key: value for key, value in current.items() if key not in ('deployment', 'digest')},
                'files': {name: (project / name).read_text() if (project / name).exists() else None
                          for name in ('request.json', 'compose.runtime.json', 'dhcp-address.json', 'dhclient.leases', 'dhclient.conf')}})
        worker.remove_network(current)
        for name in ('dhcp-address.json', 'dhclient.leases'):
            (project / name).unlink(missing_ok=True)
        worker.write_json(worker.METADATA / (identifier + '.json'), candidate)
        worker.ensure_address(candidate)
    elif candidate.get('ipv4mode') == 'dhcp':
        candidate['address'] = current['address']
    return candidate


def snapshot_capture(identifier, node, name, description='', recovery=False, quiesced=False, resume=None):
    worker.snapshot_name(name)
    if len(description) > 4096:
        raise ValueError('Snapshot description cannot exceed 4096 characters')
    info = worker.detail(identifier, node)
    tree_state = worker.snapshot_tree_state(identifier)
    containers = worker.snapshot_containers(identifier, quiesced=quiesced)
    if not containers:
        raise ValueError('DCT has no containers to snapshot')
    config = json.loads((worker.ROOT / identifier / 'compose.runtime.json').read_text())
    expected = sum(service.get('deploy', {}).get('replicas', 1) for service in config['services'].values())
    counts = {name: sum((row['Config'].get('Labels') or {}).get('com.docker.compose.service') == name for row in containers) for name in config['services']}
    if len(containers) != expected or any(counts[name] != service.get('deploy', {}).get('replicas', 1) for name, service in config['services'].items()):
        raise ValueError('DCT instances are incomplete; deploy all services before taking a snapshot')
    worker.storage_mount(identifier)
    mounts = worker.snapshot_mounts(identifier, containers)
    if worker.storage_config(identifier):
        point = str(worker.ROOT / identifier / 'storage-data')
        mounts = [entry for entry in mounts if not Path(entry['source']).is_relative_to(Path(point))]
        mounts.append({'source': point, 'type': 'bind', 'volume': ''})
    # Exclude nested filesystems; an application could otherwise cross ownership boundaries.
    mounted = json.loads(worker.capture(['findmnt', '-J', '-l', '-o', 'TARGET']))['filesystems']
    for entry in mounts:
        path = Path(entry['source']).resolve()
        if any(Path(item['target']).is_relative_to(path) and Path(item['target']) != path for item in mounted):
            raise ValueError('Nested filesystems in writable mounts are not supported by snapshots')
    root = worker.snapshot_root(identifier)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root.parent, 0o700)
    os.chmod(root, 0o700)
    final = root / name
    if final.exists():
        raise ValueError('Snapshot name already exists')
    temporary = Path(tempfile.mkdtemp(prefix='.creating-', dir=root))
    tags = []
    journal = worker.ROOT / identifier / 'snapshot-transaction.json'
    transaction = json.loads(journal.read_text()) if journal.exists() else {'action': 'create', 'name': name, 'active': []}
    transaction.update(temporary=str(temporary), tree_state=tree_state, recovery=recovery)
    worker.write_json(journal, transaction)
    native = None
    try:
        settings = worker.storage_config(identifier)
        if settings.get('native_snapshots'):
            native = {'volid': settings['volid'], 'name': name}
            transaction['native'] = native
            worker.write_json(journal, transaction)
            worker.pve_storage(identifier, 'snapshot', **native)
            point = worker.ROOT / identifier / 'storage-data'
            mounts = [entry for entry in mounts if Path(entry['source']) != point]
        files = temporary / 'configuration'
        files.mkdir(mode=0o700)
        for filename in ('request.json', 'compose.source.yaml', '.env', 'compose.runtime.json', 'settings.json', 'pending.json', 'storage.json', 'notes.txt'):
            source = worker.ROOT / identifier / filename
            if source.exists():
                shutil.copy2(source, files / filename)
        public = {key: value for key, value in info.items() if key != 'deployment'}
        worker.write_json(files / 'metadata.json', public)
        data = temporary / 'data'
        data.mkdir(mode=0o700)
        for index, entry in enumerate(mounts):
            entry['copy'] = 'data/' + str(index)
            node_runtime.copy_data(entry['source'], temporary / entry['copy'], runner=worker.run)
            node_runtime.remap_tree(temporary / entry['copy'], node_runtime.load(), node_runtime.Runtime())
        for index, container in enumerate(containers):
            tag = 'betterdocker-snapshot/' + identifier + ':' + uuid.uuid4().hex
            tags.append(tag)
            transaction['tags'] = tags
            worker.write_json(journal, transaction)
            worker.run(['docker', 'commit', '--pause=false', container['Id'], tag], timeout=86400, capture_output=True)
            container['SnapshotImage'] = worker.capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag]).strip()
            container['SnapshotTag'] = tag
        if resume:
            resume()
        worker.run(['docker', 'image', 'save', '-o', str(temporary / 'images.tar'), *tags], timeout=86400)
        os.chmod(temporary / 'images.tar', 0o600)
        network_names = sorted({network for container in containers for network in container.get('NetworkSettings', {}).get('Networks', {})})
        networks = json.loads(worker.capture(['docker', 'network', 'inspect', *network_names])) if network_names else []
        worker.write_json(temporary / 'networks.json', networks)
        worker.write_json(temporary / 'containers.json', containers)
        size = sum(path.stat().st_size for path in temporary.rglob('*') if path.is_file() and not path.is_symlink())
        worker.write_json(temporary / 'manifest.json', {'version': 2 if node_runtime.load().rootless else 1, 'runtime': node_runtime.load().identity(), 'id': identifier, 'name': name,
                   'created': int(time.time()), 'description': description, 'recovery': recovery,
                   'mounts': mounts, 'size': size, 'native_storage': native})
        worker.write_json(temporary / 'checksums.json', worker.snapshot_digest(temporary))
        os.replace(temporary, final)
        state = tree_state
        state.setdefault('parents', {})[name] = state.get('current', '')
        if not recovery:
            state['current'] = name
        worker.snapshot_tree_write(identifier, state)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        for tag in tags:
            worker.run(['docker', 'image', 'rm', tag], check=False, capture_output=True)
        if native and not final.exists():
            worker.pve_storage(identifier, 'snapshot-delete', **native)
        journal.unlink(missing_ok=True)
    print('Snapshot created: ' + name)
    return name


def docker_engine(method, path, body):
    """Only the local Docker socket is used; never log Config/Env or daemon responses."""
    return node_runtime.request(method, path, body)


def snapshot_apply(identifier, node, name, check_only=False):
    from urllib.parse import quote
    folder, manifest = worker.snapshot_check(identifier, name)
    current = worker.detail(identifier, node)
    existing = worker.snapshot_containers(identifier, allow_running=check_only)
    # Validate both source and target ownership before touching containers or application files.
    worker.snapshot_mounts(identifier, existing, allow_missing=True)
    saved = json.loads((folder / 'containers.json').read_text())
    configs = folder / 'configuration'
    if not worker.trusted_management():
        worker.validate_delegated_config(json.loads((configs / 'compose.runtime.json').read_text()), identifier)
        worker.validate_delegated_containers(saved, identifier)
        for mount in manifest['mounts']:
            if mount['type'] == 'bind':
                worker.safe_bind(mount['source'], identifier)
        if (folder / 'networks.json').exists():
            worker.validate_delegated_networks(json.loads((folder / 'networks.json').read_text()))
    worker.project_limits.check_directory(worker.ROOT / identifier, json.loads((configs / 'compose.runtime.json').read_text()))
    worker.project_limits.check_containers(saved, worker.project_limits.load(worker.ROOT / identifier))
    before_storage = worker.storage_config(identifier)
    native = manifest.get('native_storage')
    saved_storage = json.loads((configs / 'storage.json').read_text()) if (configs / 'storage.json').exists() else {}
    if saved_storage.get('volid') != before_storage.get('volid'):
        raise ValueError('Snapshot data disk differs from the current disk')
    if native:
        if native['volid'] != before_storage.get('volid') or native['name'] != name:
            raise ValueError('Snapshot disk ownership changed')
        worker.pve_storage(identifier, 'rollback-check', **native)
    worker.snapshot_restore_network(identifier, node, configs, current, check_only=True)
    mounted_targets = json.loads(worker.capture(['findmnt', '-J', '-l', '-o', 'TARGET']))['filesystems']
    for entry in manifest['mounts']:
        source = Path(entry['source'])
        if any(Path(item['target']).is_relative_to(source.resolve()) and Path(item['target']) != source.resolve() for item in mounted_targets):
            raise ValueError('Nested filesystems in snapshot targets are not supported')
        if entry['type'] == 'bind' and (source.is_symlink() or not source.resolve().is_relative_to((worker.ROOT / identifier).resolve())):
            raise ValueError('Snapshot mount ownership changed')
        if entry['type'] == 'volume':
            volume = json.loads(worker.capture(['docker', 'volume', 'inspect', entry['volume']]))[0]
            if volume.get('Driver') != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                raise ValueError('Snapshot volume ownership changed')
            users = set(worker.capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + entry['volume']]).split())
            if users - {row['Id'] for row in existing}:
                raise ValueError('Snapshot volume is now shared with another project')
    saved_networks = json.loads((folder / 'networks.json').read_text()) if (folder / 'networks.json').exists() else []
    existing_networks = set(worker.capture(['docker', 'network', 'ls', '--format', '{{.Name}}']).splitlines())
    for network in saved_networks:
        if network['Name'] not in existing_networks:
            if (network.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                raise ValueError('An external snapshot network is missing')
            body = {key: network[key] for key in ('Name', 'Driver', 'Internal', 'Attachable', 'EnableIPv6', 'IPAM', 'Options', 'Labels') if key in network}
            if not check_only:
                worker.docker_engine('POST', '/networks/create', body)
    if check_only:
        return
    worker.run(['docker', 'image', 'load', '-i', str(folder / 'images.tar')], timeout=86400, capture_output=True)
    if existing:
        worker.run(['docker', 'rm', *[row['Id'] for row in existing]], timeout=1800, capture_output=True)
    project = worker.ROOT / identifier
    metadata = worker.snapshot_restore_network(identifier, node, configs, current)
    replaced_disk = None
    if native:
        worker.storage_mount(identifier)
        worker.storage_unmount(identifier)
        worker.pve_storage(identifier, 'rollback', **native)
        worker.write_json(project / 'storage.json', saved_storage)
        worker.storage_mount(identifier)
        worker.storage_unit(identifier, saved_storage)
    elif before_storage.get('volid'):
        worker.write_json(project / 'storage.json', saved_storage)
        worker.storage_mount(identifier)
    if before_storage and (not saved_storage or before_storage['size_mib'] != saved_storage['size_mib']):
        worker.storage_mount(identifier)  # verifies that the loop device is owned by this DCT
        unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(project / 'storage-data')]).strip()
        # Remove only this DCT's dependency before unmounting; never stop the Docker daemon.
        worker.run(['systemctl', 'disable', unit], check=False, capture_output=True)
        worker.run(['systemctl', 'daemon-reload'], capture_output=True)
        # Wait for systemd's unmount job before mounting a replacement at the same path.
        worker.run(['systemctl', 'stop', unit], capture_output=True)
        if os.path.ismount(project / 'storage-data'):
            worker.run(['umount', str(project / 'storage-data')], capture_output=True)
        replaced_disk = project / ('storage-rollback-' + uuid.uuid4().hex + '.ext4')
        (project / 'storage.ext4').rename(replaced_disk)
        (project / 'storage.json').unlink()
        before_storage = {}
        if not saved_storage:
            (project / 'storage-data').rmdir()
            (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
            worker.run(['systemctl', 'daemon-reload'], capture_output=True)
    if saved_storage and not saved_storage.get('volid'):
        if not before_storage:
            image = project / 'storage.ext4'
            if image.exists():
                raise ValueError('Unmanaged data disk image exists')
            with image.open('xb') as handle:
                os.chmod(image, 0o600)
                handle.truncate(saved_storage['size_mib'] * 1048576)
            worker.run(['mkfs.ext4', '-q', '-m', '0', str(image)])
        worker.write_json(project / 'storage.json', saved_storage)
        unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(project / 'storage-data')]).strip()
        if not before_storage:
            unit_path = Path('/etc/systemd/system') / unit
            unit_path.write_text('[Unit]\nDescription=BetterDocker data disk\nBefore=' + node_runtime.load().system_unit + '\n\n[Mount]\nWhat=' + str(project / 'storage.ext4') + '\nWhere=' + str(project / 'storage-data') + '\nType=ext4\nOptions=loop,nodev,nosuid\n\n[Install]\nRequiredBy=docker.service\n'.replace('docker.service', node_runtime.load().system_unit))
            worker.run(['systemctl', 'daemon-reload'], capture_output=True)
            worker.run(['systemctl', 'enable', unit], capture_output=True)
            (project / 'storage-data').mkdir(mode=0o700, exist_ok=True)
            worker.run(['systemctl', 'start', unit], capture_output=True)
        worker.storage_mount(identifier)
    for entry in manifest['mounts']:
        target = Path(entry['source'])
        node_runtime.data_directory(target)
        with node_runtime.anchored(target if node_runtime.load().rootless else target.resolve()) as fd:
            for name in os.listdir(fd):
                if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    shutil.rmtree(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
        node_runtime.restore_data(folder / entry['copy'], target, runner=worker.run)
    for filename in ('request.json', 'compose.source.yaml', '.env', 'compose.runtime.json', 'settings.json', 'pending.json', 'pending-network.json', 'notes.txt'):
        source, target = configs / filename, project / filename
        if source.exists():
            shutil.copy2(source, target)
        else:
            target.unlink(missing_ok=True)
    metadata.update(status='stopped')
    metadata.pop('network_resume', None)
    worker.write_json(worker.METADATA / (identifier + '.json'), metadata)
    worker.rebind_runtime(project, metadata['address'])
    created = []
    for container in saved:
        body = dict(container['Config'])
        body['Image'] = container['SnapshotImage']
        body['HostConfig'] = dict(container['HostConfig'])
        if saved_storage:
            service_name = (container['Config'].get('Labels') or {}).get('com.docker.compose.service')
            runtime_config = json.loads((project / 'compose.runtime.json').read_text())
            configured = {mount['target']: mount for mount in runtime_config['services'][service_name].get('volumes', [])}
            # Data migration can be pending while stopped: use the saved runtime disk mapping.
            binds = body['HostConfig'].get('Binds') or []
            for destination, mount in configured.items():
                if mount.get('type') != 'bind' or mount.get('source') not in saved_storage['mapping'].values():
                    continue
                binds = [value for value in binds if value.split(':')[1] != destination]
                binds.append(mount['source'] + ':' + destination + (':ro' if mount.get('read_only') else ':rw'))
                body['HostConfig']['Mounts'] = [item for item in body['HostConfig'].get('Mounts') or [] if item.get('Target') != destination]
            body['HostConfig']['Binds'] = binds
        # Keep ephemeral Docker IDs/IP assignments out of create requests.
        networks = {}
        for network, endpoint in container.get('NetworkSettings', {}).get('Networks', {}).items():
            networks[network] = {key: endpoint[key] for key in ('Aliases', 'IPAMConfig', 'Links', 'DriverOpts') if endpoint.get(key)}
        body['NetworkingConfig'] = {'EndpointsConfig': networks}
        for bindings in (body['HostConfig'].get('PortBindings') or {}).values():
            for binding in bindings or []:
                if binding.get('HostIp') == current.get('address') or binding.get('HostIp') == json.loads((configs / 'metadata.json').read_text()).get('address'):
                    binding['HostIp'] = metadata['address']
        if 'interfaces' in metadata and not node_runtime.load().rootless:
            from .endpoints import port_bindings
            service = body['Labels']['com.docker.compose.service']
            runtime = json.loads((project / 'compose.runtime.json').read_text())
            body['HostConfig']['PortBindings'] = port_bindings(runtime['services'][service].get('ports', []))
        result = worker.docker_engine('POST', '/containers/create?name=' + quote(container['Name'].lstrip('/'), safe=''), body)
        created.append(result['Id'])
    for container in saved:
        if container.get('SnapshotTag'):
            worker.run(['docker', 'image', 'rm', container['SnapshotTag']], check=False, capture_output=True)
    worker.write_json(project / 'snapshot-restored.json', {'name': name, 'containers': created, 'address': metadata['address'],
        'addresses': sorted({p['host_ip'] for service in json.loads((project / 'compose.runtime.json').read_text())['services'].values() for p in service.get('ports', [])})})
    state = worker.snapshot_tree_state(identifier)
    state['current'] = name
    worker.snapshot_tree_write(identifier, state)
    for filename in ('network-transaction.json', 'network-reconcile.json', 'network-resume.json'):
        (project / filename).unlink(missing_ok=True)
    (project / 'snapshot-transaction.json').unlink(missing_ok=True)
    worker.refresh(node)
    if replaced_disk:
        replaced_disk.unlink()


def project_snapshot(identifier, node, verb, name, description=''):
    worker.snapshot_name(name)
    with worker.locked(identifier):
        worker.detail(identifier, node)
        worker.recover_storage_transaction(identifier)
        worker.recover_snapshot_transaction(identifier, restoring=verb == 'restore')
        if verb == 'restore':
            worker.recover_network_transaction(identifier, node)
        journal = worker.ROOT / identifier / 'snapshot-transaction.json'
        if journal.exists() and json.loads(journal.read_text()).get('action') == 'restore' and json.loads(journal.read_text())['name'] != name:
            raise ValueError('Retry the interrupted rollback using its original snapshot')
        if verb == 'create':
            worker.write_json(journal, {'action': 'create', 'name': name, 'active': [row['id'] for row in worker.container_records(identifier) if row['state'] == 'running']})
            try:
                with worker.snapshot_quiesce(identifier) as resume:
                    worker.snapshot_capture(identifier, node, name, description, quiesced=True, resume=resume)
            finally:
                worker.recover_snapshot_transaction(identifier)
        elif verb == 'delete':
            folder = worker.snapshot_root(identifier) / name
            if folder.is_symlink() or not (folder / 'manifest.json').exists():
                raise ValueError('Snapshot does not exist')
            manifest = json.loads((folder / 'manifest.json').read_text())
            if manifest.get('native_storage'):
                worker.pve_storage(identifier, 'snapshot-delete', **manifest['native_storage'])
            state = worker.snapshot_tree_state(identifier)
            parent = state.get('parents', {}).get(name, '')
            state['parents'] = {child: parent if ancestor == name else ancestor for child, ancestor in state.get('parents', {}).items() if child != name}
            state.setdefault('descriptions', {}).pop(name, None)
            if state.get('current') == name:
                state['current'] = parent
            shutil.rmtree(folder)
            worker.snapshot_tree_write(identifier, state)
            print('Snapshot removed: ' + name)
        elif verb == 'restore':
            # LXC validates the target, stops the guest, then rolls back without another snapshot.
            worker.snapshot_apply(identifier, node, name, check_only=True)
            records = worker.container_records(identifier)
            for row in records:
                if row['state'] == 'paused':
                    worker.run(['docker', 'unpause', row['id']], capture_output=True)
            active = [row['id'] for row in records if row['state'] in ('running', 'paused')]
            if active:
                worker.run(['docker', 'stop', '--time', '0', *active], timeout=1800, capture_output=True)
            worker.write_json(journal, {'action': 'restore', 'name': name})
            worker.snapshot_apply(identifier, node, name)
            print('Snapshot restored; DCT remains stopped.')
        else:
            raise ValueError('Invalid snapshot operation')


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
