"""Service settings and pending changes."""
import ipaddress
import json
import os
import re
import runtime as node_runtime
import service_security


def load_settings(directory):
    path = directory / 'settings.json'
    return json.loads(path.read_text()) if path.exists() else {}


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - ({'cpus', 'memory', 'swap', 'cpuunits', 'pids', 'dns', 'dns_search', 'restart', 'disk'} | service_security.KEYS):
        raise ValueError('Invalid service settings')
    result = {}
    for key, maximum in [('cpus', 8192), ('memory', 1048576), ('swap', 1048576), ('cpuunits', 262144), ('pids', 4194304)]:
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not (-1 if key == 'swap' else 0) <= raw <= maximum or (key != 'cpus' and int(raw) != raw):
            raise ValueError(f'Invalid {key} limit')
        result[key] = raw
    if result['cpuunits'] and result['cpuunits'] < 2:
        raise ValueError('CPU units must be at least 2')
    if result['swap'] and not result['memory']:
        raise ValueError('Set a memory limit before setting swap')
    if 'disk' in value:
        disk = value['disk']
        if isinstance(disk, bool) or not isinstance(disk, int) or not 0 <= disk <= 1048576:
            raise ValueError('Invalid disk limit')
        result['disk'] = disk
    dns = value.get('dns', [])
    if not isinstance(dns, list) or len(dns) > 8:
        raise ValueError('Invalid DNS servers')
    result['dns'] = [str(ipaddress.ip_address(server)) for server in dns]
    search = value.get('dns_search', [])
    if not isinstance(search, list) or len(search) > 8 or any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,253}', v) for v in search):
        raise ValueError('Invalid DNS search domains')
    result['dns_search'] = search
    restart = value.get('restart', 'unless-stopped')
    if not isinstance(restart, str) or not re.fullmatch(r'(?:no|always|unless-stopped|on-failure(?::[1-9][0-9]*)?)', restart):
        raise ValueError('Invalid restart policy')
    result['restart'] = restart
    result.update(service_security.validate(value))
    return result


def apply_settings(config, settings):
    for name, values in settings.items():
        if name not in config['services']:
            continue
        service = config['services'][name]
        values = worker.validate_settings(values)
        for key in ('cpus', 'cpu_shares', 'cpu_period', 'cpu_quota', 'mem_limit', 'memswap_limit', 'pids_limit'):
            service.pop(key, None)
        resources = service.get('deploy', {}).get('resources', {})
        for group in ('limits', 'reservations'):
            for key in ('cpus', 'memory', 'pids'):
                resources.get(group, {}).pop(key, None)
            if group in resources and not resources[group]:
                resources.pop(group)
        if 'resources' in service.get('deploy', {}) and not resources:
            service['deploy'].pop('resources')
        if values['cpus']:
            service['cpus'] = values['cpus']
        if values['cpuunits']:
            service['cpu_shares'] = values['cpuunits']
        if values['memory']:
            service['mem_limit'] = int(values['memory']) * 1048576
            service['memswap_limit'] = -1 if values['swap'] == -1 else int(values['memory'] + values['swap']) * 1048576
            # A source reservation cannot exceed a new hard limit.
            service.pop('mem_reservation', None)
        if values['pids']:
            service['pids_limit'] = values['pids']
        if 'disk' in values:
            options = service.setdefault('storage_opt', {})
            options.pop('size', None)
            if values['disk']:
                options['size'] = str(values['disk']) + 'M'
            if not options:
                service.pop('storage_opt', None)
        service['dns'] = values['dns']
        service['dns_search'] = values['dns_search']
        service['restart'] = values['restart']
        service_security.apply(service, values)
    return config


def service_values(service):
    limits = service.get('deploy', {}).get('resources', {}).get('limits', {})
    memory = int(service.get('mem_limit') or limits.get('memory') or 0) // 1048576
    total = int(service.get('memswap_limit') or 0) // 1048576
    values = {'cpus': float(service.get('cpus') or limits.get('cpus') or 0), 'memory': memory,
              'swap': -1 if service.get('memswap_limit') == -1 else max(0, total - memory) if total else memory, 'cpuunits': service.get('cpu_shares', 0),
              'pids': max(0, int(service.get('pids_limit') or limits.get('pids') or 0)),
              'dns': service.get('dns', []), 'dns_search': service.get('dns_search', []),
              'restart': service.get('restart', 'no')}
    if service.get('storage_opt', {}).get('size'):
        values['disk'] = worker.disk_mib(service['storage_opt']['size'])
    values.update(service_security.values(service))
    return values


