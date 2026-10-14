"""Public recovery hints and explicit recovery of interrupted local operations."""
from .secure_io import read_json
import json
import subprocess


class DockerUnavailable(ValueError):
    """Recovery must wait without changing its journal or guest state."""


def require_docker():
    from . import projects as w
    try:
        if w.node_runtime.service('is-active', runner=w.run).returncode:
            raise DockerUnavailable('Docker is unavailable; recovery state was retained')
        w.run(['docker', 'info', '--format', '{{.ID}}'], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise DockerUnavailable('Docker is unavailable; recovery state was retained') from error

JOURNALS = {'settings-transaction.json': 'Container configuration', 'disk-transaction.json': 'Managed disks',
            'storage-transaction.json': 'Data migration', 'network-transaction.json': 'Network configuration',
            'network-reconcile.json': 'Network bindings', 'snapshot-transaction.json': 'Snapshot',
            'export-transaction.json': 'Backup or export'}


def status(identifier):
    from . import projects as w
    directory = w.ROOT / identifier
    interrupted = [label for file, label in JOURNALS.items() if (directory / file).exists()]
    result = {'required': bool(interrupted), 'operations': interrupted, 'available': not w.operation_state(identifier).get('active')}
    for filename in ('image-update.json', 'snapshot-transaction.json'):
        path = directory / filename
        if path.exists():
            value = read_json(path)
            if value.get('phase') == 'applying' or value.get('action') == 'restore':
                result.update(required=True, available=False, rollback=value.get('recovery') or value.get('name'))
    return result


def recover(identifier, node):
    from . import projects as w
    with w.operation(identifier, 'recovery'), w.locked(identifier):
        w.detail(identifier, node)
        info = status(identifier)
        if info.get('rollback'):
            raise ValueError('Use snapshot rollback for ' + info['rollback'])
        require_docker()
        w.recover_storage_transaction(identifier)
        w.recover_network_transaction(identifier, node)
        w.recover_snapshot_transaction(identifier)
        if (w.ROOT / identifier / 'export-transaction.json').exists():
            from guest_state import cleanup_capture
            cleanup_capture(identifier)
        w.reconcile_network_runtime(w.detail(identifier, node))
        w.refresh(node)
