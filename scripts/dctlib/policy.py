"""Delegated deployment policy."""
import json
import os
import re
from pathlib import Path
import runtime as node_runtime
import service_security
from .paths import control_path

LOG_DEFAULTS = {'max-size': '10m', 'max-file': '3'}


def apply_log_defaults(config):
    """Bound local logs without changing explicitly selected drivers or limits."""
    for service in config.get('services', {}).values():
        logging = service.setdefault('logging', {})
        if logging.get('driver', 'json-file') in ('json-file', 'local'):
            logging.setdefault('driver', 'json-file')
            options = logging.setdefault('options', {})
            for key, value in LOG_DEFAULTS.items():
                options.setdefault(key, value)


def validate_log_limits(options):
    size = str(options.get('max-size', LOG_DEFAULTS['max-size']))
    match = re.fullmatch(r'([0-9]+)([kmg]?)', size, re.I)
    if not match or not 0 < int(match[1]) * 1024 ** ('', 'k', 'm', 'g').index(match[2].lower()) <= 100 * 1024 * 1024:
        raise ValueError('Delegated logs require max-size between 1 byte and 100 MiB')
    count = str(options.get('max-file', LOG_DEFAULTS['max-file']))
    if not re.fullmatch(r'[0-9]+', count) or not 1 <= int(count) <= 10:
        raise ValueError('Delegated logs require max-file between 1 and 10')


def validate_oom(disabled, score):
    if disabled or score < 0:
        raise ValueError('Changing OOM protection requires administrator access')


def trusted_management():
    # Only the authenticated API wrapper supplies this flag. Internal node helpers
    # have no RPC caller and remain trusted for boot and HA recovery.
    return os.environ.get('BETTERDOCKER_TRUSTED', '1') == '1'


def owned_local_volume(volume, name, identifier):
    labels = volume.get('Labels') or {}
    anonymous = (not labels or set(labels) == {'com.docker.volume.anonymous'}) and re.fullmatch(r'[0-9a-f]{64}', name)
    return volume.get('Driver') == 'local' and not volume.get('Options') and bool(anonymous or labels.get('com.docker.compose.project') == 'bd-' + identifier)


def safe_bind(source, identifier):
    base = (worker.ROOT / worker.project_id(identifier)).resolve()
    path = Path(source)
    if not path.is_absolute() or '..' in path.parts or not path.resolve().is_relative_to(base):
        raise ValueError('Delegated access requires project-owned bind mounts')
    relative = path.resolve().relative_to(base)
    # Deployment files contain credentials and must never be writable by a workload.
    if control_path(relative) or len(relative.parts) == 1 and (path.suffix or path.exists() and not path.is_dir()):
        raise ValueError('Delegated access cannot mount project control files')
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Delegated bind paths cannot cross symbolic links')


def owned_volume_reference(name, identifier, container):
    """Anonymous names are allowed only when present in this inspected instance."""
    if name.startswith('bd-' + identifier + '_'):
        return True
    return bool(re.fullmatch(r'[0-9a-f]{64}', name) and any(
        m.get('Type') == 'volume' and m.get('Name') == name for m in container.get('Mounts', [])))


def validate_bind_layout(paths, identifier):
    """A workload must not be able to rename another mount's source ancestors."""
    base = (worker.ROOT / identifier).resolve()
    selected = node_runtime.load()
    sources = {Path(path) for path in paths}
    for path in sources:
        worker.safe_bind(str(path), identifier)
        if any(path != other and path.is_relative_to(other) for other in sources):
            raise ValueError('Delegated bind mounts cannot overlap; use separate data directories')
        for parent in path.parents:
            if parent == base:
                break
            if parent.exists():
                info = parent.lstat()
                mapped = selected.rootless and (info.st_uid == selected.uid or selected.subuid <= info.st_uid < selected.subuid + selected.count)
                if (info.st_uid != 0 and not mapped) or info.st_mode & 0o022:
                    raise ValueError('Bind source ancestors must belong to the host or this runtime and be non-writable by others')


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
                  'blkio_config oom_score_adj oom_kill_disable storage_opt security_opt'.split())
    if not isinstance(config, dict) or set(config) - {'name', 'version', 'services', 'volumes', 'networks'}:
        raise ValueError('Delegated configuration contains unsupported resources')
    validate_bind_layout([mount['source'] for service in config.get('services', {}).values()
                          for mount in service.get('volumes') or [] if isinstance(mount, dict) and mount.get('type') == 'bind'], identifier)
    for service in config.get('services', {}).values():
        if not isinstance(service, dict) or set(service) - allowed:
            raise ValueError('Delegated configuration contains unsafe or unsupported service options')
        service_security.delegated(service_security.options(service))
        validate_oom(service.get('oom_kill_disable', False), service.get('oom_score_adj', 0))
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
        validate_log_limits(logging.get('options') or {})
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
    validate_bind_layout([mount['Source'] for container in containers for mount in container.get('Mounts') or []
                          if mount.get('Type') == 'bind'], identifier)
    for container in containers:
        host = container.get('HostConfig') or {}
        service_security.delegated(host.get('SecurityOpt') or [])
        validate_oom(host.get('OomKillDisable', False), host.get('OomScoreAdj', 0))
        for key in ('Privileged', 'CapAdd', 'Devices', 'DeviceRequests', 'DeviceCgroupRules',
                    'VolumesFrom', 'CgroupParent', 'Cgroup', 'Sysctls', 'ExtraHosts'):
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
            elif not owned_volume_reference(source, identifier, container):
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
                if mount.get('Source') and not owned_volume_reference(mount['Source'], identifier, container):
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
                    if not owned_local_volume(volume, mount['Name'], identifier):
                        raise ValueError('This deployment uses an external volume')
                    users = set(worker.capture(['docker', 'ps', '--no-trunc', '-aq', '--filter', 'volume=' + mount['Name']]).split())
                    if users - {record['id'] for record in records}:
                        raise ValueError('This deployment uses a shared volume')
                    node_runtime.check_volume_path(Path(mount['Source']), mount['Name'])
            for network in container.get('NetworkSettings', {}).get('Networks', {}):
                value = json.loads(worker.capture(['docker', 'network', 'inspect', network]))[0]
                worker.validate_delegated_networks([value])
                if value.get('Driver') != 'bridge' or (value.get('Labels') or {}).get('com.docker.compose.project') != 'bd-' + identifier:
                    raise ValueError('This deployment uses an external network')
    return {'safe': True}


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
