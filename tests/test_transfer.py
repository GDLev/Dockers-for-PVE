"""Returning to a fenced HA node must reclaim only its stopped enrolled copy."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_ha import transfer, worker, state, ID


class TransferTests(unittest.TestCase):
    def test_return_to_fenced_owner_cleans_only_enrolled_copy(self):
        for enrolled, marker in ((True, True), (False, True), (True, False)):
            with self.subTest(enrolled=enrolled, marker=marker), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / 'projects'; (root / ID).mkdir(parents=True)
                metadata = Path(temporary) / 'metadata'; metadata.mkdir()
                token = 'd' * 32
                folder = root.parent / 'transfers' / token; folder.mkdir(parents=True)
                current = {'id': ID, 'vmid': 100, 'node': 'source', 'name': 'app',
                           'migration': token, 'ha_enrolled': enrolled}
                state.write(metadata / (ID + '.json'), current)
                if marker:
                    state.write(root / ID / 'ha.json', {'managed': True})
                order = []
                def clean(identifier, local):
                    self.assertEqual(local['node'], 'target')
                    self.assertEqual(state.read(worker.METADATA / (ID + '.json'))['node'], 'target')
                    order.append('clean')
                    shutil.rmtree(root / identifier)
                def restore(*args, **kwargs):
                    if (root / ID).exists():
                        raise ValueError('Restore requires a new DCT')
                    self.assertFalse((worker.METADATA / (ID + '.json')).exists())
                    order.append('restore'); (root / ID).mkdir()
                    state.write(worker.METADATA / (ID + '.json'), {'id': ID, 'vmid': 100, 'node': 'target'})
                with patch.object(worker, 'ROOT', root), patch.object(worker, 'METADATA', metadata), \
                        patch.object(transfer, 'GLOBAL_METADATA', metadata), \
                        patch.object(transfer, 'TRANSFERS', folder.parent), \
                        patch.object(worker, 'current_node', return_value='target'), patch.object(worker, 'operation'), \
                        patch.object(state, 'unpack_state'), patch.object(state, 'restore_state', side_effect=restore), \
                        patch.object(transfer, 'clean_local', side_effect=clean) as cleanup, \
                        contextlib.redirect_stdout(io.StringIO()):
                    if enrolled and marker:
                        transfer.prepare(token, ID, 'source', '')
                        self.assertEqual(order, ['clean', 'restore'])
                    else:
                        with self.assertRaisesRegex(ValueError, 'Restore requires a new DCT'):
                            transfer.prepare(token, ID, 'source', '')
                        cleanup.assert_not_called()
                    self.assertEqual(json.loads((metadata / (ID + '.json')).read_text()), current)

    def test_active_former_copy_is_not_removed(self):
        with patch.object(worker, 'container_records', return_value=[{'id': 'old', 'state': 'running'}]), \
                patch.object(worker, 'snapshot_containers', return_value=[]), patch.object(state, 'owned_mounts'), \
                patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Cannot clean an active transfer copy'):
                transfer.clean_local(ID, {'id': ID})
            run.assert_not_called()

    def test_pending_endpoint_cannot_be_silently_discarded_by_migration(self):
        with patch.object(worker, 'operation'), patch.object(worker, 'locked'), \
             patch.object(worker, 'detail', return_value={'id':ID,'node':'source'}), \
             patch.object(worker, 'assert_configuration_digest'), \
             patch.object(worker, 'load_pending_network', return_value={'mtu':1400}), \
             patch.object(worker, 'recover_guest_hook') as hooks, patch.object(transfer.state, 'write') as write:
            with self.assertRaisesRegex(ValueError, 'Apply or revert pending network'):
                transfer.migrate(ID, 'source', 'target', '')
            hooks.assert_not_called()
            write.assert_not_called()

    def test_destination_import_has_a_transfer_timeout(self):
        from unittest.mock import Mock
        with patch.object(transfer, 'ssh_command', return_value=['ssh', 'target']), patch.object(worker, 'run', return_value=Mock(stdout='{}')) as run:
            transfer.remote('target', 'prepare', 'token')
        self.assertEqual(run.call_args.kwargs['timeout'], 86400)
