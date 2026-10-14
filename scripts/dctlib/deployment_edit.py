"""Stopped deployment edits preserve individual replicas and rollback originals."""
from .secure_io import read_json, read_text
import copy
import json
from urllib.parse import quote


def network_names(config, identifier):
    return {key: (definition or {}).get('name') or 'bd-' + identifier + '_' + key
            for key, definition in (config.get('networks') or {'default': {}}).items()}


def resources(config, identifier, journal, marker):
    from . import projects as w
    from .service_recreate import durable_write
    networks = network_names(config, identifier)
    if w.node_runtime.load().rootless:
        w.node_runtime.prepare_bind_paths(w.ROOT / identifier, config, w.node_runtime.load())
    def reserve(kind, name):
        journal.setdefault('resources', []).append({'kind': kind, 'name': name})
        durable_write(marker, journal)
    for key, definition in (config.get('networks') or {'default': {}}).items():
        definition = definition or {}
        name = definition.get('name') or 'bd-' + identifier + '_' + key
        networks[key] = name
        result = w.run(['docker', 'network', 'inspect', name], check=False, capture_output=True)
        if result.returncode:
            if 'no such network' not in result.stderr.lower() and 'not found' not in result.stderr.lower():
                raise ValueError('Cannot inspect deployment network')
            if definition.get('external'):
                raise ValueError('External network is missing')
            reserve('network', name)
            ipam = definition.get('ipam') or {}
            mapping = {'subnet': 'Subnet', 'gateway': 'Gateway', 'ip_range': 'IPRange', 'aux_addresses': 'AuxiliaryAddresses'}
            w.docker_engine('POST', '/networks/create', {'Name': name, 'Driver': definition.get('driver', 'bridge'),
                'Internal': bool(definition.get('internal')), 'Attachable': bool(definition.get('attachable')),
                'EnableIPv6': bool(definition.get('enable_ipv6')), 'Options': definition.get('driver_opts') or {},
                'Labels': dict(definition.get('labels') or {}) | {'com.docker.compose.project': 'bd-' + identifier,
                    'betterdocker.transaction': journal['token']},
                'IPAM': {'Driver': ipam.get('driver', 'default'), 'Options': ipam.get('options') or {},
                         'Config': [{mapping[k]: v for k, v in item.items()} for item in ipam.get('config') or []]}})
        elif not definition.get('external') and (json.loads(result.stdout)[0].get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
            raise ValueError('Deployment network belongs to another project')
    for key, definition in (config.get('volumes') or {}).items():
        definition = definition or {}
        name = definition.get('name') or 'bd-' + identifier + '_' + key
        result = w.run(['docker', 'volume', 'inspect', name], check=False, capture_output=True)
        if result.returncode:
            if 'no such volume' not in result.stderr.lower():
                raise ValueError('Cannot inspect deployment volume')
            if definition.get('external'):
                raise ValueError('External volume is missing')
            reserve('volume', name)
            w.docker_engine('POST', '/volumes/create', {'Name': name, 'Driver': definition.get('driver', 'local'),
                'DriverOpts': definition.get('driver_opts') or {}, 'Labels': dict(definition.get('labels') or {}) |
                {'com.docker.compose.project': 'bd-' + identifier, 'betterdocker.transaction': journal['token']}})
        elif not definition.get('external') and (json.loads(result.stdout)[0].get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
            raise ValueError('Deployment volume belongs to another project')
    return networks


def apply(info, data, config, resolved, staging):
    from . import projects as w
    from .engine_config import create_body
    from .service_recreate import execute, defaults_state, owned
    identifier = info['id']
    directory = w.ROOT / identifier
    w.project_limits.check_directory(directory, resolved)
    w.project_limits.preflight(identifier)
    old = read_json(directory / 'compose.runtime.json')
    records = w.container_records(identifier)
    w.require_stopped(records)
    rows = json.loads(w.capture(['docker', 'inspect', '--size', *[r['id'] for r in records]])) if records else []
    networks = network_names(config, identifier)
    entries = []
    image_overrides = read_json(directory / 'images.json') if (directory / 'images.json').exists() else {}
    for name, service in resolved['services'].items():
        current = sorted([row for row in rows if row['Config']['Labels'].get('com.docker.compose.service') == name],
                         key=lambda row: int(row['Config']['Labels'].get('com.docker.compose.container-number', '1')))
        before = old['services'].get(name) or {}
        if current and before.get('platform') != service.get('platform'):
            raise ValueError('Deploy platform changes as a new DCT instead of reusing its existing filesystem')
        reference = (image_overrides.get(name) or {}).get('reference') if isinstance(image_overrides.get(name), dict) else None
        if current and service['image'] not in (before.get('image'), reference):
            raise ValueError('Use the image update action before changing a deployed base image')
        count = service.get('deploy', {}).get('replicas', 1)
        if not current and not count:
            continue
        inspected = w.run(['docker', 'image', 'inspect', service['image']], check=False, capture_output=True)
        if inspected.returncode:
            if 'no such image' not in (inspected.stderr or '').lower():
                raise ValueError('Cannot inspect the deployment image')
            from .capacity import docker_space
            docker_space(256 * 1024 * 1024)
            w.run(['docker', 'pull', service['image']], timeout=86400)
            inspected = w.run(['docker', 'image', 'inspect', service['image']], capture_output=True)
        image = json.loads(inspected.stdout)[0]
        if (image.get('Descriptor') or {}).get('mediaType') in ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'):
            from .images import inspect
            reference = image['Id']
            platform = service.get('platform') or '/'.join(filter(None, (image['Os'], image['Architecture'], image.get('Variant'))))
            image = inspect(service['image'], platform)
            image['_create_image'] = reference
        base_ids = {image['Id'], image.get('_create_image', image['Id'])}
        if current and any((row['Config']['Labels'].get('com.docker.compose.image') or row['Image']) not in base_ids for row in current):
            raise ValueError('The image tag now points to another base image; use the image update action first')
        if service.get('container_name') and count > 1:
            raise ValueError('A fixed container name cannot be used with multiple replicas')
        for index in range(max(count, len(current))):
            row = current[index] if index < len(current) else None
            if row:
                owned(row, identifier, name)
            body = create_body(service, image, identifier, name, index + 1, networks, config) if index < count else None
            if row and body:
                # Config.User can carry an override absent from the image defaults.
                from .service_recreate import default_user
                body['User'] = default_user(row, before, config['services'][name], defaults_state(directory)) or '0'
                body['Labels']['com.docker.compose.image'] = image['Id']
                body['Labels']['betterdocker.preserved-bytes'] = row['Config']['Labels'].get('betterdocker.preserved-bytes', '0')
                anonymous = {m['Destination']: m['Name'] for m in row.get('Mounts', []) if m['Type'] == 'volume'}
                for mount in body['HostConfig'].get('Mounts') or []:
                    if mount['Type'] == 'volume' and not mount.get('Source') and mount['Target'] in anonymous:
                        mount['Source'] = anonymous[mount['Target']]
                targets = {mount['Target'] for mount in body['HostConfig'].get('Mounts') or []}
                for mount in row.get('Mounts', []):
                    if mount['Type'] == 'volume' and mount['Destination'] not in targets and mount['Destination'] in (body.get('Volumes') or {}):
                        body['HostConfig']['Mounts'].append({'Type': 'volume', 'Source': mount['Name'], 'Target': mount['Destination'], 'ReadOnly': not mount.get('RW', True)})
            entries.append({'source': row, 'body': body, 'platform': service.get('platform'),
                            'name': 'bd-' + identifier + '-' + name + '-' + str(index + 1)})
    selected = {name for name in resolved['services']}
    entries.extend({'source': row, 'body': None} for row in rows if row['Config']['Labels']['com.docker.compose.service'] not in selected)
    metadata = {key: value for key, value in info.items() if key not in ('deployment', 'digest', 'metrics')}
    metadata.update(expected=sum(s.get('deploy', {}).get('replicas', 1) for s in config['services'].values()), status='stopped')
    from .service_start import order
    priority = {name: index for index, name in enumerate(order(config['services']))}
    entries.sort(key=lambda entry: priority.get((entry.get('body') or {}).get('Labels', {}).get('com.docker.compose.service') or (entry.get('source') or {}).get('Config', {}).get('Labels', {}).get('com.docker.compose.service'), len(priority)))
    updates = {name: read_text(staging / name) for name in ('compose.source.yaml', '.env')}
    updates.update({'request.json': json.dumps(data), 'snapshot-restored.json': None, 'images.json': None, 'image-update.json': None})
    from .deployment import escape_literals
    execute(info, '*', entries, escape_literals(config), w.load_settings(directory), w.load_pending(directory), defaults_state(directory), updates, metadata,
            prepare=lambda journal, marker: resources(config, identifier, journal, marker))