def load_pending(directory):
    path = directory / 'pending.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_pending(directory, pending):
    # Empty queues have the same digest as configurations created before this feature.
    pending = {name: changes for name, changes in pending.items() if changes}
    if pending:
        worker.write_json(directory / 'pending.json', pending)
    else:
        (directory / 'pending.json').unlink(missing_ok=True)


def pending_settings(identifier, node, service):
    worker.detail(identifier, node)
    directory = worker.ROOT / identifier
    config = json.loads((directory / 'compose.runtime.json').read_text())
    if service not in config['services']:
        raise ValueError('Unknown Compose service')
    values = worker.load_settings(directory).get(service, worker.service_values(config['services'][service]))
    values = dict(service_security.values(config['services'][service]), **values)
    values = dict(values, disk=values.get('disk', 0))
    pending = worker.load_pending(directory).get(service, {})
    return [dict(key=key, value=value, **({'pending': pending[key]} if key in pending else {}))
            for key, value in values.items()]


def revert_settings(identifier, node, service, keys, digest=None):
    with worker.locked(identifier):
        worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        directory = worker.ROOT / identifier
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if service not in config['services']:
            raise ValueError('Unknown Compose service')
        values = worker.load_settings(directory).get(service, worker.service_values(config['services'][service]))
        values = dict(service_security.values(config['services'][service]), **values)
        values = dict(values, disk=values.get('disk', 0))
        names = keys.split(',')
        worker.authorize_config_keys(names)
        if not names or any(key not in values for key in names):
            raise ValueError('Invalid setting to revert')
        pending = worker.load_pending(directory)
        changes = dict(pending.get(service, {}))
        for key in names:
            changes.pop(key, None)
        worker.validate_settings(dict(values, **changes))
        pending[service] = changes
        worker.save_pending(directory, pending)


def management(identifier, node, shared_stats=None):
    public = json.loads((worker.METADATA / (worker.project_id(identifier) + '.json')).read_text())
    if public.get('ha_recovery') and public['node'] == node:
        return dict(public, status='stopped', containers=[], services=[], stats=[], metrics={}, storage={}, operation=worker.operation_state(identifier))
    info = worker.detail(identifier, node)
    info['pending_network'] = worker.load_pending_network(worker.ROOT / identifier)
    snapshots = worker.snapshot_root(identifier)
    info['has_snapshots'] = snapshots.exists() and any(snapshots.iterdir())
    info['network_restored'] = (worker.ROOT / identifier / 'snapshot-restored.json').exists()
    directory = worker.ROOT / identifier
    config = json.loads((directory / 'compose.runtime.json').read_text())
    settings = worker.load_settings(directory)
    info['security'] = service_security.availability(node_runtime.load())
    from .images import state as image_state
    info['image_update'] = image_state(directory)
    image_path = directory / 'images.json'
    image_overrides = json.loads(image_path.read_text()) if image_path.exists() else {}
    info['services'] = []
    for name, service in config['services'].items():
        values = worker.service_values(service)
        info['services'].append({'name': name, 'image': service.get('image', ''),
                                 'image_reference': image_overrides.get(name, {}).get('reference', service.get('image', '')) if isinstance(image_overrides.get(name, {}), dict) else service.get('image', ''),
                                 'settings': dict(values, **settings.get(name, {})), 'replicas': service.get('deploy', {}).get('replicas', 1),
                                 'healthcheck': bool(service.get('healthcheck') and not service['healthcheck'].get('disable') and (service['healthcheck'].get('test') or ['CMD'])[0] != 'NONE'),
                                 'mounts': service.get('volumes', []), 'ports': service.get('ports', []),
                                 'overridden': name in settings, 'pending': worker.load_pending(directory).get(name, {})})
    if info.get('template'):
        info['containers'], info['stats'] = [], []
        info['metrics'] = worker.metric_snapshot([], [], info['services'])
        info['metrics'].update(worker.swap_usage([], info['services']))
        info['storage'] = worker.project_storage_status(identifier)
        info['operation'] = worker.operation_state(identifier)
        return info
    info['containers'] = worker.container_records(identifier)
    active = [item['id'] for item in info['containers'] if item['state'] == 'running']
    info['stats'] = []
    if shared_stats is not None:
        names = {item['name'] for item in info['containers']}
        info['stats'] = [row for row in shared_stats if row['Name'] in names]
    elif active:
        info['stats'] = [json.loads(line) for line in worker.capture(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *active]).splitlines()]
    info['metrics'] = worker.metric_snapshot(info['containers'], info['stats'], info['services'])
    info['metrics'].update(worker.swap_usage(info['containers'], info['services']))
    info['metrics'].update(worker.disk_usage(identifier, info['containers']))
    info['storage'] = worker.project_storage_status(identifier)
    info['operation'] = worker.operation_state(identifier)
    return info


