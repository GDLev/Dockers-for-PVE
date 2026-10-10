"""Delegated deployment policy."""
import json
import os
from pathlib import Path
import runtime as node_runtime


def trusted_management():
    # Only the authenticated API wrapper supplies this flag. Internal node helpers
    # have no RPC caller and remain trusted for boot and HA recovery.
    return os.environ.get('BETTERDOCKER_TRUSTED', '1') == '1'


def safe_bind(source, identifier):
    base = (worker.ROOT / worker.project_id(identifier)).resolve()
    path = Path(source)
    if not path.is_absolute() or '..' in path.parts or not path.resolve().is_relative_to(base):
        raise ValueError('Delegated access requires project-owned bind mounts')
    relative = path.resolve().relative_to(base)
    # Deployment files contain credentials and must never be writable by a workload.
    if not relative.parts or relative.parts[0] in {
        'request.json', 'compose.source.yaml', 'compose.runtime.json', '.env',
        'settings.json', 'pending.json', 'pending-network.json', 'storage.json', 'ha.json', 'snapshots',
        'export-transaction.json', 'notes.txt', 'allocated-volume.json', 'storage.ext4',
        'snapshot-restored.json', 'snapshot-tree.json', 'snapshot-transaction.json',
        'storage-transaction.json', 'network-transaction.json', 'network-resume.json',
        'network-reconcile.json', 'dhcp-address.json', 'dhclient.conf', 'dhclient.leases',
        'metrics.json', 'disk-usage.json', 'rootfs-quota.json', 'replication.json',
    } or relative.parts[0].startswith('.'):
        raise ValueError('Delegated access cannot mount project control files')


def validate_delegated_source(source):
    # Check file-loading directives before Compose can read anything as host root.
    import yaml
    try:
        value = yaml.safe_load(source)
    except (yaml.YAMLError, RecursionError):
        raise ValueError('Invalid Compose configuration') from None
    if not isinstance(value, dict) or set(value) - {'name', 'version', 'services', 'volumes', 'networks'} - {k for k in value if isinstance(k, str) and k.startswith('x-')}:
        raise ValueError('Delegated Compose cannot include external files, secrets or configs')
    services = value.get('services')
    if not isinstance(services, dict) or not services:
        raise ValueError('Compose must define services')
    for service in services.values():
        if not isinstance(service, dict):
            raise ValueError('Invalid Compose service')
        for key in ('extends', 'build', 'label_file', 'secrets', 'configs', 'credential_spec', 'develop', 'provider', 'use_api_socket'):
            if key in service:
                raise ValueError('Delegated Compose does not support ' + key)
        files = service.get('env_file', [])
        files = [files] if isinstance(files, str) else files
        if not isinstance(files, list) or any(item != '.env' for item in files):
            raise ValueError('Delegated Compose can only read the imported .env file')
    return value


def validate_delegated_config(config, identifier):
    allowed = set('image command entrypoint environment env_file ports expose volumes networks labels restart '
                  'depends_on healthcheck user working_dir hostname domainname stop_signal stop_grace_period '
                  'init tty stdin_open read_only tmpfs cap_drop cpus cpu_shares cpu_period cpu_quota '
                  'mem_limit mem_reservation memswap_limit mem_swappiness pids_limit ulimits dns dns_search '
                  'dns_opt shm_size deploy logging pull_policy platform attach container_name name '
                  'blkio_config oom_score_adj oom_kill_disable storage_opt'.split())
    if not isinstance(config, dict) or set(config) - {'name', 'version', 'services', 'volumes', 'networks'}:
        raise ValueError('Delegated configuration contains unsupported resources')
    for service in config.get('services', {}).values():
        if not isinstance(service, dict) or set(service) - allowed:
            raise ValueError('Delegated configuration contains unsafe or unsupported service options')
        if service.get('container_name') and not service['container_name'].startswith('bd-' + identifier + '-'):
            raise ValueError('Delegated container names must belong to the project')
        for mount in service.get('volumes', []):
            if not isinstance(mount, dict) or mount.get('type') not in ('bind', 'volume', 'tmpfs'):
                raise ValueError('Delegated configuration contains an unsupported mount')
            if mount['type'] == 'bind':
                worker.safe_bind(mount.get('source', ''), identifier)
                if mount.get('bind', {}).get('propagation') not in (None, 'rprivate', 'private'):
                    raise ValueError('Delegated bind propagation is not allowed')
            if mount['type'] == 'volume' and mount.get('volume', {}).get('subpath'):
                raise ValueError('Delegated volume subpaths are not supported')
        files = service.get('env_file') or []
        for item in files:
            path = item.get('path', '') if isinstance(item, dict) else item
            if path not in ('.env', str(worker.ROOT / identifier / '.env')):
                raise ValueError('Delegated configuration references an external environment file')
        deploy = service.get('deploy') or {}
        if set(deploy) - {'replicas', 'restart_policy', 'resources', 'mode'} or deploy.get('mode', 'replicated') != 'replicated':
            raise ValueError('Delegated deploy settings are unsupported')
        resources = deploy.get('resources') or {}
        if any(set(v) - {'cpus', 'memory', 'pids'} for v in resources.values()):
            raise ValueError('Delegated device reservations are not allowed')
        logging = service.get('logging') or {}
        if logging.get('driver', 'json-file') not in ('json-file', 'local') or set(logging.get('options') or {}) - {'max-size', 'max-file', 'compress', 'labels', 'env'}:
            raise ValueError('Delegated logging must use a local bounded driver')
    for kind in ('volumes', 'networks'):
        for key, value in config.get(kind, {}).items():
            value = value or {}
            allowed_driver = 'local' if kind == 'volumes' else 'bridge'
            if value.get('external') or value.get('driver', allowed_driver) != allowed_driver or value.get('driver_opts'):
                raise ValueError('Delegated resources must be local and project-owned')
            ipam = value.get('ipam') or {}
            if kind == 'networks' and (ipam.get('driver', 'default') != 'default' or ipam.get('options')):
                raise ValueError('Delegated networks must use default IPAM without custom options')
            if not value.get('name', 'bd-' + identifier + '_' + key).startswith('bd-' + identifier + '_'):
                raise ValueError('Delegated resources cannot use custom shared names')


