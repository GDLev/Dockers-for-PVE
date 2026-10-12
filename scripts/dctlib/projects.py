#!/usr/bin/env python3
"""Native host Docker projects. Public cluster metadata never contains deployment secrets."""
import argparse
import contextlib
from datetime import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import runtime as node_runtime
from . import limits as project_limits

HELPERS = Path(__file__).resolve().parent.parent
ROOT = Path('/var/lib/betterdocker/projects')
METADATA = Path('/etc/pve/betterdocker')
LOCK = Path('/run/lock/betterdocker-projects.lock')
PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup')
PVE_NODES = Path('/etc/pve/nodes')

from .policy import (
    apply_log_defaults,
    trusted_management,
    safe_bind,
    validate_delegated_source,
    validate_delegated_config,
    validate_delegated_containers,
    validate_delegated_networks,
    delegated_access,
)

from .metrics import (
    byte_size,
    metric_snapshot,
    swap_usage,
    disk_usage,
    record_metrics,
    rrddata,
)

from .storage import (
    rootfs_quota_support,
    disk_mib,
    storage_config,
    pve_storage,
    storage_device,
    storage_unit,
    storage_unmount,
    recover_storage_transaction,
    project_pve_storage,
    storage_mount,
    apply_storage,
    project_storage_status,
    purge_retained_storage,
    project_storage_limit,
)

from .data import owned_data_plan, remove_owned_data, delete_storage_volume, storage_volume_state

from .settings import (
    load_settings,
    validate_settings,
    apply_settings,
    service_values,
    load_pending,
    save_pending,
    pending_settings,
    revert_settings,
    management,
    require_stopped,
    live_settings_arguments,
    update_live_containers,
    authorize_config_keys,
    service_settings,
    _service_settings,
    apply_pending_settings,
)

from .network import (
    NETWORK_KEYS,
    bridge_addresses,
    check_address,
    check_sdn_bridge,
    network_preflight,
    ensure_link,
    add_address,
    dhcp_paths,
    lease,
    dhcp_command,
    start_dhcp,
    dhcp_client,
    ensure_address,
    dhcp_event,
    rebind_runtime,
    network_boot_id,
    save_network_resume,
    read_network_resume,
    active_network_instances,
    clear_network_resume,
    stop_network_instances,
    prepare_network_start,
    reconcile_network_runtime,
    sync_dhcp,
    firewall_check,
    prepare_firewall,
    finish_network_start,
    load_pending_network,
    save_pending_network,
    network_config,
    recover_network_transaction,
    network_candidate,
    apply_network,
    project_network,
    apply_pending_network,
    remove_network,
)

from .snapshots import (
    snapshot_name,
    snapshot_root,
    snapshot_tree_state,
    snapshot_tree_write,
    snapshot_list,
    snapshot_config,
    snapshot_edit,
    snapshot_containers,
    snapshot_quiesce,
    snapshot_mounts,
    snapshot_digest,
    snapshot_check,
    recover_snapshot_transaction,
    snapshot_restore_network,
    snapshot_capture,
    docker_engine,
    snapshot_apply,
    project_snapshot,
)

from .lifecycle import (
    parse_startup,
    access_state,
    lifecycle_config,
    restore_restart_policy,
    daemon_prepare,
    daemon_resume,
    daemon_unmanage,
    graceful_shutdown,
    pve_hook,
    hook_journal,
    sync_guest_runtime_state,
    guest_hook,
    recover_guest_hook,
    expect_guest_hook,
)

from .deployment import (
    validate,
    parse_ports,
    image_compose,
    bind_ports,
    compose_base,
    prepare,
    create,
    update,
    restore,
    preview,
    reset_settings,
)


def reserved_ids():
    registry = METADATA / 'ids'
    return {int(path.stem): json.loads(path.read_text()) for path in registry.glob('*.json')}


def reserve_id(identifier, node, preferred=None):
    """Called under the cluster datacenter.cfg lock by the API/installer."""
    project_id(identifier)
    registry = reserved_ids()
    for vmid, entry in registry.items():
        if entry['id'] == identifier:
            path = METADATA / (identifier + '.json')
            if path.exists():
                info = json.loads(path.read_text())
                info['vmid'] = vmid
                write_json(path, info)
            return vmid
    occupied = set(registry)
    occupied.update(int(path.stem) for kind in ('qemu-server', 'lxc')
                    for path in PVE_NODES.glob('*/' + kind + '/*.conf') if path.stem.isdigit())
    occupied.update(int(info['vmid']) for info in summaries() if info.get('vmid'))
    if preferred not in (None, ''):
        vmid = int(preferred)
        if not 100 <= vmid <= 999999999:
            raise ValueError('DCT ID must be between 100 and 999999999')
        if vmid in occupied:
            raise ValueError(f'Guest ID {vmid} is already in use')
    else:
        vmid = next(value for value in range(100, 1000000) if value not in occupied)
    write_json(METADATA / 'ids' / f'{vmid}.json', {'id': identifier, 'node': node})
    path = METADATA / (identifier + '.json')
    if path.exists():
        info = json.loads(path.read_text())
        info['vmid'] = vmid
        write_json(path, info)
    return vmid


def release_id(identifier):
    if storage_config(identifier).get('volid') or storage_config(identifier).get('unused'):
        return  # Retained PVE disks keep their numeric guest ID reserved.
    for vmid, entry in reserved_ids().items():
        if entry['id'] == identifier:
            (METADATA / 'ids' / f'{vmid}.json').unlink()


