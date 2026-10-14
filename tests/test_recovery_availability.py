"""Unavailable runtime contracts using temporary journals and mocked commands."""
from contextlib import ExitStack
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import recovery, network, snapshots, disks, storage, service_recreate
import guest_state
import transfer

ID = 'a' * 32


class RecoveryAvailability(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.base = Path(temporary)
        self.root = self.base / 'projects'
        self.directory = self.root / ID
        self.directory.mkdir(parents=True)
        self.stack.enter_context(patch.object(w, 'ROOT', self.root))
        self.stack.enter_context(patch.object(w, 'METADATA', self.base / 'metadata'))
        self.stack.enter_context(patch.object(w, 'current_node', return_value='pve'))
        self.stack.enter_context(patch.object(w, 'container_records', return_value=[]))
        self.stack.enter_context(patch.object(w, 'locked'))
        self.service = self.stack.enter_context(patch.object(w.node_runtime, 'service', return_value=subprocess.CompletedProcess([], 0, '', '')))
        self.engine = self.stack.enter_context(patch.object(w, 'run', side_effect=subprocess.CalledProcessError(1, ['docker', 'info'], stderr='Docker is unavailable')))
        self.effects = [self.stack.enter_context(patch.object(w, name)) for name in ('remove_network', 'ensure_address', 'pve_storage', 'storage_mount', 'write_json')]

    def marker(self, filename):
        path = self.directory / filename
        path.write_text('retained')
        return path

    def refuses(self, path, function, *args):
        with self.assertRaises(recovery.DockerUnavailable):
            function(*args)
        self.assertEqual(path.read_text(), 'retained')
        for effect in self.effects:
            effect.assert_not_called()
        self.assertEqual(self.engine.call_args.args[0], ['docker', 'info', '--format', '{{.ID}}'])

    def test_inactive_service_is_rejected_without_contacting_socket(self):
        self.service.return_value = subprocess.CompletedProcess([], 3, '', '')
        with self.assertRaises(recovery.DockerUnavailable):
            recovery.require_docker()
        self.engine.assert_not_called()

    def test_active_service_with_unreachable_socket_is_rejected(self):
        with self.assertRaises(recovery.DockerUnavailable):
            recovery.require_docker()
        self.assertEqual(self.engine.call_args.kwargs['timeout'], 5)

    def test_socket_timeout_is_rejected(self):
        self.engine.side_effect = subprocess.TimeoutExpired(['docker', 'info'], 5)
        with self.assertRaises(recovery.DockerUnavailable):
            recovery.require_docker()

    def test_available_runtime_passes_bounded_read_only_probe(self):
        self.engine.side_effect = None
        self.engine.return_value = subprocess.CompletedProcess([], 0, 'fixture', '')
        recovery.require_docker()
        self.engine.assert_called_once_with(['docker', 'info', '--format', '{{.ID}}'], capture_output=True, timeout=5)

    def test_public_recovery_checks_before_dispatching_changes(self):
        marker = self.marker('network-transaction.json')
        with patch.object(w, 'operation'), patch.object(w, 'detail'), patch.object(recovery, 'status', return_value={}), patch.object(w, 'recover_storage_transaction') as recover_storage:
            self.refuses(marker, recovery.recover, ID, 'pve')
            recover_storage.assert_not_called()

    def test_network_recovery_preserves_files_and_endpoint(self):
        marker = self.marker('network-transaction.json')
        info = {'id': ID, 'node': 'pve'}
        with patch.object(network, 'read_json', side_effect=[{'metadata': info, 'files': {}}, info]):
            self.refuses(marker, w.recover_network_transaction, ID, 'pve')

    def test_snapshot_recovery_preserves_native_snapshot_and_journal(self):
        marker = self.marker('snapshot-transaction.json')
        with patch.object(snapshots, 'read_json', return_value={'action': 'create', 'native': {'volid': 'fixture'}}):
            self.refuses(marker, w.recover_snapshot_transaction, ID)

    def test_capture_recovery_preserves_journal(self):
        marker = self.marker('export-transaction.json')
        with patch.object(guest_state, 'journal_path', return_value=marker), patch.object(guest_state, 'read', return_value={'paused': [], 'restart': []}):
            self.refuses(marker, guest_state.cleanup_capture, ID)

    def test_import_recovery_checks_before_linked_disk_cleanup(self):
        marker = self.base / 'imports' / (ID + '.json')
        marker.parent.mkdir()
        marker.write_text('retained')
        linked = self.marker('linked-clone.json')
        with patch.object(guest_state, 'read', return_value={'id': ID, 'phase': 'preparing'}):
            self.refuses(marker, guest_state.cleanup_import, ID)
        self.assertEqual(linked.read_text(), 'retained')

    def test_service_recovery_preserves_preparing_and_committed_journals(self):
        marker = self.marker('settings-transaction.json')
        for phase in ('preparing', 'committed'):
            with self.subTest(phase=phase), patch.object(service_recreate, 'read_json', return_value={'phase': phase}), patch.object(service_recreate, 'validate_journal'), patch.object(service_recreate, 'inspect') as inspect:
                self.refuses(marker, service_recreate.recover, ID)
                inspect.assert_not_called()

    def test_disk_recovery_preserves_allocation_and_journal(self):
        marker = self.marker('disk-transaction.json')
        allocation = self.marker('allocated-volume.json')
        with patch.object(disks, 'read_json', return_value={'phase': 'committed'}):
            self.refuses(marker, disks.recover, ID)
        self.assertEqual(allocation.read_text(), 'retained')

    def test_storage_recovery_checks_socket_even_if_service_is_active(self):
        marker = self.marker('storage-transaction.json')
        with patch.object(storage, 'read_json', return_value={'operation': 'grow'}):
            self.refuses(marker, w.recover_storage_transaction, ID)

    def test_transfer_cleanup_checks_before_creating_cleanup_journal(self):
        retained = self.marker('request.json')
        self.refuses(retained, transfer.clean_local, ID, {'id': ID})
        self.assertFalse((self.directory / 'transfer-cleanup.json').exists())

    def test_boot_defers_network_recovery_and_continues_other_guests(self):
        rows = [{'id': ID, 'node': 'pve'}, {'id': 'b' * 32, 'node': 'pve'}]
        with patch.object(w, 'summaries', return_value=rows), patch.object(w, 'recover_storage_transaction'), patch.object(w, 'recover_network_transaction', side_effect=[recovery.DockerUnavailable('unavailable'), False]), patch('sys.stderr', new_callable=io.StringIO):
            w.restore('pve')
        w.ensure_address.assert_called_once_with(rows[1], wait=False)

    def test_boot_defers_storage_mount_and_endpoint_for_pending_guest(self):
        rows = [{'id': ID, 'node': 'pve'}, {'id': 'b' * 32, 'node': 'pve'}]
        self.marker('storage.json')
        with patch.object(w, 'summaries', return_value=rows), patch.object(w, 'recover_storage_transaction', side_effect=[recovery.DockerUnavailable('unavailable'), None]), patch.object(w, 'recover_network_transaction', return_value=False), patch('sys.stderr', new_callable=io.StringIO):
            w.restore('pve')
        w.storage_mount.assert_not_called()
        w.ensure_address.assert_called_once_with(rows[1], wait=False)

    def test_generic_not_found_does_not_mean_recovery_volume_is_absent(self):
        marker = self.marker('settings-transaction.json')
        value = {'phase': 'preparing', 'entries': [], 'resources': [{'kind': 'volume', 'name': 'fixture'}], 'files': {}}
        self.engine.side_effect = [subprocess.CompletedProcess([], 0, 'fixture', ''), subprocess.CompletedProcess([], 1, '', 'API endpoint not found')]
        with patch.object(service_recreate, 'read_json', return_value=value), patch.object(service_recreate, 'validate_journal'), patch.object(service_recreate, 'prune_images') as prune:
            with self.assertRaisesRegex(ValueError, 'Cannot inspect deployment recovery resource'):
                service_recreate.recover(ID)
            prune.assert_not_called()
        self.assertEqual(marker.read_text(), 'retained')
