"""Names reserved for host control state, never exposed as workload mounts."""
from pathlib import PurePath

CONFIG_FILES = ('request.json', 'compose.source.yaml', '.env', 'compose.runtime.json',
                'images.json', 'service-defaults.json', 'settings.json', 'pending.json',
                'pending-network.json', 'notes.txt', 'limits.json')
CONTROL_NAMES = frozenset(CONFIG_FILES) | frozenset({
    'settings-transaction.json', 'deployment-transaction.json', 'image-update.json',
    'storage.json', 'storage.ext4', 'ha.json', 'snapshots', 'export-transaction.json',
    'allocated-volume.json', 'snapshot-restored.json', 'snapshot-tree.json',
    'snapshot-transaction.json', 'storage-transaction.json', 'storage-transfer',
    'network-transaction.json', 'network-resume.json', 'network-reconcile.json',
    'dhcp-address.json', 'dhclient.conf', 'dhclient.leases', 'metrics.json',
    'disk-usage.json', 'rootfs-quota.json', 'replication.json', 'transfer-cleanup.json',
    'owned-volumes.json', 'disk-transaction.json', 'storage-usage.json', 'linked-clone.json',
})


def control_path(relative):
    parts = PurePath(relative).parts
    return not parts or parts[0] in CONTROL_NAMES or parts[0].startswith('.') or (
        len(parts) == 1 and PurePath(parts[0]).suffix in ('.json', '.yaml', '.yml'))