def run(args, **kwargs):
    inherited = set(kwargs.get('pass_fds', ())) | set(getattr(_operation_fds, 'values', {}).values())
    if inherited:
        kwargs['pass_fds'] = tuple(sorted(inherited))
    if args[:2] == ['docker', 'compose']:
        index = 2
        while index < len(args):
            word = args[index]
            if word in ('--project-name', '--project-directory', '--env-file', '-f', '--file'):
                index += 2
            elif word.startswith('-'):
                index += 1
            else:
                break
        command = args[index] if index < len(args) else ''
        if command in ('up', 'start', 'restart', 'run'):
            for index, word in enumerate(args[:-1]):
                if word in ('-f', '--file') and Path(args[index + 1]).name == 'compose.runtime.json':
                    project_limits.check_directory(Path(args[index + 1]).parent)
    kwargs.setdefault('timeout', 60)
    kwargs.setdefault('env', {'PATH': os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin'), 'HOME': '/root'})
    if args and args[0] == 'docker' and not trusted_management():
        client = ROOT.parent / 'delegated-client'
        client.mkdir(parents=True, exist_ok=True, mode=0o700)
        kwargs['env'] = dict(kwargs['env'], DOCKER_CONFIG=str(client))
    for key in ('BETTERDOCKER_TASK_USER', 'BETTERDOCKER_TRUSTED', 'BETTERDOCKER_REQUIRE_SAFE', 'BETTERDOCKER_CONFIG_KEYS', 'BETTERDOCKER_TASK_SYS_MODIFY'):
        if key in os.environ:
            kwargs['env'][key] = os.environ[key]
    kwargs.setdefault('check', True)
    kwargs.setdefault('text', True)
    return node_runtime.run(args, delegated=not trusted_management(), **kwargs)


def capture(args):
    return run(args, capture_output=True).stdout


def write_json(path, value, allow_migration_change=False):
    if path.parent == METADATA and isinstance(value, dict):
        value = {key: item for key, item in value.items() if key not in ('digest', 'deployment')}
    if path.parent == METADATA and METADATA == Path('/etc/pve/betterdocker') and isinstance(value, dict) and value.get('id') and value.get('vmid'):
        owner = reserved_ids().get(int(value['vmid']))
        if owner and owner.get('node') != value.get('node'):
            raise ValueError('DCT ownership moved to another node')
        if path.exists() and not allow_migration_change:
            existing = json.loads(path.read_text())
            if existing.get('migration'):
                value = dict(value, migration=existing['migration'])
    # A refresh that started before migration must not overwrite the assigned ID.
    if path.parent == METADATA and isinstance(value, dict) and value.get('id') and not value.get('vmid'):
        vmid = next((number for number, entry in reserved_ids().items() if entry['id'] == value['id']), None)
        if vmid:
            value = dict(value, vmid=vmid)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2) + '\n'
    if path.exists() and path.read_text() == content:
        return
    # pmxcfs does not implement chmod: only public summaries are stored there.
    fd, temporary = tempfile.mkstemp(prefix='.betterdocker-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(content)
            if not path.is_relative_to(METADATA):
                output.flush()
                os.fsync(output.fileno())
        os.replace(temporary, path)
        if not path.is_relative_to(METADATA):
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


_held_locks = threading.local()
_operation_fds = threading.local()


@contextlib.contextmanager
def locked(identifier=None):
    # Configuration locks are distinct from the non-reentrant operation lock.
    # Guest -> node is the only permitted nesting order.
    path = ROOT.parent / 'config-locks' / (project_id(identifier) + '.lock') if identifier else LOCK
    held = getattr(_held_locks, 'paths', set())
    if path in held:
        yield
        return
    if identifier and LOCK in held:
        raise ValueError('Cannot acquire a guest lock while holding the node lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        owner = os.fstat(handle.fileno())
        if owner.st_uid != os.geteuid() or not stat.S_ISREG(owner.st_mode):
            raise ValueError('Invalid lock file owner or type')
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        _held_locks.paths = held | {path}
        try:
            yield
        finally:
            _held_locks.paths = held


def node_locked(function):
    from functools import wraps
    @wraps(function)
    def call(*args, **kwargs):
        with locked():
            return function(*args, **kwargs)
    return call


def project_id(value):
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid Docker project ID')
    return value


def summaries():
    results = []
    if METADATA.exists():
        for path in METADATA.glob('*.json'):
            try:
                results.append(json.loads(path.read_text()))
            except (ValueError, OSError):
                continue
    return results


def health_state(container):
    test = (container['Config'].get('Healthcheck') or {}).get('Test') or []
    if not test or test[0] == 'NONE':
        return 'none'
    if container['State']['Status'] != 'running':
        return 'inactive'
    return (container['State'].get('Health') or {}).get('Status', 'starting')


def container_records(identifier):
    ids = capture(['docker', 'ps', '-aq', '--filter', 'label=betterdocker.id=' + identifier]).split()
    if not ids:
        return []
    inspected = json.loads(capture(['docker', 'inspect', *ids]))
    recovery = ROOT / identifier / 'settings-transaction.json'
    backups = {}
    if recovery.exists():
        journal = json.loads(recovery.read_text())
        from .service_recreate import validate_journal
        validate_journal(identifier, journal)
        backups = {entry['source']['Id']: entry['backup'] for entry in journal['entries'] if entry.get('source')}
    records = []
    for item in inspected:
        if backups.get(item['Id']) == item['Name'].lstrip('/'):
            continue
        labels = item['Config'].get('Labels') or {}
        if labels.get('betterdocker.id') != identifier:
            continue
        records.append({'id': item['Id'], 'name': item['Name'].lstrip('/'),
                        'service': labels.get('com.docker.compose.service', ''),
                        'number': labels.get('com.docker.compose.container-number', '1'),
                        'state': item['State']['Status'], 'started': item['State'].get('StartedAt', ''),
                        'image': item['Config']['Image'], 'pid': item['State'].get('Pid', 0),
                        'stop_signal': item['Config'].get('StopSignal') or 'SIGTERM',
                        'health': health_state(item),
                        'apparmor': item.get('AppArmorProfile', ''),
                        'restarts': item.get('RestartCount', 0), 'exitcode': item['State'].get('ExitCode', 0)})
    return records


def notes(identifier, node, description=None):
    detail(identifier, node)
    path = ROOT / identifier / 'notes.txt'
    if description is not None:
        if len(description) > 8192:
            raise ValueError('Notes cannot exceed 8192 characters')
        path.write_text(description)
        os.chmod(path, 0o600)
    return {'description': path.read_text() if path.exists() else ''}


def console_target(identifier, node, container, shell):
    detail(identifier, node)
    if shell not in ('/bin/sh', '/bin/bash') or not re.fullmatch(r'[0-9a-f]{64}', container):
        raise ValueError('Invalid console target')
    if not any(item['id'] == container and item['state'] == 'running' for item in container_records(identifier)):
        raise ValueError('Choose a running container belonging to this project')
    if not trusted_management():
        delegated_access(identifier, node)
    return ['docker', 'exec', '-it', container, shell]


def current_node():
    return os.uname().nodename.split('.')[0]


def configuration_digest(directory, deployment, protection=False, tags='', lifecycle=None):
    # Runtime files include assigned IPs and Docker state, not source configuration.
    state = {'deployment': deployment, 'settings': load_settings(directory),
             'storage': json.loads((directory / 'storage.json').read_text()) if (directory / 'storage.json').exists() else None}
    if (directory / 'images.json').exists():
        state['images'] = json.loads((directory / 'images.json').read_text())
    if (directory / 'limits.json').exists():
        state['project_limits'] = project_limits.load(directory)
    if lifecycle:
        state['lifecycle'] = lifecycle
    if protection:
        state['protection'] = True
    if tags:
        state['tags'] = tags
    notes_path = directory / 'notes.txt'
    if notes_path.exists() and notes_path.read_text():
        state['description'] = notes_path.read_text()
    pending = load_pending(directory)
    if pending:
        state['pending'] = pending
    network = load_pending_network(directory)
    if network:
        state['pending_network'] = network
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def assert_configuration_digest(identifier, node, expected):
    path = METADATA / (project_id(identifier) + '.json')
    info = json.loads(path.read_text()) if path.exists() else {}
    if info.get('migration'):
        raise ValueError('DCT migration is in progress; recover it before changing configuration')
    journal = ROOT / identifier / 'snapshot-transaction.json'
    if journal.exists() and json.loads(journal.read_text()).get('action') == 'restore':
        raise ValueError('Retry the interrupted snapshot rollback before changing configuration')
    if expected is None or expected == '':
        return  # Like PVE, older API clients may omit optimistic concurrency checks.
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid configuration digest')
    if detail(identifier, node)['digest'] != expected:
        raise ValueError('checksum mismatch (file change by other user?)')


def detail(identifier, node):
    identifier = project_id(identifier)
    info = json.loads((METADATA / (identifier + '.json')).read_text())
    if info['node'] != node or node != current_node():
        raise ValueError('Project belongs to another node')
    directory = ROOT / identifier
    deployment = json.loads((directory / 'request.json').read_text())
    return dict(info, deployment=deployment, digest=configuration_digest(directory, deployment, info.get('protection', False), info.get('tags', ''), {key: info[key] for key in ('onboot', 'startup', 'template', 'hookscript') if info.get(key)}))


def lifecycle_stop(identifier, node, timeout=180, force=1):
    with operation(identifier, 'shutdown'):
        try:
            action(identifier, node, 'shutdown', timeout)
        except ValueError as error:
            if not int(force) or not str(error).startswith('Shutdown timed out.'):
                raise
            action(identifier, node, 'force-stop')


def project_config(identifier, node, changes, digest=None):
    if not isinstance(changes, dict) or set(changes) - {'description', 'tags', 'protection', 'onboot', 'startup', 'hookscript'}:
        raise ValueError('Invalid project configuration')
    if 'description' in changes and (not isinstance(changes['description'], str) or len(changes['description']) > 8192):
        raise ValueError('Notes cannot exceed 8192 characters')
    if 'tags' in changes:
        if not isinstance(changes['tags'], str):
            raise ValueError('Invalid tags')
        entries = sorted(set(tag.strip().lower() for tag in re.split(r'[;,]', changes['tags']) if tag.strip()))
        if len(entries) > 32 or any(not re.fullmatch(r'[a-z0-9_][a-z0-9_.+-]{0,63}', tag) for tag in entries):
            raise ValueError('Invalid tags')
        changes = dict(changes, tags=';'.join(entries))
    if 'hookscript' in changes:
        if os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam') != 'root@pam':
            raise ValueError('Changing the hookscript is only allowed for root@pam')
        if not isinstance(changes['hookscript'], str):
            raise ValueError('Invalid hookscript')
        if changes['hookscript']:
            pve_hook(changes['hookscript'])
    if 'onboot' in changes:
        if changes['onboot'] not in (0, 1):
            raise ValueError('Start at boot must be 0 or 1')
        changes = dict(changes, onboot=int(changes['onboot']))
    if 'startup' in changes:
        parsed = parse_startup(changes['startup'])
        changes = dict(changes, startup=','.join(f'{key}={parsed[key]}' for key in ('order', 'up', 'down') if key in parsed))
    if 'protection' in changes:
        if changes['protection'] not in (0, 1):
            raise ValueError('Protection must be 0 or 1')
        changes = dict(changes, protection=int(changes['protection']))
    with locked(identifier):
        info = detail(identifier, node)
        assert_configuration_digest(identifier, node, digest)
        if info.get('template') and changes.get('onboot'):
            raise ValueError('A DCT template cannot start at boot')
        authorize_config_keys(changes)
        info.pop('deployment', None)
        path = ROOT / identifier / 'notes.txt'
        previous = path.read_text() if path.exists() else None
        try:
            if 'description' in changes:
                fd, temporary = tempfile.mkstemp(prefix='.notes-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w') as handle:
                        handle.write(changes['description'])
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            info.update({key: value for key, value in changes.items() if key != 'description'})
            if 'hookscript' in changes:
                hook_journal(identifier).unlink(missing_ok=True)
            if set(changes) - {'description'}:
                write_json(METADATA / (identifier + '.json'), info)
        except Exception:
            if 'description' in changes:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(previous)
                    os.chmod(path, 0o600)
            raise


def check_protection(info, message="can't remove DCT"):
    if info.get('protection'):
        raise ValueError(message + ' - protection mode enabled')


def project_protection(identifier, node, value=None, digest=None):
    with locked(identifier):
        info = detail(identifier, node)
        if value is not None:
            if str(value) not in ('0', '1'):
                raise ValueError('Protection must be 0 or 1')
            assert_configuration_digest(identifier, node, digest)
            info.pop('deployment', None)
            info['protection'] = int(value)
            write_json(METADATA / (identifier + '.json'), info)
            info = detail(identifier, node)
        return {'protection': int(bool(info.get('protection'))), 'digest': info['digest']}


def check_protected_layout(info, before, after):
    if not info.get('protection'):
        return
    def layout(config):
        return {name: {'image': service.get('image'), 'build': service.get('build'),
                       'replicas': service.get('deploy', {}).get('replicas', 1),
                       'volumes': service.get('volumes', []), 'storage_opt': service.get('storage_opt', {})}
                for name, service in config['services'].items()}
    if layout(before) != layout(after) or before.get('volumes', {}) != after.get('volumes', {}):
        check_protection(info, "can't update DCT services or disks")


def refresh(node):
    def update_status(info):
        try:
            write_json(METADATA / (info['id'] + '.json'), info)
        except ValueError as error:
            # A migration may hand off ownership after this refresh collected its list.
            # Its old node must stop publishing status, without failing other guests.
            if str(error) != 'DCT ownership moved to another node':
                raise
    if any((ROOT.parent / 'transfers').glob('*/source.json')):
        from transfer import recover_all
        recover_all()
    # Rebinding may recreate containers, so refresh the state list afterwards.
    projects = [info for info in summaries() if info['node'] == node and not info.get('ha_recovery')]
    for info in projects:
        if operation_state(info['id']).get('active') or (ROOT / info['id'] / 'settings-transaction.json').exists():
            continue
        with locked(info['id']):
            if not (METADATA / (info['id'] + '.json')).exists():
                continue
            # Portable state transactions use the same per-project operation lock.
            if (ROOT / info['id'] / 'export-transaction.json').exists() or (ROOT.parent / 'imports' / (info['id'] + '.json')).exists():
                from guest_state import recover
                recover(info['id'])
            if not (METADATA / (info['id'] + '.json')).exists():
                continue
            recover_network_transaction(info['id'], node)
            info.update(json.loads((METADATA / (info['id'] + '.json')).read_text()))
            if info['node'] != node or info.get('ha_recovery'):
                continue
            try:
                journal = ROOT / info['id'] / 'snapshot-transaction.json'
                if journal.exists() and json.loads(journal.read_text()).get('action') == 'create':
                    recover_snapshot_transaction(info['id'])
                sync_dhcp(info)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                # One broken lease must not freeze status refresh for every guest on the node.
                info['status'] = 'network-error'
                update_status(info)
                print(f"Network unavailable for DCT {info.get('vmid', info['id'])}: {error}", file=sys.stderr)
    projects = [info for info in projects if info['node'] == node and not info.get('ha_recovery') and (METADATA / (info['id'] + '.json')).exists() and
                not (ROOT.parent / 'imports' / (info['id'] + '.json')).exists()]
    entries = json.loads('[' + ','.join(capture(['docker', 'ps', '-a', '--filter', 'label=betterdocker.id',
                                              '--format', '{{json .}}']).splitlines()) + ']')
    for info in projects:
        if operation_state(info['id']).get('active') or (ROOT / info['id'] / 'settings-transaction.json').exists():
            continue
        with locked(info['id']):
            if not (METADATA / (info['id'] + '.json')).exists():
                continue
            if info['status'] == 'network-error':
                continue
            states = [entry['State'] for entry in entries
                      if ('betterdocker.id=' + info['id']) in entry.get('Labels', '').split(',')]
            status = 'running' if states and len(states) >= info.get('expected', 1) and all(state == 'running' for state in states) else (
                'partial' if 'running' in states else 'stopped')
            # Preserve a failed deployment until it is started successfully or stopped explicitly.
            if info['status'] == 'error' and status != 'running':
                continue
            info['status'] = status
            update_status(info)
    due = [info for info in projects if not info.get('template') and (not (ROOT / info['id'] / 'metrics.json').exists() or
           time.time() - (ROOT / info['id'] / 'metrics.json').stat().st_mtime >= 60)]
    if due:
        active = [entry['ID'] for entry in entries if entry['State'] == 'running']
        try:
            shared_stats = [json.loads(line) for line in capture(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *active]).splitlines()] if active else []
            for info in due:
                if operation_state(info['id']).get('active'):
                    continue
                with locked(info['id']):
                    if not (METADATA / (info['id'] + '.json')).exists():
                        continue
                    snapshot = management(info['id'], node, shared_stats=shared_stats)
                    if snapshot.get('ha_recovery'):
                        continue
                    record_metrics(snapshot)
                    public = json.loads((METADATA / (info['id'] + '.json')).read_text())
                    public['metrics'] = {key: snapshot['metrics'][key] for key in ('cpu', 'cpus', 'mem', 'maxmem', 'uptime', 'time')}
                    write_json(METADATA / (info['id'] + '.json'), public)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f'Metrics unavailable: {error}', file=sys.stderr)


def action(identifier, node, operation, timeout=60, destroy_data=False):
    with locked(identifier):
        recover_network_transaction(identifier, node)
        info = detail(identifier, node)
        recover_storage_transaction(identifier)
        recover_snapshot_transaction(identifier)
        directory = ROOT / identifier
        commands = {'start': ['up', '-d'], 'stop': ['stop'], 'restart': ['restart'], 'delete': ['down']}
        if operation not in commands and operation not in ('shutdown', 'reboot', 'force-stop'):
            raise ValueError('Unsupported operation')
        if info.get('template') and operation in ('start', 'restart', 'reboot'):
            raise ValueError('A DCT template cannot be started; clone it first')
        if operation == 'delete':
            if info.get('replication'):
                raise ValueError('Remove replication jobs and their state disk before deleting the DCT')
            check_protection(info)
        records = [] if operation == 'stop' and not info.get('hookscript') else container_records(identifier)
        was_running = any(item['state'] in ('running', 'restarting', 'paused') for item in records)
        if operation == 'delete' and was_running:
            raise ValueError('DCT is running. Stop it before removal.')
        if operation == 'reboot' and not was_running:
            raise ValueError('DCT is not running')
        owned_volumes = owned_data_plan(identifier, info) if operation == 'delete' and destroy_data else []
        recover_guest_hook(info)
        if operation == 'start' and records and len(records) == info.get('expected') and all(item['state'] == 'running' for item in records):
            return
        stopping = operation in ('stop', 'shutdown', 'force-stop', 'restart', 'reboot')
        starting = operation in ('start', 'restart', 'reboot')
        if stopping and was_running:
            guest_hook(info, 'pre-stop')
            expect_guest_hook(info, 'post-stop')
            if operation in ('shutdown', 'reboot'):
                graceful_shutdown(identifier, timeout)
            elif operation == 'force-stop':
                ids = [item['id'] for item in records if item['state'] in ('running', 'restarting', 'paused')]
                run(['docker', 'kill', *ids], capture_output=True, timeout=30)
            else:
                run(compose_base(directory, identifier) + ['stop'], timeout=1800)
            guest_hook(info, 'post-stop')
        if starting:
            apply_pending_network(identifier, node)
            info = detail(identifier, node)
            prepare_network_start(info)
            info.pop('deployment', None)
            info.pop('network_resume', None)
            reconcile_network_runtime(info)
            clear_network_resume(identifier)
            info['status'] = 'creating'
            write_json(METADATA / (identifier + '.json'), info)
            apply_pending_settings(identifier, node)
            guest_hook(info, 'pre-start')
            expect_guest_hook(info, 'post-start')
            prepare_firewall(info)
            if not restored_start(identifier):
                containers = container_records(identifier)
                if containers:
                    from .service_start import start
                    start(identifier)
                else:
                    run(compose_base(directory, identifier) + ['up', '-d'], timeout=1800)
            finish_network_start(info)
            guest_hook(info, 'post-start')
        elif operation == 'stop' and not was_running:
            run(compose_base(directory, identifier) + ['stop'], timeout=1800)
        elif operation == 'delete':
            run(compose_base(directory, identifier) + ['down'], timeout=1800)
            # Older Engine-created replicas may lack Compose's config-hash label.
            # They still belong to this guest and must not outlive its deletion.
            remaining = container_records(identifier)
            if remaining:
                ids = [row['id'] for row in remaining]
                run(['docker', 'stop', '--time', '0', *ids], timeout=1800, capture_output=True)
                run(['docker', 'rm', *ids], capture_output=True)
        if operation in ('start', 'restart', 'reboot'):
            restore_restart_policy(identifier)
        if operation == 'delete':
            from .service_recreate import prune_images
            prune_images(identifier)
            # Reserve the metadata until the owned alias is successfully released.
            remove_network(info)
            if storage_config(identifier):
                storage_unmount(identifier)
                unit = capture(['systemd-escape', '--path', '--suffix=mount', str(directory / 'storage-data')]).strip()
                run(['systemctl', 'disable', unit], check=False, capture_output=True)
            if destroy_data:
                remove_owned_data(identifier, owned_volumes)
            if storage_config(identifier).get('volid'):
                reservation = METADATA / 'ids' / (str(info['vmid']) + '.json')
                owner = json.loads(reservation.read_text())
                if owner['id'] != identifier:
                    raise ValueError('DCT guest ID ownership changed')
                write_json(reservation, dict(owner, name=info['name']))
            if info.get('vmid'):
                (Path('/etc/pve/firewall') / (str(info['vmid']) + '.fw')).unlink(missing_ok=True)
            hook_journal(identifier).unlink(missing_ok=True)
            (METADATA / (identifier + '.json')).unlink()
            release_id(identifier)
            if info.get('firewall'):
                run([str(HELPERS / 'firewall.py'), 'apply'])
            print('Project and owned data removed.' if destroy_data else f'Project removed. Volumes and bind-mount data are retained at {directory}.')
        else:
            info.pop('deployment', None)
            if operation in ('stop', 'shutdown', 'force-stop'):
                clear_network_resume(identifier)
                info.pop('network_resume', None)
                info['status'] = 'stopped'
                write_json(METADATA / (identifier + '.json'), info)
            refresh(node)


# Operation markers contain no deployment data and live outside project directories.
# The per-project flock rejects conflicting requests instead of queueing them.
def check_shutdown_cancellation(identifier):
    path = ROOT.parent / 'operations' / (project_id(identifier) + '.cancel.json')
    if not path.exists():
        return
    request = json.loads(path.read_text())
    process_start = (PROC / str(os.getpid()) / 'stat').read_text().split(') ', 1)[1].split()[19]
    if request.get('pid') == os.getpid() and request.get('process_start') == process_start:
        raise ValueError('Shutdown interrupted by Stop.')


def operation_state(identifier):
    path = ROOT.parent / 'operations' / (project_id(identifier) + '.json')
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if state.get('active'):
        pid = state.get('pid', 0)
        try:
            alive = pid > 0 and (PROC / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19] == state.get('process_start')
        except (OSError, IndexError):
            alive = False
        if not alive:
            lock_path = ROOT.parent / 'operations' / (identifier + '.lock')
            held = False
            if lock_path.exists():
                with lock_path.open('r') as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        held = True
            if held:
                state.update(active=True, message='Operation helper is still running.')
            else:
                state.update(active=False, failed=True, message='Operation interrupted. Open Task History.')
    return state


@contextlib.contextmanager
def operation(identifier, name, overrule=False, service=None, wait_timeout=0):
    project_id(identifier)
    metadata = METADATA / (identifier + '.json')
    info = json.loads(metadata.read_text()) if metadata.exists() else {}
    if info.get('migration') and name not in ('migrate', 'migration-prepare', 'migration-abort', 'migration-recovery'):
        raise ValueError('DCT migration is in progress; recover it before another operation')
    if info.get('template') and name in ('update', 'settings', 'network', 'snapshot-restore'):
        raise ValueError('A DCT template is immutable; clone it before changing its deployment')
    from .images import state as image_state
    image_update = image_state(ROOT / identifier)
    if image_update.get('phase') == 'applying' and name not in ('snapshot-restore', 'snapshot-edit', 'force-stop', 'recovery'):
        raise ValueError('Interrupted image update: roll back snapshot ' + image_update['recovery'])
    folder = ROOT.parent / 'operations'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / (identifier + '.lock')).open('w') as handle:
        os.chmod(handle.name, 0o600)
        if wait_timeout:
            deadline = time.monotonic() + wait_timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('Timed out waiting for the DCT operation lock') from None
                    time.sleep(0.2)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not overrule or name != 'force-stop':
                raise ValueError('Another operation is running for this project') from None
            previous = operation_state(identifier)
            if not previous.get('active') or previous.get('action') != 'shutdown' or previous.get('service') != service:
                raise ValueError('Only a matching active Shutdown can be overruled') from None
            actor = os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam')
            owner = previous.get('user', '')
            privileged = os.environ.get('BETTERDOCKER_TASK_SYS_MODIFY', '1' if actor == 'root@pam' else '0') == '1'
            if not privileged and (not owner or actor.split('!', 1)[0] != owner.split('!', 1)[0]):
                raise ValueError('Permission denied: cannot overrule another user\'s Shutdown') from None
            token = {key: previous[key] for key in ('pid', 'process_start')}
            write_json(folder / (identifier + '.cancel.json'), token)
            deadline = time.monotonic() + 35
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('Shutdown did not release its lock; Stop was not performed') from None
                    time.sleep(0.2)
            # A competing request must not overrule a newer operation after handoff.
            latest = operation_state(identifier)
            if any(latest.get(key) != value for key, value in token.items()):
                raise ValueError('Operation changed before Stop acquired the lock')
        (folder / (identifier + '.cancel.json')).unlink(missing_ok=True)
        path = folder / (identifier + '.json')
        pid = os.getpid()
        state = {'active': True, 'action': name, 'pid': pid, 'started': int(time.time()),
                 'user': os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam'), 'service': service,
                 'process_start': (PROC / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19]}
        write_json(path, state)
        os.chmod(path, 0o600)
        try:
            from .service_recreate import recover as recover_settings
            with locked(identifier):
                recover_settings(identifier)
                from .disks import recover as recover_disks
                recover_disks(identifier)
            if os.environ.get('BETTERDOCKER_REQUIRE_SAFE') == '1' and (ROOT / identifier / 'compose.runtime.json').exists():
                delegated_access(identifier, current_node())
            previous_fds = getattr(_operation_fds, 'values', {})
            _operation_fds.values = dict(previous_fds, **{identifier: handle.fileno()})
            try:
                yield handle
            finally:
                _operation_fds.values = previous_fds
        except Exception:
            state.update(active=False, failed=True, message='Operation failed. Open Task History.')
            raise
        finally:
            state.update(active=False, finished=int(time.time()))
            write_json(path, state)