def require_stopped(containers):
    if any(c['state'] not in ('created', 'exited', 'dead', 'stopped') for c in containers):
        raise ValueError('Stop the affected service or DCT before changing these settings')


def live_settings_arguments(before, after):
    """Only Docker's mutable settings qualify; everything else needs a stop."""
    flags = {'cpus': '--cpus', 'cpu_shares': '--cpu-shares',
             'mem_limit': '--memory', 'memswap_limit': '--memory-swap',
             'pids_limit': '--pids-limit', 'restart': '--restart'}
    old, new = dict(before), dict(after)
    arguments = []
    for key, flag in flags.items():
        a, b = old.pop(key, None), new.pop(key, None)
        if a == b:
            continue
        # Zero CPU, memory and shares are ignored by Docker update. Never silently recreate.
        if key in ('cpus', 'mem_limit') and not a:
            raise ValueError('Stop the affected service before adding this limit to an unlimited container')
        if key in ('cpus', 'mem_limit', 'cpu_shares') and not b:
            raise ValueError('Stop the affected service before removing this limit')
        value = b if b is not None else -1 if key in ('memswap_limit', 'pids_limit') else 'no' if key == 'restart' else 0
        arguments.extend([flag, str(value)])
    for key in ('dns', 'dns_search'):
        if not old.get(key): old.pop(key, None)
        if not new.get(key): new.pop(key, None)
    if old != new:
        raise ValueError('Stop the affected service before changing DNS, disk or Compose settings')
    return arguments


def update_live_containers(containers, arguments):
    if not arguments:
        return
    snapshots = json.loads(worker.capture(['docker', 'inspect', *[c['id'] for c in containers]]))
    saved = {c['Id']: c['HostConfig'] for c in snapshots}
    for flag, key in (('--cpus', 'NanoCpus'), ('--memory', 'Memory')):
        if flag in arguments[::2] and any(not h.get(key) for h in saved.values()):
            raise ValueError('Stop the affected service before adding this limit to an unlimited container')
    touched = []
    try:
        for container in containers:
            identifier = container['id']
            touched.append(identifier)
            worker.run(['docker', 'update', *arguments, identifier])
    except Exception as error:
        failures = []
        keys = {'--cpus': ('NanoCpus', 0), '--cpu-shares': ('CpuShares', 1024),
                '--memory': ('Memory', 0), '--memory-swap': ('MemorySwap', -1),
                '--pids-limit': ('PidsLimit', -1)}
        for identifier in touched:
            rollback = []
            for flag in arguments[::2]:
                if flag == '--restart':
                    policy = saved[identifier]['RestartPolicy']
                    value = policy['Name'] or 'no'
                    if value == 'on-failure' and policy.get('MaximumRetryCount'):
                        value += ':' + str(policy['MaximumRetryCount'])
                else:
                    key, default = keys[flag]
                    value = saved[identifier].get(key) or default
                    if flag == '--memory-swap' and not saved[identifier].get(key):
                        value = 2 * saved[identifier].get('Memory', 0) or -1
                    if flag == '--cpus': value /= 1000000000
                rollback.extend([flag, str(value)])
            try:
                worker.run(['docker', 'update', *rollback, identifier])
            except Exception as failure:
                failures.append(str(failure))
        if failures:
            raise ValueError('Resource update failed; restoring container settings also failed: ' + '; '.join(failures)) from error
        raise


def authorize_config_keys(keys):
    allowed = os.environ.get('BETTERDOCKER_CONFIG_KEYS', '*')
    if allowed != '*' and set(keys) - set(allowed.split(',')):
        raise ValueError('Permission denied: configuration field requires additional privileges')


