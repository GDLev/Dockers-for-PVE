"""Replace stopped service instances while retaining their files and rollback originals."""
from .secure_io import read_json, read_text
import copy
import json
import os
import re
import uuid
from urllib.parse import quote

from .rebind import preserved_body, network_endpoints, storage_bindings
import service_security

JOURNAL = 'settings-transaction.json'
FILES = ('compose.runtime.json', 'settings.json', 'pending.json', 'snapshot-restored.json', 'service-defaults.json', 'compose.source.yaml', '.env', 'request.json', 'images.json', 'image-update.json', 'storage.json', 'disk-transaction.json', 'security-policy.json')


from .operation_capacity import bounded


def durable_write(path, value):
    from . import projects as w
    w.write_json(path, value)
    with path.open('rb') as handle:
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_text(path, value):
    from .secure_io import atomic_text
    atomic_text(path, value)


def defaults_state(directory):
    path = directory / 'service-defaults.json'
    value = read_json(path) if path.exists() else {}
    if not isinstance(value, dict) or any(not re.fullmatch(r'sha256:[0-9a-f]{64}', key) or not isinstance(user, str) for key, user in value.items()):
        raise ValueError('Invalid original image user defaults')
    return value


def inspect(name):
    from . import projects as w
    result = w.run(['docker', 'inspect', name], check=False, capture_output=True)
    if result.returncode:
        # Do not mistake an unavailable daemon for a deleted container.
        if not re.search(r'no such (?:object|container)\b', result.stderr or '', re.IGNORECASE):
            raise ValueError('Cannot inspect service recovery container: ' + name)
        return None
    return json.loads(result.stdout)[0]


def owned(row, identifier, service):
    labels = row['Config'].get('Labels') or {}
    if labels.get('betterdocker.id') != identifier or not labels.get('com.docker.compose.service') or (service != '*' and labels.get('com.docker.compose.service') != service):
        raise ValueError('Service recovery container ownership changed')


def stop_backup(row):
    from . import projects as w
    if row['State']['Status'] not in ('created', 'exited', 'dead'):
        w.run(['docker', 'stop', '--time', '0', row['Id']], timeout=1800, capture_output=True)


def restart_policy(service):
    name, _, count = service.get('restart', 'no').partition(':')
    return {'Name': name, 'MaximumRetryCount': int(count) if count else 0}