def project_limit_config(identifier, node, values=None, digest=None):
    with locked(identifier):
        info = detail(identifier, node)
        directory = ROOT / project_id(identifier)
        if values is None:
            return dict(project_limits.load(directory), digest=info['digest'])
        actor = os.environ.get('BETTERDOCKER_TASK_USER', 'root@pam')
        if os.environ.get('BETTERDOCKER_TASK_SYS_MODIFY', '1' if actor == 'root@pam' else '0') != '1':
            raise ValueError('Permission denied: project budgets require node Sys.Modify')
        assert_configuration_digest(identifier, node, digest)
        require_stopped(container_records(identifier))
        if info.get('template') or info.get('ha', {}).get('managed'):
            raise ValueError('Project budgets require an unmanaged non-template DCT')
        ceilings = project_limits.validate(values)
        config = json.loads((directory / 'compose.runtime.json').read_text())
        project_limits.check(config, ceilings)
        apply_settings(config, load_pending(directory))
        project_limits.check(config, ceilings)
        records = container_records(identifier)
        if records and any(ceilings.values()):
            inspected = json.loads(capture(['docker', 'inspect', *[item['id'] for item in records]]))
            project_limits.check_containers(inspected, ceilings)
        write_json(directory / 'limits.json', ceilings)
        os.chmod(directory / 'limits.json', 0o600)
        return ceilings


