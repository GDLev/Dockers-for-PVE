"""Translate canonical Compose service configuration into an Engine create request."""
import copy
import shlex
import json
import re
import hashlib


def words(value):
    return shlex.split(value) if isinstance(value, str) else value


def create_body(service, image, identifier, name, number, networks, config):
    from . import projects as w
    defaults = copy.deepcopy(image.get('Config') or {})
    body = {key: defaults[key] for key in ('Env', 'Cmd', 'Entrypoint', 'WorkingDir', 'User',
            'ExposedPorts', 'Volumes', 'Healthcheck', 'StopSignal') if key in defaults}
    body['Image'] = image.get('_create_image', image['Id'])
    for source, target in (('command', 'Cmd'), ('entrypoint', 'Entrypoint')):
        if source in service and service[source] is not None:
            body[target] = words(service[source])
    for source, target in (('user', 'User'), ('working_dir', 'WorkingDir'), ('hostname', 'Hostname'),
            ('domainname', 'Domainname'), ('stop_signal', 'StopSignal'), ('tty', 'Tty'), ('stdin_open', 'OpenStdin')):
        if source in service:
            body[target] = service[source]
    if 'stop_grace_period' in service:
        body['StopTimeout'] = max(0, duration(service['stop_grace_period']) // 1000000000)
    environment = dict(value.split('=', 1) for value in body.get('Env') or [] if '=' in value)
    environment.update({key: str(value) for key, value in (service.get('environment') or {}).items() if value is not None})
    body['Env'] = [key + '=' + value for key, value in environment.items()]
    image_labels = {key: value for key, value in (defaults.get('Labels') or {}).items() if not key.startswith('betterdocker.')}
    body['Labels'] = image_labels | dict(service.get('labels') or {}) | {
        'betterdocker.id': identifier, 'com.docker.compose.project': 'bd-' + identifier,
        'betterdocker.preserved-bytes': '0',
        'com.docker.compose.service': name, 'com.docker.compose.container-number': str(number),
        'com.docker.compose.oneoff': 'False', 'com.docker.compose.image': image['Id'],
        'com.docker.compose.config-hash': hashlib.sha256(json.dumps(service, sort_keys=True).encode()).hexdigest()}
    if service.get('healthcheck'):
        health = service['healthcheck']
        body['Healthcheck'] = {'Test': ['NONE']} if health.get('disable') else {
            target: health[source] for source, target in (('test', 'Test'), ('interval', 'Interval'),
                ('timeout', 'Timeout'), ('start_period', 'StartPeriod'), ('start_interval', 'StartInterval'), ('retries', 'Retries')) if source in health}
        for field in ('Interval', 'Timeout', 'StartPeriod', 'StartInterval'):
            if field in body['Healthcheck']:
                body['Healthcheck'][field] = duration(body['Healthcheck'][field])
    host = body['HostConfig'] = {}
    for source, target in (('init', 'Init'), ('privileged', 'Privileged'), ('cap_add', 'CapAdd'),
            ('shm_size', 'ShmSize'), ('mem_swappiness', 'MemorySwappiness'), ('oom_score_adj', 'OomScoreAdj'),
            ('oom_kill_disable', 'OomKillDisable'), ('dns_opt', 'DnsOptions'), ('group_add', 'GroupAdd'),
            ('sysctls', 'Sysctls'), ('extra_hosts', 'ExtraHosts'), ('ipc', 'IpcMode'), ('pid', 'PidMode'),
            ('uts', 'UTSMode'), ('userns_mode', 'UsernsMode'), ('cgroup', 'CgroupnsMode'), ('runtime', 'Runtime')):
        if source in service:
            host[target] = service[source]
    logging = service.get('logging') or {}
    host['LogConfig'] = {'Type': logging.get('driver', 'json-file'), 'Config': logging.get('options') or {}}
    host['Mounts'] = mounts(service, identifier, config)
    host['Tmpfs'] = dict(item.split(':', 1) if ':' in item else (item, '') for item in service.get('tmpfs') or [])
    if service.get('ulimits'):
        host['Ulimits'] = [{'Name': key, 'Soft': value.get('soft'), 'Hard': value.get('hard')} if isinstance(value, dict)
                          else {'Name': key, 'Soft': value, 'Hard': value} for key, value in service['ulimits'].items()]
    if service.get('devices'):
        host['Devices'] = []
        for device in service['devices']:
            parts = device.split(':')
            host['Devices'].append({'PathOnHost': parts[0], 'PathInContainer': parts[1] if len(parts) > 1 else parts[0],
                                    'CgroupPermissions': parts[2] if len(parts) > 2 else 'rwm'})
    host['DeviceCgroupRules'] = service.get('device_cgroup_rules') or []
    devices = service.get('deploy', {}).get('resources', {}).get('reservations', {}).get('devices', [])
    if service.get('gpus'):
        devices = [*devices, *([{'count': 'all', 'capabilities': ['gpu']}] if service['gpus'] == 'all' else service['gpus'])]
    if devices:
        host['DeviceRequests'] = [{'Driver': device.get('driver', ''), 'Count': 0 if device.get('device_ids') else -1 if device.get('count', 'all') == 'all' else int(device['count']),
            'DeviceIDs': device.get('device_ids') or [], 'Capabilities': [device.get('capabilities') or ['gpu']], 'Options': device.get('options') or {}} for device in devices]
    block = service.get('blkio_config') or {}
    if block:
        host['BlkioWeight'] = block.get('weight', 0)
        for key, target in (('weight_device', 'BlkioWeightDevice'), ('device_read_bps', 'BlkioDeviceReadBps'),
                ('device_write_bps', 'BlkioDeviceWriteBps'), ('device_read_iops', 'BlkioDeviceReadIOps'), ('device_write_iops', 'BlkioDeviceWriteIOps')):
            if key in block:
                host[target] = [{'Path': value['path'], 'Weight' if key == 'weight_device' else 'Rate': value.get('weight', value.get('rate'))} for value in block[key]]
    if service.get('volumes_from'):
        host['VolumesFrom'] = []
        for value in service['volumes_from']:
            if value.startswith('container:'):
                host['VolumesFrom'].append(value[len('container:'):])
            else:
                source_name, _, mode = value.partition(':')
                host['VolumesFrom'].append('bd-' + identifier + '-' + source_name + '-1' + (':' + mode if mode else ''))
    for key in ('credential_spec', 'use_api_socket', 'post_start', 'pre_stop'):
        if service.get(key):
            raise ValueError('Deployment editing requires an explicit adapter for ' + key)
    body['NetworkingConfig'] = {'EndpointsConfig': {networks[key]: dict(value or {}) for key, value in
            (service.get('networks') or {'default': {}}).items()}}
    for endpoint in body['NetworkingConfig']['EndpointsConfig'].values():
        aliases = endpoint.pop('aliases', [])
        address = {target: endpoint.pop(source) for source, target in (('ipv4_address', 'IPv4Address'),
                   ('ipv6_address', 'IPv6Address')) if source in endpoint}
        driver_options = endpoint.get('driver_opts')
        mac = endpoint.get('mac_address') or service.get('mac_address')
        link_local = endpoint.get('link_local_ips')
        endpoint.clear()
        endpoint.update(Aliases=[name, *aliases])
        if driver_options:
            endpoint['DriverOpts'] = driver_options
        if mac:
            endpoint['MacAddress'] = mac
        if link_local:
            address['LinkLocalIPs'] = link_local
        if address:
            endpoint['IPAMConfig'] = address
    host['NetworkMode'] = next(iter(body['NetworkingConfig']['EndpointsConfig']), 'none')
    from .endpoints import port_bindings
    host['PortBindings'] = port_bindings(service.get('ports') or []) if not w.node_runtime.load().rootless else {}
    body['ExposedPorts'] = body.get('ExposedPorts') or {}
    body['ExposedPorts'].update({port: {} for port in port_bindings(service.get('ports') or [])})
    for value in service.get('expose') or []:
        port, separator, protocol = str(value).partition('/')
        body['ExposedPorts'][port + '/' + (protocol if separator else 'tcp')] = {}
    from .service_recreate import settings_body
    fake = {'Config': {'User': body.get('User', ''), 'Labels': body['Labels']}, 'Image': body['Image'],
            'HostConfig': host, 'Mounts': [], 'NetworkSettings': {}}
    resource = settings_body(fake, service, service, {})
    body['User'] = resource['User']
    body['HostConfig'].update(resource['HostConfig'])
    body['HostConfig']['SecurityOpt'] = service.get('security_opt') or body['HostConfig'].get('SecurityOpt') or []
    return body


def duration(value):
    if isinstance(value, int):
        return value
    tokens = re.findall(r'([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)', value)
    if not tokens or ''.join(number + unit for number, unit in tokens) != value:
        raise ValueError('Invalid Compose duration')
    scale = {'ns': 1, 'us': 1000, 'µs': 1000, 'ms': 1000000, 's': 1000000000, 'm': 60000000000, 'h': 3600000000000}
    return int(sum(float(number) * scale[unit] for number, unit in tokens))


def mounts(service, identifier, config):
    from . import projects as w
    result = []
    for mount in service.get('volumes') or []:
        item = {'Type': mount['type'], 'Target': mount['target'], 'ReadOnly': bool(mount.get('read_only'))}
        source = mount.get('source', '')
        if item['Type'] == 'volume' and source:
            definition = config.get('volumes', {}).get(source) or {}
            source = definition.get('name') or 'bd-' + identifier + '_' + source
        if source:
            item['Source'] = source
        if item['Type'] == 'bind':
            item['BindOptions'] = {'CreateMountpoint': mount.get('bind', {}).get('create_host_path', True)}
        for key, target in (('bind', 'BindOptions'), ('volume', 'VolumeOptions'), ('tmpfs', 'TmpfsOptions')):
            options = mount.get(key) or {}
            values = {'propagation': 'Propagation', 'create_host_path': 'CreateMountpoint', 'nocopy': 'NoCopy',
                      'subpath': 'Subpath', 'size': 'SizeBytes', 'mode': 'Mode'}
            if options:
                item.setdefault(target, {}).update({values[k]: v for k, v in options.items() if values.get(k)})
        result.append(item)
    for kind in ('secrets', 'configs'):
        for reference in service.get(kind) or []:
            reference = {'source': reference} if isinstance(reference, str) else reference
            definition = config.get(kind, {}).get(reference['source']) or {}
            if not definition.get('file') or definition.get('external'):
                raise ValueError('Deployment edits require local files for ' + kind)
            target = reference.get('target') or ('/run/secrets/' if kind == 'secrets' else '/') + reference['source']
            if not target.startswith('/'):
                target = '/run/secrets/' + target if kind == 'secrets' else '/' + target
            result.append({'Type': 'bind', 'Source': definition['file'], 'Target': target, 'ReadOnly': True})
    return result
