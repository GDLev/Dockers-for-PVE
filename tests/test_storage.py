"""Disk transaction recovery, ownership boundaries and interrupted snapshot recovery."""
import json
import tempfile
import subprocess
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from test_worker import worker

ID = 'a' * 32


class StorageTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        lock = patch.object(worker, 'LOCK', Path(temporary.name) / 'node.lock')
        lock.start()
        self.addCleanup(lock.stop)

    def test_refresh_skips_migrated_ownership_without_hiding_other_write_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'projects'
            metadata = Path(tmp) / 'metadata'
            metadata.mkdir()
            (root / ID).mkdir(parents=True)
            (root / ID / 'metrics.json').write_text('{}')
            info = {'id': ID, 'node': 'pve', 'status': 'stopped', 'expected': 1}
            (metadata / (ID + '.json')).write_text(json.dumps(info))
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'METADATA', metadata), \
                    patch.object(worker, 'summaries', return_value=[dict(info)]), \
                    patch.object(worker, 'capture', return_value=''), \
                    patch.object(worker, 'sync_dhcp'), patch.object(worker, 'recover_network_transaction'), \
                    patch.object(worker, 'write_json', side_effect=ValueError('DCT ownership moved to another node')):
                worker.refresh('pve')
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'METADATA', metadata), \
                    patch.object(worker, 'summaries', return_value=[dict(info)]), \
                    patch.object(worker, 'capture', return_value=''), \
                    patch.object(worker, 'sync_dhcp'), patch.object(worker, 'recover_network_transaction'), \
                    patch.object(worker, 'write_json', side_effect=ValueError('Unrelated failure')):
                with self.assertRaisesRegex(ValueError, 'Unrelated failure'):
                    worker.refresh('pve')

    def test_stopped_snapshot_flushes_dirty_filesystem_buffers(self):
        with patch.object(worker, 'container_records', return_value=[{'id': '1' * 64, 'state': 'exited'}]), \
                patch.object(worker.os, 'sync') as sync, patch.object(worker, 'run') as run:
            with worker.snapshot_quiesce(ID):
                sync.assert_called_once_with()
            run.assert_not_called()

    def test_pve_progress_output_does_not_corrupt_json_protocol(self):
        result = subprocess.CompletedProcess([], 0, stdout='Logical volume created\n{"volid":"local-lvm:vm-105-disk-0"}\n')
        with patch.object(worker, 'run', return_value=result):
            self.assertEqual(worker.pve_storage(ID, 'inspect')['volid'], 'local-lvm:vm-105-disk-0')

    def test_storage_errors_reach_task_log(self):
        error = subprocess.CalledProcessError(2, [], stderr='Insufficient storage space\n')
        with patch.object(worker, 'run', side_effect=error), self.assertRaisesRegex(ValueError, 'Insufficient storage space'):
            worker.pve_storage(ID, 'allocate')

    def test_retained_disks_keep_guest_id_reserved(self):
        with patch.object(worker, 'storage_config', return_value={'volid': 'local-lvm:vm-105-disk-0'}), patch.object(worker, 'reserved_ids') as ids:
            worker.release_id(ID)
            ids.assert_not_called()

    def test_purge_never_accepts_an_existing_guest(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'METADATA', Path(tmp)), patch.object(worker, 'locked'), patch.object(worker, 'operation'):
            (Path(tmp) / (ID + '.json')).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Remove the DCT'):
                worker.purge_retained_storage(ID)

    def test_interrupted_rollback_blocks_start_but_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp)):
            worker.write_json(Path(tmp) / ID / 'snapshot-transaction.json', {'action': 'restore', 'name': 'before'})
            with self.assertRaisesRegex(ValueError, 'retry restoration'):
                worker.recover_snapshot_transaction(ID)
            worker.recover_snapshot_transaction(ID, restoring=True)
            self.assertTrue((Path(tmp) / ID / 'snapshot-transaction.json').exists())

    def test_interrupted_capture_unpauses_only_owned_previously_running_instances(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp)), patch.object(worker, 'container_records', return_value=[
            {'id': 'one', 'state': 'paused'}, {'id': 'two', 'state': 'paused'}]), patch.object(worker, 'run') as run:
            journal = Path(tmp) / ID / 'snapshot-transaction.json'
            worker.write_json(journal, {'action': 'create', 'name': 'before', 'active': ['one', 'foreign']})
            worker.recover_snapshot_transaction(ID)
            run.assert_called_once_with(['docker', 'unpause', 'one'], capture_output=True)
            self.assertFalse(journal.exists())

    def test_snapshot_recovery_rejects_foreign_cleanup_paths(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp)), patch.object(worker, 'container_records', return_value=[]):
            worker.write_json(Path(tmp) / ID / 'snapshot-transaction.json', {'action': 'create', 'name': 'before', 'temporary': '/unrelated/.creating-x'})
            with self.assertRaisesRegex(ValueError, 'Invalid snapshot recovery path'):
                worker.recover_snapshot_transaction(ID)

    def test_interrupted_rollback_blocks_config_even_without_digest(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp)):
            worker.write_json(Path(tmp) / ID / 'snapshot-transaction.json', {'action': 'restore', 'name': 'before'})
            with self.assertRaisesRegex(ValueError, 'interrupted snapshot rollback'):
                worker.assert_configuration_digest(ID, 'pve', None)

    def test_recovery_finishes_tree_after_snapshot_directory_commit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp) / 'projects'), patch.object(worker, 'container_records', return_value=[]):
            complete = worker.snapshot_root(ID) / 'before'
            complete.mkdir(parents=True)
            (complete / 'manifest.json').write_text(json.dumps({'created': 2}))
            parent = worker.snapshot_root(ID) / 'parent'
            parent.mkdir()
            (parent / 'manifest.json').write_text(json.dumps({'created': 1}))
            worker.write_json(worker.ROOT / ID / 'snapshot-transaction.json', {
                'action': 'create', 'name': 'before', 'active': [], 'tree_state': {'current': 'parent'}})
            worker.recover_snapshot_transaction(ID)
            tree = worker.snapshot_tree_state(ID)
            self.assertEqual(tree['current'], 'before')
            self.assertEqual(tree['parents']['before'], 'parent')