def project_tags(identifier, node, tags=None):
    with locked(identifier):
        info = detail(identifier, node)
        info.pop('deployment', None)
        if tags is not None:
            entries = sorted(set(tag.strip().lower() for tag in re.split(r'[;,]', tags) if tag.strip()))
            if len(entries) > 32 or any(not re.fullmatch(r'[a-z0-9_][a-z0-9_.+-]{0,63}', tag) for tag in entries):
                raise ValueError('Invalid tags')
            info['tags'] = ';'.join(entries)
            write_json(METADATA / (identifier + '.json'), info)
        return {'tags': info.get('tags', '')}


def service_action(identifier, node, service, verb, timeout=60):
    with locked(identifier):
        recover_snapshot_transaction(identifier)
        recover_network_transaction(identifier, node)
        info = detail(identifier, node)
        directory = ROOT / identifier
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if service not in config['services'] or verb not in ('start', 'stop', 'restart', 'shutdown', 'reboot', 'force-stop'):
            raise ValueError('Invalid service operation')
        if info.get('template') and verb in ('start', 'restart', 'reboot'):
            raise ValueError('A DCT template cannot be started; clone it first')
        if verb in ('start', 'restart', 'reboot'):
            prepare_network_start(info)
            prepare_firewall(info)
        if verb == 'start':
            apply_pending_settings(identifier, node, service)
        if verb in ('shutdown', 'reboot'):
            ids = graceful_shutdown(identifier, timeout, service)
            if verb == 'reboot' and ids:
                apply_pending_settings(identifier, node, service)
                ids = [c['id'] for c in container_records(identifier) if c['service'] == service]
                run(['docker', 'start', *ids], timeout=1800)
        elif verb == 'force-stop':
            ids = [item['id'] for item in container_records(identifier)
                   if item['service'] == service and item['state'] in ('running', 'restarting', 'paused')]
            if ids:
                run(['docker', 'kill', *ids], capture_output=True, timeout=30)
        else:
            command = {'start': ['up', '-d', '--no-deps'], 'stop': ['stop'], 'restart': ['restart']}[verb]
            if verb != 'start' or not restored_start(identifier, service):
                containers = [c for c in container_records(identifier) if c['service'] == service] if verb == 'start' else []
                expected = config['services'][service].get('deploy', {}).get('replicas', 1)
                if containers and len(containers) == expected:
                    from .service_start import start
                    start(identifier, selected=service)
                else:
                    run(compose_base(directory, identifier) + command + [service], timeout=1800)
        clear_network_resume(identifier, service)
        if verb in ('start', 'restart', 'reboot'):
            finish_network_start(info)
            restore_restart_policy(identifier, service)
        refresh(node)


