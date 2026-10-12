"""Replace stopped service instances while retaining their files and rollback originals."""
import copy
import json
import os
import re
import uuid
from urllib.parse import quote

from .rebind import preserved_body, network_endpoints, storage_bindings
import service_security

JOURNAL = 'settings-transaction.json'
FILES = ('compose.runtime.json', 'settings.json', 'pending.json', 'snapshot-restored.json', 'service-defaults.json')


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


def defaults_state(directory):
    path = directory / 'service-defaults.json'
    value = json.loads(path.read_text()) if path.exists() else {}
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
    if labels.get('betterdocker.id') != identifier or labels.get('com.docker.compose.service') != service:
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
        raise ValueError('Service has no instances to preserve')
    rows = json.loads(w.capture(['docker', 'inspect', *[r['id'] for r in records]]))
    original = json.loads((directory / 'compose.runtime.json').read_text())
    defaults_path = directory / 'service-defaults.json'
    defaults = defaults_state(directory)
    remember_defaults(rows, original['services'][service], defaults)
    token = uuid.uuid4().hex
    entries = []
    for index, row in enumerate(rows):
        owned(row, identifier, service)
        body = settings_body(row, original['services'][service], candidate['services'][service], defaults)
        storage_bindings(body['HostConfig'], candidate['services'][service], w.storage_config(identifier))
        if not w.trusted_management():
            w.validate_delegated_containers([row], identifier)
        entries.append({'source': row, 'body': body,
            'backup': 'bd-settings-' + identifier + '-' + token + '-' + str(index),
            'tag': 'betterdocker-settings/' + identifier + ':' + token + '-' + str(index)})
    journal = {'id': identifier, 'service': service, 'token': token, 'phase': 'preparing', 'entries': entries,
        'files': {name: (directory / name).read_text() if (directory / name).exists() else None for name in FILES}}
    durable_write(marker, journal)
    try:
        # Capture every replica before changing names or network membership.
        for entry in entries:
            entry['image'] = w.run(['docker', 'commit', entry['source']['Id'], entry['tag']],
                timeout=86400, capture_output=True).stdout.strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['image']):
                raise ValueError('Docker did not return a committed image ID')
            durable_write(marker, journal)
        for entry in entries:
            row = entry['source']
            w.docker_engine('POST', '/containers/' + row['Id'] + '/update', {'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0}})
            w.run(['docker', 'rename', row['Id'], entry['backup']], capture_output=True)
            for network, endpoint in row.get('NetworkSettings', {}).get('Networks', {}).items():
                if endpoint.get('EndpointID'):
                    w.docker_engine('POST', '/networks/' + quote(network, safe='') + '/disconnect', {'Container': row['Id'], 'Force': True})
            body = copy.deepcopy(entry['body'])
            body['Image'] = entry['image']
            body.setdefault('Labels', {})['betterdocker.settings-transaction'] = token
            body['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
            entry['created'] = w.docker_engine('POST', '/containers/create?name=' + quote(row['Name'].lstrip('/'), safe=''), body)['Id']
            durable_write(marker, journal)
        for entry in entries:
            w.docker_engine('POST', '/containers/' + entry['created'] + '/update', {'RestartPolicy': entry['body']['HostConfig']['RestartPolicy']})
        durable_write(directory / 'compose.runtime.json', candidate)
        durable_write(directory / 'settings.json', settings)
        durable_write(directory / 'pending.json', {name: changes for name, changes in pending.items() if changes})
        durable_write(defaults_path, defaults)
        restored = directory / 'snapshot-restored.json'
        if restored.exists():
            value = json.loads(restored.read_text())
            changed = {e['source']['Id']: e['created'] for e in entries}
            value['containers'] = [changed.get(cid, cid) for cid in value.get('containers', [])]
            durable_write(restored, value)
        journal['phase'] = 'committed'
        durable_write(marker, journal)
        finish(identifier, journal)
    except Exception:
        if journal['phase'] != 'committed':
            try:
                recover(identifier)
            except Exception as failure:
                raise ValueError('Service settings failed; recovery is incomplete. Retry the operation: ' + str(failure)) from None
        raise


def validate_journal(identifier, journal):
    if journal.get('id') != identifier or not re.fullmatch(r'[0-9a-f]{32}', journal.get('token', '')):
        raise ValueError('Invalid service settings recovery journal')
    if journal.get('phase') not in ('preparing', 'committed') or set(journal['files']) != set(FILES):
        raise ValueError('Invalid service settings recovery state')
    if not journal['entries'] or len({e['source']['Id'] for e in journal['entries']}) != len(journal['entries']):
        raise ValueError('Invalid service settings recovery instances')
    for saved in journal['files'].values():
        if saved is not None:
            json.loads(saved)
    for index, entry in enumerate(journal['entries']):
        if not re.fullmatch(r'[0-9a-f]{64}', entry['source']['Id']) or not re.fullmatch(r'/?[a-zA-Z0-9][a-zA-Z0-9_.-]*', entry['source']['Name']):
            raise ValueError('Invalid service settings recovery container')
        if entry.get('created') and not re.fullmatch(r'[0-9a-f]{64}', entry['created']):
            raise ValueError('Invalid service settings replacement ID')
        if entry.get('image') and not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['image']):
            raise ValueError('Invalid service settings image ID')
        owned(entry['source'], identifier, journal['service'])
        token = journal['token']
        if entry['backup'] != 'bd-settings-' + identifier + '-' + token + '-' + str(index) or entry['tag'] != 'betterdocker-settings/' + identifier + ':' + token + '-' + str(index):
            raise ValueError('Invalid service settings recovery resource')


def finish(identifier, journal):
    from . import projects as w
    validate_journal(identifier, journal)
    for entry in journal['entries']:
        replacement = inspect(entry['created'])
        if not replacement:
            raise ValueError('Committed replacement is missing; retain the original recovery container')
        owned(replacement, identifier, journal['service'])
        if (replacement['Config'].get('Labels') or {}).get('betterdocker.settings-transaction') != journal['token']:
            raise ValueError('Replacement ownership changed')
    for entry in journal['entries']:
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
    journal = json.loads(marker.read_text())
    validate_journal(identifier, journal)
    if journal['phase'] == 'committed':
        finish(identifier, journal)
        return True
    # Validate all originals before removing any replacement, including replicas.
    originals = []
    for entry in journal['entries']:
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
        name = entry['source']['Name'].lstrip('/')
        replacement = inspect(name)
        if replacement and replacement['Id'] != original['Id']:
            owned(replacement, identifier, journal['service'])
            if (replacement['Config'].get('Labels') or {}).get('betterdocker.settings-transaction') != journal['token']:
                raise ValueError('Service name is owned by another operation')
            stop_backup(replacement)
            w.run(['docker', 'rm', replacement['Id']], capture_output=True)
        stop_backup(original)
        if original['Name'].lstrip('/') != name:
            w.run(['docker', 'rename', original['Id'], name], capture_output=True)
        latest = inspect(original['Id'])
        connected = latest.get('NetworkSettings', {}).get('Networks', {})
        for network, endpoint in network_endpoints(entry['source']).items():
            if network not in connected:
                w.docker_engine('POST', '/networks/' + quote(network, safe='') + '/connect', {'Container': original['Id'], 'EndpointConfig': endpoint})
        w.docker_engine('POST', '/containers/' + original['Id'] + '/update', {'RestartPolicy': entry['source']['HostConfig']['RestartPolicy']})
    for name, value in journal['files'].items():
        path = directory / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            # Reuse atomic, private configuration writes, preserving JSON semantics.
            durable_write(path, json.loads(value))
    prune_images(identifier)
    marker.unlink()
    return True


def prune_images(identifier):
    """Retain images used by containers; discard unused tags owned by this guest."""
    from . import projects as w
    reference = 'betterdocker-settings/' + identifier
    result = w.run(['docker', 'image', 'ls', '--no-trunc', '--filter', 'reference=' + reference + ':*', '--format', '{{json .}}'], capture_output=True)
    images = [json.loads(line) for line in result.stdout.splitlines()]
    if not images:
        return
    containers = w.capture(['docker', 'ps', '-aq']).split()
    used = set(w.capture(['docker', 'inspect', '--format', '{{.Image}}', *containers]).split()) if containers else set()
    for image in images:
        if image['Repository'] == reference and re.fullmatch(r'[0-9a-f]{32}-[0-9]+', image['Tag']) and image['ID'] not in used:
            w.run(['docker', 'image', 'rm', '--force', reference + ':' + image['Tag']], capture_output=True)
