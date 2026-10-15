"""Cluster installation membership and VMID protection during removal."""
import json
from pathlib import Path
import re
import subprocess

from .secure_io import atomic_text, read_json

METADATA = Path('/etc/pve/betterdocker')


def nodes():
    program = 'use PVE::Cluster; use JSON; PVE::Cluster::cfs_update(); print encode_json(PVE::Cluster::get_nodelist());'
    names = json.loads(subprocess.check_output(['perl', '-e', program], text=True))
    if not names or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', name) for name in names):
        raise ValueError('Cannot determine cluster membership')
    return names


def registered(node):
    path = METADATA / 'nodes' / (node + '.json')
    try:
        record = read_json(path)
    except FileNotFoundError:
        return False
    return isinstance(record, dict) and record.get('node') == node and record.get('state') == 'ready'


def require_cluster():
    missing = [name for name in nodes() if not registered(name)]
    if missing:
        raise ValueError('Install or repair BetterDockers on every cluster node before allocating DCT IDs. Missing: ' + ', '.join(sorted(missing)))


def check_uninstall():
    # Reservations survive guest deletion while managed disks are retained.
    if any(METADATA.glob('*.json')) or any((METADATA / 'ids').iterdir() if (METADATA / 'ids').exists() else ()):
        raise ValueError('Remove all DCTs and their retained disks from the cluster before uninstalling BetterDockers. Guest ID reservations must be empty.')


def registration(action, node):
    """The caller holds PVE's datacenter.cfg lock, also used for ID allocation."""
    if node not in nodes():
        raise ValueError('Node is not a current cluster member')
    path = METADATA / 'nodes' / (node + '.json')
    if action == 'ready':
        from .version import VERSION
        atomic_text(path, json.dumps(dict(node=node, state='ready', version=VERSION)) + '\n', public=True)
    elif action == 'remove':
        check_uninstall()
        path.unlink(missing_ok=True)
    else:
        raise ValueError('Invalid installation registration action')