def project_logs(identifier, node, service='', container='', tail=200):
    detail(identifier, node)
    tail = int(tail)
    if not 1 <= tail <= 5000:
        raise ValueError('Tail must be between 1 and 5000')
    if container:
        if not any(c['id'] == container for c in container_records(identifier)):
            raise ValueError('Container does not belong to this project')
        from .log_output import bounded
        return bounded(['docker', 'logs', '--timestamps', '--tail', str(tail), container])
    config = json.loads((ROOT / identifier / 'compose.runtime.json').read_text())
    if service and service not in config['services']:
        raise ValueError('Unknown service')
    from .log_output import bounded
    return bounded(compose_base(ROOT / identifier, identifier) + ['logs', '--no-color', '--timestamps', '--tail', str(tail)] + ([service] if service else []))


def diagnostics(node):
    if node != current_node():
        raise ValueError('Wrong node')
    def probe(command):
        try:
            return capture(command).strip()
        except (OSError, subprocess.SubprocessError):
            return 'Unavailable'
    docker_root = probe(['docker', 'info', '--format', '{{.DockerRootDir}}'])
    disk = shutil.disk_usage(docker_root) if Path(docker_root).is_dir() else None
    projects = [item for item in summaries() if item['node'] == node]
    targets = ['/usr/share/pve-manager/js/betterdocker.js', '/usr/share/pve-manager/css/betterdocker.css',
               '/usr/share/perl5/PVE/API2/BetterDocker.pm', '/usr/local/lib/betterdocker/provision.py']
    markers = ['/usr/share/pve-manager/index.html.tpl', '/usr/share/perl5/PVE/API2/Nodes.pm',
               '/usr/share/perl5/PVE/API2/Cluster.pm', '/usr/share/perl5/PVE/Cluster.pm']
    result = run([sys.executable, str(HELPERS / 'patch.py'), 'status'], check=False, capture_output=True)
    integration = all(Path(p).is_file() for p in targets) and result.returncode == 0
    selected = node_runtime.load()
    from .version import VERSION
    return {'addon': VERSION, 'runtime': selected.mode, 'runtime_health': node_runtime.service('is-active', runner=run).stdout.strip(),
            'pve': probe(['pveversion']), 'engine': probe(['docker', '--version']),
            'compose': probe(['docker', 'compose', 'version', '--short']),
            'timer': probe(['systemctl', 'is-active', 'betterdocker-refresh.timer']),
            'addresses': probe(['systemctl', 'is-active', 'betterdocker-addresses.service']),
            'integration': 'OK' if integration else 'Repair required',
            'integration_check': probe(['systemctl', 'show', '--property=Result', '--value', 'betterdocker-integration.service']), 'projects': len(projects),
            'storage': {'path': docker_root, 'total': disk.total, 'used': disk.used, 'free': disk.free} if disk else {},
            'dhcp': [{'vmid': p.get('vmid'), 'state': probe(['systemctl', 'is-active', 'betterdocker-dhcp@' + p['id'] + '.service'])}
                     for p in projects if p.get('ipv4mode') == 'dhcp']}


