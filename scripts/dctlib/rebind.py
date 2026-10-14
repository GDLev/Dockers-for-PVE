"""Recreate stopped bindings without discarding a container's writable layer."""
from .secure_io import read_json
import copy
import json
import re
from urllib.parse import quote


def reconcile(info):
    from . import projects as w
    marker = w.ROOT / info['id'] / 'network-reconcile.json'
    if not marker.exists():
        return
    w.require_stopped(w.container_records(info['id']))
    if 'containers' in read_json(marker):
        # Resume journals created by pre-transaction releases before upgrading.
        return legacy_reconcile(info)
    from .service_recreate import replace_bindings, prune_images
    replace_bindings(info)
    marker.unlink()
    prune_images(info['id'])


def legacy_reconcile(info):
    from . import projects as w
    directory = w.ROOT / info['id']
    marker = directory / 'network-reconcile.json'
    if not marker.exists():
        return
    w.require_stopped(w.container_records(info['id']))
    saved = read_json(marker)
    if 'containers' not in saved:
        rows = w.snapshot_containers(info['id'])
        saved['containers'] = []
        for row in rows:
            tag = 'betterdocker-rebind:' + info['id'] + '-' + row['Id'][:12]
            # Journal the tag before committing, so an interrupted commit can be retried.
            saved['containers'].append({'source': row, 'tag': tag})
        w.write_json(marker, saved)
    runtime = read_json(directory / 'compose.runtime.json')
    for entry in saved['containers']:
        row = entry['source']
        if not entry.get('image'):
            entry['image'] = w.run(['docker', 'commit', row['Id'], entry['tag']], timeout=86400, capture_output=True).stdout.strip()
            w.write_json(marker, saved)
        if entry.get('created'):
            continue
        name = row['Name'].lstrip('/')
        current = w.run(['docker', 'inspect', name], check=False, capture_output=True)
        if current.returncode and not re.search(r'no such (?:object|container)\b', current.stderr or '', re.IGNORECASE):
            raise ValueError('Cannot inspect network recovery container: ' + name)
        if current.returncode == 0:
            existing = json.loads(current.stdout)[0]
            if existing['Id'] != row['Id']:
                if (existing['Config'].get('Labels') or {}).get('betterdocker.rebind') != row['Id']:
                    raise ValueError('Network recovery container name is owned by another operation')
                entry['created'] = existing['Id']; w.write_json(marker, saved)
                continue
            w.run(['docker', 'rm', row['Id']], capture_output=True)
        body = preserved_body(row, entry['image'])
        body.setdefault('Labels', {})['betterdocker.rebind'] = row['Id']
        host = body['HostConfig']
        service = body['Labels']['com.docker.compose.service']
        from .endpoints import port_bindings
        host['PortBindings'] = port_bindings(runtime['services'][service].get('ports', [])) if not w.node_runtime.load().rootless else {}
        storage_bindings(host, runtime['services'][service], w.storage_config(info['id']))
        entry['created'] = w.docker_engine('POST', '/containers/create?name=' + quote(name, safe=''), body)['Id']
        w.write_json(marker, saved)
    restored = directory / 'snapshot-restored.json'
    if restored.exists():
        state = read_json(restored); state.update(containers=[r['created'] for r in saved['containers']], address=info['address'],
            addresses=sorted({p['host_ip'] for service in runtime['services'].values() for p in service.get('ports', [])}))
        w.write_json(restored, state)
    marker.unlink()
    for entry in saved['containers']:
        w.run(['docker', 'image', 'rm', entry['tag']], check=False, capture_output=True)


def preserved_body(row, image):
    """Retain process configuration, network aliases and anonymous volume identity."""
    body = copy.deepcopy(row['Config'])
    body['Image'] = image
    host = body['HostConfig'] = copy.deepcopy(row['HostConfig'])
    body['NetworkingConfig'] = {'EndpointsConfig': network_endpoints(row)}
    # Compose stores anonymous mounts with an empty Source. Reusing that
    # request would allocate a new volume instead of retaining the old one.
    volumes = {m['Destination']: m['Name'] for m in row.get('Mounts', []) if m['Type'] == 'volume'}
    for mount in host.get('Mounts') or []:
        if mount.get('Type') == 'volume' and mount['Target'] in volumes:
            mount['Source'] = volumes[mount['Target']]
    destinations = {b.split(':')[1] for b in host.get('Binds') or [] if ':' in b}
    destinations.update(m['Target'] for m in host.get('Mounts') or [])
    for mount in row.get('Mounts', []):
        if mount['Type'] == 'volume' and mount['Destination'] not in destinations:
            host['Mounts'] = host.get('Mounts') or []
            host['Mounts'].append({'Type': 'volume', 'Source': mount['Name'], 'Target': mount['Destination'], 'ReadOnly': not mount.get('RW', True)})
    return body


def network_endpoints(row):
    return {name: {key: copy.deepcopy(value) for key, value in endpoint.items()
        if key in ('IPAMConfig', 'Links', 'DriverOpts') and value} | {
            'Aliases': [a for a in endpoint.get('Aliases') or [] if a != row['Id'][:12]]}
        for name, endpoint in row.get('NetworkSettings', {}).get('Networks', {}).items()}


def storage_bindings(host, service, storage):
    """Apply pending managed-disk mounts when preserving a container image."""
    from . import projects as w
    from .disks import source, records
    additional = {str(source(storage.get('id', service.get('labels', {}).get('betterdocker.id', '')), key)) for key in records(storage)}
    destinations = {mount['target']: mount for mount in service.get('volumes', [])
        if mount.get('type') == 'bind' and (mount.get('source') in storage.get('mapping', {}).values() or mount.get('source') in additional)}
    def removed(value):
        path = value.split(':')[0]
        return path.startswith(str(w.ROOT)) and __import__('re').search(r'/mount-mp(?:[1-9]|1[0-5])$', path) and path not in {m['source'] for m in destinations.values()}
    host['Binds'] = [value for value in host.get('Binds') or [] if not removed(value)]
    host['Mounts'] = [value for value in host.get('Mounts') or [] if not (value.get('Type') == 'bind' and removed(value.get('Source', '')))]
    if not destinations:
        return
    host['Binds'] = [value for value in host.get('Binds') or [] if value.split(':')[1] not in destinations]
    host['Mounts'] = [value for value in host.get('Mounts') or [] if value.get('Target') not in destinations]
    host['Binds'].extend(mount['source'] + ':' + target + (':ro' if mount.get('read_only') else ':rw')
        for target, mount in destinations.items())