def service_settings(identifier, node, service, values, digest=None):
    worker.validate_settings(values)
    if values.get('disk') and not worker.rootfs_quota_support()['supported']:
        raise ValueError('Container disk limit requires overlay2 on XFS with project quotas; volumes use a separate data disk')
    with worker.locked(identifier):
        worker.detail(identifier, node)
        worker.assert_configuration_digest(identifier, node, digest)
        rows = worker.pending_settings(identifier, node, service)
        effective = {row['key']: row.get('pending', row['value']) for row in rows}
        proposed = worker.validate_settings(dict(effective, **values))
        candidate = json.loads((worker.ROOT / identifier / 'compose.runtime.json').read_text())
        worker.apply_settings(candidate, {service: proposed})
        worker.project_limits.check_directory(worker.ROOT / identifier, candidate)
        # Include every queued service change when reserving the project budget.
        worker.apply_settings(candidate, worker.load_pending(worker.ROOT / identifier))
        worker.apply_settings(candidate, {service: proposed})
        worker.project_limits.check_directory(worker.ROOT / identifier, candidate)
        node_runtime.check_resources(candidate)
        if not worker.trusted_management():
            worker.validate_delegated_config(candidate, identifier)
        if 'disk' not in values and not proposed.get('disk'):
            proposed.pop('disk', None)
        worker.authorize_config_keys(key for key in proposed if proposed[key] != effective.get(key))
        worker._service_settings(identifier, node, service, proposed)


def _service_settings(identifier, node, service, values):
    """Caller holds the worker lock. Only active settings enter the runtime file."""
    directory = worker.ROOT / identifier
    runtime = directory / 'compose.runtime.json'
    original = runtime.read_text()
    config = json.loads(original)
    if service not in config['services']:
        raise ValueError('Unknown Compose service')
    candidate = json.loads(original)
    worker.apply_settings(candidate, {service: values})
    worker.project_limits.check_directory(directory, candidate)
    settings = worker.load_settings(directory)
    current = dict(worker.service_values(config['services'][service]), **settings.get(service, {}))
    pending = worker.load_pending(directory)
    containers = [c for c in worker.container_records(identifier) if c['service'] == service]
    active = any(c['state'] not in ('created', 'exited', 'dead', 'stopped') for c in containers)
    changed = {key: value for key, value in values.items() if current.get(key, 0 if key == 'disk' else None) != value}
    if 'disk' in changed:
        worker.check_protection(worker.detail(identifier, node), "can't update DCT container disk")
    if not changed:
        pending.pop(service, None)
        worker.save_pending(directory, pending)
        return
    if active:
        # Persist requested values first, as LXC does before trying hotplug.
        pending[service] = changed
        worker.save_pending(directory, pending)
        immediate = dict(current)
        waiting = dict(changed)
        before = json.loads(original)['services'][service]
        for group in (('memory', 'swap'), ('cpus',), ('cpuunits',), ('pids',), ('restart',), ('dns', 'dns_search'), ('disk',)):
            selected = {key: values[key] for key in group if key in changed}
            if not selected or any(c['state'] != 'running' for c in containers):
                continue
            candidate = dict(immediate, **selected)
            proposed = json.loads(original)
            worker.apply_settings(proposed, {service: candidate})
            try:
                worker.live_settings_arguments(before, proposed['services'][service])
            except ValueError:
                continue
            immediate = candidate
            for key in selected:
                waiting.pop(key, None)
        if immediate != current:
            worker.apply_settings(config, {service: immediate})
            arguments = worker.live_settings_arguments(before, config['services'][service])
            # Docker errors leave the requested changes pending and current state intact.
            worker.update_live_containers(containers, arguments)
            worker.write_json(runtime, config)
            settings[service] = immediate
            worker.write_json(directory / 'settings.json', settings)
        pending[service] = waiting
        worker.save_pending(directory, pending)
    else:
        # Save the queue before recreation, so a failed Start can be retried or reverted.
        pending[service] = changed
        worker.save_pending(directory, pending)
        worker.apply_settings(config, {service: values})
        worker.write_json(runtime, config)
        try:
            worker.run(worker.compose_base(directory, identifier) + ['up', '--no-start', '--no-deps', service], timeout=1800)
        except Exception:
            runtime.write_text(original)
            raise
        settings[service] = values
        worker.write_json(directory / 'settings.json', settings)
        pending.pop(service, None)
        worker.save_pending(directory, pending)
        marker = directory / 'snapshot-restored.json'
        if marker.exists():
            restored = json.loads(marker.read_text())
            restored['containers'] = [row['id'] for row in worker.container_records(identifier)]
            worker.write_json(marker, restored)
    worker.refresh(node)


def apply_pending_settings(identifier, node, service=None):
    directory = worker.ROOT / identifier
    for name, changes in list(worker.load_pending(directory).items()):
        if service is not None and name != service:
            continue
        worker.require_stopped([c for c in worker.container_records(identifier) if c['service'] == name])
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if name not in config['services']:
            raise ValueError('Unknown Compose service in pending settings')
        current = dict(worker.service_values(config['services'][name]), **worker.load_settings(directory).get(name, {}))
        worker._service_settings(identifier, node, name, worker.validate_settings(dict(current, **changes)))


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