# DCT snapshots are private, complete copies independent of the Docker image cache.


def restored_start(identifier, service=None):
    marker = ROOT / identifier / 'snapshot-restored.json'
    if not marker.exists():
        return False
    restored = json.loads(marker.read_text())
    runtime = json.loads((ROOT / identifier / 'compose.runtime.json').read_text())
    addresses = {port.get('host_ip') for service_config in runtime['services'].values() for port in service_config.get('ports', [])}
    if addresses and addresses != set(restored.get('addresses', [restored.get('address')])):
        raise ValueError('DCT address changed after rollback; restore a snapshot again to rebind preserved filesystems')
    saved = set(restored['containers'])
    records = container_records(identifier)
    if {row['id'] for row in records} != saved:
        raise ValueError('Restored DCT instances changed; redeploy before starting')
    ids = [row['id'] for row in records if not service or row['service'] == service]
    if ids:
        from .service_start import start
        start(identifier, selected=service)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['retained-data', 'recover', 'dhcp6-client', 'dhcp6-release', 'disk', 'image-state', 'image-preview', 'image-apply', 'storage-volume-state', 'delete-storage-volume', 'project-limits', 'network', 'network-config', 'firewall-check', 'daemon-unmanage', 'lifecycle', 'daemon-prepare', 'daemon-resume', 'lifecycle-stop', 'validate', 'create', 'detail', 'config', 'access-state', 'protection', 'check-digest', 'action', 'logs', 'restore', 'refresh', 'update', 'dhcp-client', 'dhcp-release', 'management', 'settings', 'pending', 'revert-settings', 'console-check', 'console', 'delegated-check', 'new-id', 'notes', 'rrddata', 'reserve-id', 'release-id', 'preview', 'tags', 'service-action', 'reset-settings', 'diagnostics', 'storage-limit', 'storage-config', 'purge-retained-storage', 'snapshots', 'snapshot', 'snapshot-config', 'snapshot-edit'])
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    values = args.arguments
    if args.operation == 'retained-data':
        from .data import retained_data
        print(json.dumps(retained_data(values[0], destroy=len(values) > 1 and values[1] == '1')))
    elif args.operation == 'recover':
        from .recovery import recover
        recover(*values)
    elif args.operation in ('dhcp6-client', 'dhcp6-release'):
        from .ipv6_dynamic import client
        client(values[0], release=args.operation == 'dhcp6-release')
    elif args.operation == 'disk':
        from .disks import configure
        configure(values[0], values[1], values[2], json.loads(values[3]), values[4] if len(values) > 4 else None)
    elif args.operation == 'image-state':
        detail(*values)
        from .images import state
        print(json.dumps(state(ROOT / values[0])))
    elif args.operation in ('image-preview', 'image-apply'):
        from . import images
        with operation(values[0], 'update'):
            if args.operation == 'image-preview':
                images.preview(values[0], values[1], json.loads(Path(values[2]).read_text()), values[3])
            else:
                images.apply(*values)
    elif args.operation == 'project-limits':
        if len(values) > 2:
            with operation(values[0], 'settings'):
                result = project_limit_config(values[0], values[1], json.loads(Path(values[2]).read_text()), values[3] if len(values) > 3 else None)
        else:
            result = project_limit_config(*values)
        print(json.dumps(result))
    elif args.operation == 'reserve-id':
        print(reserve_id(values[0], values[1], values[2] if len(values) > 2 else None))
    elif args.operation == 'release-id':
        if not (METADATA / (project_id(values[0]) + '.json')).exists():
            release_id(values[0])
    elif args.operation == 'new-id':
        print(uuid.uuid4().hex)
    elif args.operation in ('validate', 'create'):
        data = validate(json.loads(Path(values[0]).read_text()))
        if args.operation == 'create':
            with operation(values[2], 'create'):
                create(data, values[1], values[2], values[3] if len(values) > 3 else None,
                       start=len(values) < 5 or values[4] != '0')
    elif args.operation == 'network-config':
        print(json.dumps(network_config(*values)))
    elif args.operation == 'network':
        with operation(values[0], 'network'):
            project_network(values[0], values[1], json.loads(Path(values[2]).read_text()), values[3] if len(values) > 3 else None)
    elif args.operation == 'update':
        with operation(values[0], 'update'):
            update(values[0], values[1], validate(json.loads(Path(values[2]).read_text())), values[3] if len(values) > 3 else None)
    elif args.operation in ('dhcp-client', 'dhcp-release'):
        dhcp_client(values[0], release=args.operation == 'dhcp-release')
    elif args.operation == 'notes':
        print(json.dumps(notes(values[0], values[1], Path(values[2]).read_text() if len(values) > 2 else None)))
    elif args.operation == 'rrddata':
        print(json.dumps(rrddata(*values)))
    elif args.operation == 'management':
        print(json.dumps(management(*values)))
    elif args.operation == 'pending':
        print(json.dumps(pending_settings(*values)))
    elif args.operation == 'revert-settings':
        with operation(values[0], 'settings'):
            revert_settings(*values)
    elif args.operation == 'settings':
        with operation(values[0], 'settings'):
            service_settings(values[0], values[1], values[2], json.loads(Path(values[3]).read_text()), values[4] if len(values) > 4 else None)
    elif args.operation == 'firewall-check':
        firewall_check(*values)
    elif args.operation == 'delegated-check':
        print(json.dumps(delegated_access(*values)))
    elif args.operation in ('console-check', 'console'):
        command = console_target(*values)
        if args.operation == 'console':
            node_runtime.exec_docker(command, delegated=not trusted_management())
    elif args.operation == 'detail':
        print(json.dumps(detail(*values)))
    elif args.operation == 'lifecycle':
        print(json.dumps(lifecycle_config(*values)))
    elif args.operation == 'daemon-unmanage':
        daemon_unmanage(values[0] if values else current_node())
    elif args.operation == 'daemon-prepare':
        daemon_prepare(values[0] if values else current_node())
    elif args.operation == 'daemon-resume':
        daemon_resume(values[0] if values else current_node())
    elif args.operation == 'lifecycle-stop':
        lifecycle_stop(*values)
    elif args.operation == 'config':
        project_config(values[0], values[1], json.loads(Path(values[2]).read_text()), values[3] if len(values) > 3 else None)
    elif args.operation == 'access-state':
        print(json.dumps(access_state(*values)))
    elif args.operation == 'protection':
        print(json.dumps(project_protection(*values)))
    elif args.operation == 'check-digest':
        assert_configuration_digest(*values)
    elif args.operation == 'action':
        overrule = len(values) > 4 and values[4] == '1'
        if overrule and values[2] != 'force-stop':
            raise ValueError('Overrule is only available for Stop')
        with operation(values[0], values[2], overrule=overrule):
            action(*values[:4], destroy_data=len(values) > 5 and values[5] == '1')
    elif args.operation == 'logs':
        print(project_logs(*values), end='')
    elif args.operation == 'preview':
        print(json.dumps(preview(json.loads(Path(values[0]).read_text()), values[1] if len(values) > 1 else None)))
    elif args.operation == 'tags':
        print(json.dumps(project_tags(*values)))
    elif args.operation == 'service-action':
        overrule = len(values) > 5 and values[5] == '1'
        if overrule and values[3] != 'force-stop':
            raise ValueError('Overrule is only available for Stop')
        with operation(values[0], values[3], overrule=overrule, service=values[2]):
            service_action(*values[:5])
    elif args.operation == 'reset-settings':
        with operation(values[0], 'settings'):
            reset_settings(*values)
    elif args.operation == 'snapshot-config':
        print(json.dumps(snapshot_config(*values)))
    elif args.operation == 'snapshot-edit':
        with operation(values[0], 'snapshot-edit'):
            snapshot_edit(*values)
    elif args.operation == 'snapshots':
        print(json.dumps(snapshot_list(*values)))
    elif args.operation == 'snapshot':
        with operation(values[0], 'snapshot-' + values[2]):
            project_snapshot(*values)
    elif args.operation == 'storage-config':
        detail(*values)
        print(json.dumps(storage_config(values[0])))
    elif args.operation == 'storage-volume-state':
        print(json.dumps(storage_volume_state(*values)))
    elif args.operation == 'delete-storage-volume':
        delete_storage_volume(*values)
    elif args.operation == 'purge-retained-storage':
        purge_retained_storage(*values)
    elif args.operation == 'storage-limit':
        with operation(values[0], 'settings'):
            project_storage_limit(*values)
    elif args.operation == 'diagnostics':
        print(json.dumps(diagnostics(*values)))
    elif args.operation == 'restore':
        restore(current_node())
    elif args.operation == 'refresh':
        refresh(current_node())

# Network allocation touches shared node addresses; guest work remains independent.
network_preflight = node_locked(network_preflight)
ensure_address = node_locked(ensure_address)
remove_network = node_locked(remove_network)
pve_storage = node_locked(pve_storage)
