"""Recreate stopped bindings without discarding a container's writable layer."""
import copy
import json
from urllib.parse import quote


def reconcile(info):
    from . import projects as w
    directory = w.ROOT / info['id']
    marker = directory / 'network-reconcile.json'
    if not marker.exists():
        return
    w.require_stopped(w.container_records(info['id']))
    saved = json.loads(marker.read_text())
    if 'containers' not in saved:
        rows = w.snapshot_containers(info['id'])
        saved['containers'] = []
        for row in rows:
            tag = 'betterdocker-rebind:' + info['id'] + '-' + row['Id'][:12]
            # Journal the tag before committing, so an interrupted commit can be retried.
            saved['containers'].append({'source': row, 'tag': tag})
        w.write_json(marker, saved)
    runtime = json.loads((directory / 'compose.runtime.json').read_text())
    for entry in saved['containers']:
        row = entry['source']
        if not entry.get('image'):
            entry['image'] = w.capture(['docker', 'commit', row['Id'], entry['tag']]).strip()
            w.write_json(marker, saved)
        if entry.get('created'):
            continue
        name = row['Name'].lstrip('/')
        current = w.run(['docker', 'inspect', name], check=False, capture_output=True)
        if current.returncode == 0:
            existing = json.loads(current.stdout)[0]
            if existing['Id'] != row['Id']:
                if (existing['Config'].get('Labels') or {}).get('betterdocker.rebind') != row['Id']:
                    raise ValueError('Network recovery container name is owned by another operation')
                entry['created'] = existing['Id']; w.write_json(marker, saved)
                continue
            w.run(['docker', 'rm', row['Id']], capture_output=True)
        body = copy.deepcopy(row['Config'])
        body['Image'] = entry['image']
        body.setdefault('Labels', {})['betterdocker.rebind'] = row['Id']
        host = copy.deepcopy(row['HostConfig'])
        service = body['Labels']['com.docker.compose.service']
        from .endpoints import port_bindings
        host['PortBindings'] = port_bindings(runtime['services'][service].get('ports', [])) if not w.node_runtime.load().rootless else {}
        storage_bindings(host, runtime['services'][service], w.storage_config(info['id']))
        body['HostConfig'] = host
        body['NetworkingConfig'] = {'EndpointsConfig': {name: {'Aliases': [a for a in net.get('Aliases') or [] if a != row['Id'][:12]]}
            for name, net in row.get('NetworkSettings', {}).get('Networks', {}).items()}}
        # Anonymous volume mounts must survive removing the original container.
        destinations = {b.split(':')[1] for b in host.get('Binds') or []}
        destinations.update(m['Target'] for m in host.get('Mounts') or [])
        for mount in row.get('Mounts', []):
            if mount['Type'] == 'volume' and mount['Destination'] not in destinations:
                host['Mounts'] = host.get('Mounts') or []
                host['Mounts'].append({'Type': 'volume', 'Source': mount['Name'], 'Target': mount['Destination'], 'ReadOnly': not mount.get('RW', True)})
        entry['created'] = w.docker_engine('POST', '/containers/create?name=' + quote(name, safe=''), body)['Id']
        w.write_json(marker, saved)
    restored = directory / 'snapshot-restored.json'
    if restored.exists():
        state = json.loads(restored.read_text()); state.update(containers=[r['created'] for r in saved['containers']], address=info['address'],
            addresses=sorted({p['host_ip'] for service in runtime['services'].values() for p in service.get('ports', [])}))
        w.write_json(restored, state)
    marker.unlink()
    for entry in saved['containers']:
        w.run(['docker', 'image', 'rm', entry['tag']], check=False, capture_output=True)


def storage_bindings(host, service, storage):
    """Apply pending managed-disk mounts when preserving a container image."""
    destinations = {mount['target']: mount for mount in service.get('volumes', [])
        if mount.get('type') == 'bind' and mount.get('source') in storage.get('mapping', {}).values()}
    if not destinations:
        return
    host['Binds'] = [value for value in host.get('Binds') or [] if value.split(':')[1] not in destinations]
    host['Mounts'] = [value for value in host.get('Mounts') or [] if value.get('Target') not in destinations]
    host['Binds'].extend(mount['source'] + ':' + target + (':ro' if mount.get('read_only') else ':rw')
        for target, mount in destinations.items())