def image_user(row, before):
    """Resolve the original user even when Docker exposes a multi-platform index."""
    from . import projects as w
    base = (row['Config'].get('Labels') or {}).get('com.docker.compose.image', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', base):
        raise ValueError('Original image identity is unavailable; specify an explicit container user')
    result = w.run(['docker', 'image', 'inspect', base], check=False, capture_output=True)
    if result.returncode == 0:
        return json.loads(result.stdout)[0]['Config'].get('User', '')
    # With containerd, a platform manifest may only be inspectable through its
    # index reference. Verify its identity before trusting a mutable image tag.
    reference = before.get('image')
    if reference:
        result = w.run(['docker', 'image', 'inspect', reference], check=False, capture_output=True)
        if result.returncode == 0:
            image = json.loads(result.stdout)[0]
            if (image.get('Descriptor') or {}).get('mediaType', '') in (
                    'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'):
                platform = before.get('platform') or '/'.join(filter(None, (image['Os'], image['Architecture'], image.get('Variant'))))
                result = w.run(['docker', 'image', 'inspect', '--platform', platform, reference], check=False, capture_output=True)
                image = json.loads(result.stdout)[0] if result.returncode == 0 else {}
            if image.get('Id') == base:
                return image['Config'].get('User', '')
    raise ValueError('Original image user is unavailable; restore that image or specify an explicit user')


def default_user(row, before, after, defaults):
    from . import projects as w
    old_user, new_user = before.get('user', ''), after.get('user', '')
    if new_user:
        return new_user
    if not old_user:
        return row['Config'].get('User', '')
    base = (row['Config'].get('Labels') or {}).get('com.docker.compose.image', '')
    if base in defaults:
        return defaults[base]
    defaults[base] = image_user(row, before)
    return defaults[base]


def settings_body(row, before, service, defaults):
    """Apply the canonical service settings to the preserved instance configuration."""
    body = preserved_body(row, row['Image'])
    # Docker treats an empty User as "inherit from this image". A committed
    # image may still carry the previous override, so select root explicitly.
    body['User'] = default_user(row, before, service, defaults) or '0'
    host = body['HostConfig']
    for key, source in (('Memory', 'mem_limit'), ('MemorySwap', 'memswap_limit'),
                        ('MemoryReservation', 'mem_reservation'), ('CpuShares', 'cpu_shares'),
                        ('CpuPeriod', 'cpu_period'), ('CpuQuota', 'cpu_quota'), ('PidsLimit', 'pids_limit')):
        host[key] = int(service.get(source) or 0)
    # Compose also accepts limits under deploy.resources.
    resources = service.get('deploy', {}).get('resources', {})
    limits, reservations = resources.get('limits', {}), resources.get('reservations', {})
    host['Memory'] = int(service.get('mem_limit') or limits.get('memory') or 0)
    host['MemoryReservation'] = int(service.get('mem_reservation') or reservations.get('memory') or 0)
    host['PidsLimit'] = int(service.get('pids_limit') or limits.get('pids') or 0)
    host['NanoCpus'] = int(round(float(service.get('cpus') or limits.get('cpus') or 0) * 1000000000))
    host['Dns'], host['DnsSearch'] = service.get('dns', []), service.get('dns_search', [])
    host['RestartPolicy'] = restart_policy(service)
    host['StorageOpt'] = service.get('storage_opt') or {}
    host['ReadonlyRootfs'] = bool(service.get('read_only', False))
    host['CapDrop'] = service.get('cap_drop') or []
    host['CapAdd'] = service.get('cap_add') or []
    opts = [o for o in host.get('SecurityOpt') or [] if not o.startswith(('no-new-privileges', 'apparmor'))]
    security = service_security.values(service)
    if security['no_new_privileges']:
        opts.append('no-new-privileges')
    if security['apparmor']:
        opts.append('apparmor=' + security['apparmor'])
    host['SecurityOpt'] = opts
    return body


def remember_defaults(rows, before, defaults):
    """Keep image defaults portable after an explicit user overrides Config.User."""
    from . import projects as w
    for row in rows:
        base = (row['Config'].get('Labels') or {}).get('com.docker.compose.image', '')
        if base in defaults or not re.fullmatch(r'sha256:[0-9a-f]{64}', base):
            continue
        if not before.get('user'):
            defaults[base] = row['Config'].get('User', '')
        else:
            try:
                defaults[base] = image_user(row, before)
            except ValueError:
                # Existing explicit users can remain in place without the base
                # image. Clearing one must resolve its original default first.
                pass


def replace(info, service, candidate, settings, pending):
    from . import projects as w
    identifier = info['id']
    directory = w.ROOT / identifier
    marker = directory / JOURNAL
    if marker.exists():
        raise ValueError('Recover the previous service settings operation first')
    records = [r for r in w.container_records(identifier) if r['service'] == service]
    w.require_stopped(records)
    if not records:
        execute(info, service, [], candidate, settings, pending, defaults_state(directory))
        return
    rows = json.loads(w.capture(['docker', 'inspect', '--size', *[r['id'] for r in records]]))
    original = read_json(directory / 'compose.runtime.json')
    defaults_path = directory / 'service-defaults.json'
    defaults = defaults_state(directory)
    remember_defaults(rows, original['services'][service], defaults)
    entries = []
    for index, row in enumerate(rows):
        owned(row, identifier, service)
        body = settings_body(row, original['services'][service], candidate['services'][service], defaults)
        storage_bindings(body['HostConfig'], candidate['services'][service], w.storage_config(identifier))
        if not w.trusted_management():
            w.validate_delegated_containers([row], identifier)
        entries.append({'source': row, 'body': body})
    execute(info, service, entries, candidate, settings, pending, defaults)


def replace_bindings(info, candidate=None, storage=None, updates=None):
    """Replace all stopped bindings as one recoverable container transaction."""
    from . import projects as w
    directory = w.ROOT / info['id']
    candidate = candidate if candidate is not None else read_json(directory / 'compose.runtime.json')
    storage = storage if storage is not None else w.storage_config(info['id'])
    records = w.container_records(info['id'])
    w.require_stopped(records)
    rows = json.loads(w.capture(['docker', 'inspect', '--size', *[r['id'] for r in records]])) if records else []
    entries = []
    from .endpoints import port_bindings
    for row in rows:
        owned(row, info['id'], '*')
        service = row['Config']['Labels']['com.docker.compose.service']
        body = preserved_body(row, row['Image'])
        body['HostConfig']['StorageOpt'] = copy.deepcopy(candidate['services'][service].get('storage_opt') or {})
        body['HostConfig']['PortBindings'] = port_bindings(candidate['services'][service].get('ports', [])) if not w.node_runtime.load().rootless else {}
        storage_bindings(body['HostConfig'], candidate['services'][service], storage)
        entries.append({'source': row, 'body': body})
    if entries:
        execute(info, '*', entries, candidate, w.load_settings(directory), w.load_pending(directory), defaults_state(directory), updates=updates)


@bounded
def execute(info, service, entries, candidate, settings, pending, defaults, updates=None, metadata=None, prepare=None):
    from . import projects as w
    identifier = info['id']
    directory = w.ROOT / identifier
    marker = directory / JOURNAL
    defaults_path = directory / 'service-defaults.json'
    if marker.exists():
        raise ValueError('Recover the previous container transaction first')
    from . import hardening
    policy = hardening.mode(directory)
    if updates and hardening.FILE in updates:
        change = json.loads(updates[hardening.FILE])
        if not isinstance(change, dict) or set(change) != {'mode'} or change['mode'] not in hardening.MODES:
            raise ValueError('Invalid transaction security policy')
        policy = change['mode']
    hardening.apply(candidate, w.node_runtime.load(), policy)
    for entry in entries:
        if entry.get('body'):
            hardening.host(entry['body'], w.node_runtime.load(), policy)
            from .retention import container_logs
            container_logs(entry['body'])
    w.project_limits.preflight(identifier, candidate, entries=entries)
    token = uuid.uuid4().hex
    from .space import replacement
    replacement(identifier, entries, candidate)
    from .capacity import docker_space, preserve_quota
    for entry in entries:
        if entry.get('source') and entry.get('body') and entry.get('preserve', True):
            preserve_quota(entry['source'], entry['body'])
    required = sum(max(0, (entry.get('source') or {}).get('SizeRw', 0)) for entry in entries)
    from .retention import preservation
    preservation(identifier, [entry['source'] for entry in entries if entry.get('source') and entry.get('body') and entry.get('preserve', True)])
    docker_space(required)
    for index, entry in enumerate(entries):
        entry['backup'] = 'bd-settings-' + identifier + '-' + token + '-' + str(index)
        entry['tag'] = 'betterdocker-settings/' + identifier + ':' + token + '-' + str(index)
    journal = {'id': identifier, 'service': service, 'token': token, 'phase': 'preparing', 'entries': entries,
        'files': {name: read_text(directory / name) if (directory / name).exists() else None for name in FILES}}
    if metadata is not None:
        journal['metadata'] = read_json(w.METADATA / (identifier + '.json'))
    from .data import remember_volumes
    remember_volumes(identifier, [entry['source'] for entry in entries if entry.get('source')])
    durable_write(marker, journal)
    try:
        if prepare:
            prepare(journal, marker)
        # Capture every replica before changing names or network membership.
        for entry in entries:
            if not entry.get('source') or not entry.get('body') or not entry.get('preserve', True):
                entry['image'] = entry['body']['Image'] if entry.get('body') else None
                continue
            entry['image'] = w.run(['docker', 'commit', entry['source']['Id'], entry['tag']],
                timeout=86400, capture_output=True).stdout.strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['image']):
                raise ValueError('Docker did not return a committed image ID')
            durable_write(marker, journal)
        from .retention import preservation
        if any(entry.get('source') and entry.get('body') and entry.get('preserve', True) for entry in entries):
            preservation(identifier)
        for entry in entries:
            row = entry.get('source')
            if row:
                backup_original(entry, row)
            if not entry.get('body'):
                continue
            body = copy.deepcopy(entry['body'])
            body['Image'] = entry['image']
            body.setdefault('Labels', {})['betterdocker.settings-transaction'] = token
            body['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
            name = row['Name'].lstrip('/') if row else entry['name']
            endpoint = '/containers/create?name=' + quote(name, safe='')
            if entry.get('platform'):
                endpoint += '&platform=' + quote(entry['platform'], safe='')
            entry['created'] = w.docker_engine('POST', endpoint, body)['Id']
            durable_write(marker, journal)
        for entry in entries:
            if entry.get('created'):
                w.docker_engine('POST', '/containers/' + entry['created'] + '/update', {'RestartPolicy': entry['body']['HostConfig']['RestartPolicy']})
        for name, value in (updates or {}).items():
            if name not in FILES:
                raise ValueError('Invalid transaction control file')
            target = directory / name
            if value is None:
                target.unlink(missing_ok=True)
            else:
                durable_text(target, value)
        if metadata is not None:
            w.write_json(w.METADATA / (identifier + '.json'), metadata)
        durable_write(directory / 'compose.runtime.json', candidate)
        durable_write(directory / 'settings.json', settings)
        durable_write(directory / 'pending.json', {name: changes for name, changes in pending.items() if changes})
        durable_write(defaults_path, defaults)
        restored = directory / 'snapshot-restored.json'
        if restored.exists():
            value = read_json(restored)
            changed = {e['source']['Id']: e.get('created') for e in entries if e.get('source')}
            value['containers'] = [changed.get(cid, cid) for cid in value.get('containers', []) if changed.get(cid, cid)]
            durable_write(restored, value)
        journal['phase'] = 'committed'
        durable_write(marker, journal)
        finish(identifier, journal)
    except Exception:
        if journal['phase'] != 'committed':
            try:
                recover(identifier)
            except Exception as failure:
                raise ValueError('Container change failed; recovery is incomplete. Retry the operation: ' + str(failure)) from None
        raise


def backup_original(entry, row):
    from . import projects as w
    w.docker_engine('POST', '/containers/' + row['Id'] + '/update', {'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0}})
    w.run(['docker', 'rename', row['Id'], entry['backup']], capture_output=True)
    for network, endpoint in row.get('NetworkSettings', {}).get('Networks', {}).items():
        if endpoint.get('EndpointID'):
            w.docker_engine('POST', '/networks/' + quote(network, safe='') + '/disconnect', {'Container': row['Id'], 'Force': True})


def validate_journal(identifier, journal):
    if journal.get('id') != identifier or not re.fullmatch(r'[0-9a-f]{32}', journal.get('token', '')):
        raise ValueError('Invalid service settings recovery journal')
    if journal.get('phase') not in ('preparing', 'committed') or (set(journal['files']) - set(FILES) or not set(FILES[:5]).issubset(journal['files'])):
        raise ValueError('Invalid service settings recovery state')
    sources = [e['source'] for e in journal['entries'] if e.get('source')]
    if not isinstance(journal['entries'], list) or len({row['Id'] for row in sources}) != len(sources):
        raise ValueError('Invalid service settings recovery instances')
    for name, saved in journal['files'].items():
        if saved is not None and name.endswith('.json'):
            json.loads(saved)
    for index, entry in enumerate(journal['entries']):
        if entry.get('source') and (not re.fullmatch(r'[0-9a-f]{64}', entry['source']['Id']) or not re.fullmatch(r'/?[a-zA-Z0-9][a-zA-Z0-9_.-]*', entry['source']['Name'])):
            raise ValueError('Invalid service settings recovery container')
        if entry.get('created') and not re.fullmatch(r'[0-9a-f]{64}', entry['created']):
            raise ValueError('Invalid service settings replacement ID')
        if entry.get('image') and not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['image']):
            raise ValueError('Invalid service settings image ID')
        if entry.get('source'):
            owned(entry['source'], identifier, journal['service'])
        elif not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', entry.get('name', '')):
            raise ValueError('Invalid new container name')
        token = journal['token']
        if entry['backup'] != 'bd-settings-' + identifier + '-' + token + '-' + str(index) or entry['tag'] != 'betterdocker-settings/' + identifier + ':' + token + '-' + str(index):
            raise ValueError('Invalid service settings recovery resource')


def finish(identifier, journal):
    from . import projects as w
    validate_journal(identifier, journal)
    from .recovery import require_docker
    require_docker()
    for entry in journal['entries']:
        if not entry.get('body'):
            continue
        replacement = inspect(entry['created'])
        if not replacement:
            raise ValueError('Committed replacement is missing; retain the original recovery container')
        owned(replacement, identifier, journal['service'])
        if (replacement['Config'].get('Labels') or {}).get('betterdocker.settings-transaction') != journal['token']:
            raise ValueError('Replacement ownership changed')
    for entry in journal['entries']:
        if not entry.get('source'):
            continue
        row = inspect(entry['source']['Id'])
        if row:
            owned(row, identifier, journal['service'])
            stop_backup(row)
            w.run(['docker', 'rm', row['Id']], capture_output=True)
    prune_images(identifier)
    (w.ROOT / identifier / JOURNAL).unlink()


def recover(identifier):
    """Incomplete swaps restore originals; committed swaps only finish cleanup."""
    from . import projects as w
    directory = w.ROOT / identifier
    marker = directory / JOURNAL
    if not marker.exists():
        return False
    journal = read_json(marker)
    validate_journal(identifier, journal)
    if journal['phase'] == 'committed':
        finish(identifier, journal)
        return True
    from .recovery import require_docker
    require_docker()
    # Validate all originals before removing any replacement, including replicas.
    originals = []
    for entry in journal['entries']:
        if not entry.get('source'):
            replacement = inspect(entry['name'])
            if replacement:
                owned(replacement, identifier, journal['service'])
                if (replacement['Config'].get('Labels') or {}).get('betterdocker.settings-transaction') != journal['token']:
                    raise ValueError('New container name is owned by another operation')
            originals.append(None)
            continue
        row = inspect(entry['source']['Id'])
        if not row:
            raise ValueError('Original service recovery container is missing')
        owned(row, identifier, journal['service'])
        replacement = inspect(entry['source']['Name'].lstrip('/'))
        if replacement and replacement['Id'] != row['Id']:
            owned(replacement, identifier, journal['service'])
            if (replacement['Config'].get('Labels') or {}).get('betterdocker.settings-transaction') != journal['token']:
                raise ValueError('Service name is owned by another operation')
        originals.append(row)
    for entry, original in zip(journal['entries'], originals):
        name = entry['source']['Name'].lstrip('/') if entry.get('source') else entry['name']
        replacement = inspect(name)
        if replacement and (not original or replacement['Id'] != original['Id']):
            owned(replacement, identifier, journal['service'])
            if (replacement['Config'].get('Labels') or {}).get('betterdocker.settings-transaction') != journal['token']:
                raise ValueError('Service name is owned by another operation')
            stop_backup(replacement)
            w.run(['docker', 'rm', replacement['Id']], capture_output=True)
        if not original:
            continue
        stop_backup(original)
        if original['Name'].lstrip('/') != name:
            w.run(['docker', 'rename', original['Id'], name], capture_output=True)
        latest = inspect(original['Id'])
        connected = latest.get('NetworkSettings', {}).get('Networks', {})
        for network, endpoint in network_endpoints(entry['source']).items():
            if network not in connected:
                w.docker_engine('POST', '/networks/' + quote(network, safe='') + '/connect', {'Container': original['Id'], 'EndpointConfig': endpoint})
        w.docker_engine('POST', '/containers/' + original['Id'] + '/update', {'RestartPolicy': entry['source']['HostConfig']['RestartPolicy']})
    for resource in reversed(journal.get('resources', [])):
        kind, name = resource['kind'], resource['name']
        if kind not in ('volume', 'network') or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}', name):
            raise ValueError('Invalid deployment recovery resource')
        result = w.run(['docker', kind, 'inspect', name], check=False, capture_output=True)
        if result.returncode:
            if not re.search(r'no such ' + kind + r'\b', result.stderr or '', re.IGNORECASE):
                raise ValueError('Cannot inspect deployment recovery resource')
            continue
        labels = json.loads(result.stdout)[0].get('Labels') or {}
        if labels.get('betterdocker.transaction') != journal['token']:
            raise ValueError('Deployment recovery resource ownership changed')
        w.run(['docker', kind, 'rm', name], capture_output=True)
    for name, value in journal['files'].items():
        path = directory / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            # Reuse atomic, private configuration writes, preserving JSON semantics.
            durable_text(path, value)
    if 'metadata' in journal:
        w.write_json(w.METADATA / (identifier + '.json'), journal['metadata'])
    prune_images(identifier)
    marker.unlink()
    return True


def prune_images(identifier):
    """Retain images used by containers; discard unused tags owned by this guest."""
    from . import projects as w
    reference = 'betterdocker-settings/' + identifier
    result = w.run(['docker', 'image', 'ls', '--no-trunc', '--format', '{{json .}}'], capture_output=True)
    images = [json.loads(line) for line in result.stdout.splitlines()]
    if not images:
        return
    containers = w.capture(['docker', 'ps', '-aq']).split()
    used = set(w.capture(['docker', 'inspect', '--format', '{{.Image}}', *containers]).split()) if containers else set()
    for image in images:
        owned_tag = (image['Repository'] == reference and re.fullmatch(r'[0-9a-f]{32}-[0-9]+', image['Tag'])) or (
            image['Repository'] in ('betterdocker-snapshot/' + identifier, 'betterdocker-export/' + identifier)
            and re.fullmatch(r'[0-9a-f]{32}', image['Tag'])) or (
            image['Repository'] == 'betterdocker-rebind' and re.fullmatch(re.escape(identifier) + r'-[0-9a-f]{12}', image['Tag']))
        if owned_tag and image['ID'] not in used:
            w.run(['docker', 'image', 'rm', '--force', image['Repository'] + ':' + image['Tag']], capture_output=True)