def validate_delegated_containers(containers, identifier):
    for container in containers:
        host = container.get('HostConfig') or {}
        for key in ('Privileged', 'CapAdd', 'Devices', 'DeviceRequests', 'DeviceCgroupRules',
                    'VolumesFrom', 'SecurityOpt', 'CgroupParent', 'Cgroup', 'Sysctls', 'ExtraHosts'):
            if host.get(key):
                raise ValueError('This deployment requires node administrator access: ' + key)
        for key in ('PidMode', 'IpcMode', 'UTSMode', 'UsernsMode', 'CgroupnsMode'):
            if host.get(key) not in (None, '', 'private'):
                raise ValueError('This deployment shares host or container namespaces')
        if host.get('Runtime', 'runc') not in ('', 'runc'):
            raise ValueError('Custom runtimes require administrator access')
        network = host.get('NetworkMode', '')
        if network not in ('none', '') and not network.startswith('bd-' + identifier + '_'):
            raise ValueError('This deployment uses a shared network')
        if host.get('LogConfig', {}).get('Type', 'json-file') not in ('', 'json-file', 'local'):
            raise ValueError('This deployment uses external logging')
        for bind in host.get('Binds') or []:
            if any(option in bind.split(':')[-1].split(',') for option in ('shared', 'rshared', 'slave', 'rslave')):
                raise ValueError('Shared bind propagation requires administrator access')
            source = bind.split(':', 1)[0]
            if source.startswith('/'):
                worker.safe_bind(source, identifier)
            elif not source.startswith('bd-' + identifier + '_'):
                raise ValueError('This deployment uses an external volume')
        for mount in host.get('Mounts') or []:
            if mount.get('Type') == 'bind':
                worker.safe_bind(mount.get('Source', ''), identifier)
                if (mount.get('BindOptions') or {}).get('Propagation') not in (None, '', 'private', 'rprivate'):
                    raise ValueError('Shared bind propagation requires administrator access')
            elif mount.get('Type') == 'volume':
                options = mount.get('VolumeOptions') or {}
                if options.get('DriverConfig') or options.get('Subpath'):
                    raise ValueError('Custom volume options require administrator access')
                if mount.get('Source') and not mount['Source'].startswith('bd-' + identifier + '_'):
                    raise ValueError('This deployment uses an external volume')
            elif mount.get('Type') != 'tmpfs':
                raise ValueError('Unsupported deployment mount')
        for mount in container.get('Mounts', []):
            if mount.get('Type') == 'bind':
                worker.safe_bind(mount['Source'], identifier)
            elif mount.get('Type') not in ('volume', 'tmpfs'):
                raise ValueError('Unsupported deployment mount')


def validate_delegated_networks(networks):
    for network in networks:
        ipam = network.get('IPAM') or {}
        options = network.get('Options') or {}
        if node_runtime.load().rootless and options == {'com.docker.network.bridge.gateway_mode_ipv4': 'nat-unprotected'}:
            options = {}
        if network.get('Driver') != 'bridge' or options or ipam.get('Driver', 'default') != 'default' or ipam.get('Options'):
            raise ValueError('Custom saved networks require administrator access')


def delegated_access(identifier, node):
    worker.detail(identifier, node)
    directory = worker.ROOT / worker.project_id(identifier)
    worker.validate_delegated_config(json.loads((directory / 'compose.runtime.json').read_text()), identifier)
    records = worker.container_records(identifier)
    if records:
        containers = json.loads(worker.capture(['docker', 'inspect', *[c['id'] for c in records]]))
        worker.validate_delegated_containers(containers, identifier)
        for container in containers:
            for mount in container.get('Mounts', []):
                if mount.get('Type') == 'volume':
                    volume = json.loads(worker.capture(['docker', 'volume', 'inspect', mount['Name']]))[0]
                    if volume.get('Driver') != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                        raise ValueError('This deployment uses an external volume')
            for network in container.get('NetworkSettings', {}).get('Networks', {}):
                value = json.loads(worker.capture(['docker', 'network', 'inspect', network]))[0]
                worker.validate_delegated_networks([value])
                if value.get('Driver') != 'bridge' or (value.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                    raise ValueError('This deployment uses an external network')
    return {'safe': True}


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
