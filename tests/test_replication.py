"""DCT replication must keep the last state intact and recover interrupted allocation."""
import contextlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker
from test_guest_state import state

spec = importlib.util.spec_from_file_location('replica', Path(__file__).resolve().parents[1] / 'scripts/replica.py')
replica = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'provision': worker, 'guest_state': state}):
    spec.loader.exec_module(replica)

ID = 'a' * 32


class ReplicationTests(unittest.TestCase):
    def test_failed_format_is_retried_without_allocating_another_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'
            metadata = Path(temporary) / 'metadata'
            metadata.mkdir(); (root / ID).mkdir(parents=True)
            info = {'id': ID, 'vmid': 100, 'node': 'pve', 'deployment': {'environment': 'private-value'}}
            volume = {'volid': 'zfs:vm-100-disk-1', 'path': '/dev/zvol/test', 'size_mib': 256}
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'METADATA', metadata), \
                    patch.object(state, 'operation', side_effect=lambda *a: contextlib.nullcontext()), \
                    patch.object(worker, 'locked', side_effect=contextlib.nullcontext), \
                    patch.object(worker, 'detail', return_value=info), \
                    patch.object(worker, 'storage_config', return_value={'type': 'zfspool', 'storage': 'zfs'}), \
                    patch.object(worker, 'pve_storage', return_value=dict(volume)) as storage, \
                    patch.object(worker, 'run', side_effect=ValueError('format failed')):
                # The storage bridge records allocation before returning the volume.
                (root / ID / 'allocated-volume.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'format failed'):
                    replica.ensure(ID, 'pve', 1)
                self.assertFalse(replica.record(ID)['initialized'])
                storage.reset_mock()
                with patch.object(worker, 'run'):
                    result = replica.ensure(ID, 'pve', 1)
                self.assertTrue(result['initialized'])
                self.assertEqual(storage.call_args.args[1], 'inspect')
                self.assertEqual(result['volid'], volume['volid'])
                self.assertNotIn('private-value', (metadata / (ID + '.json')).read_text())

    def test_failed_capture_does_not_mount_or_replace_replication_state(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), \
                patch.object(state, 'operation', side_effect=lambda *a: contextlib.nullcontext()), \
                patch.object(replica, 'ensure', return_value={'volid': 'zfs:vm-100-disk-1'}), \
                patch.object(state, 'capture_state', side_effect=ValueError('external mount')), \
                patch.object(replica, 'mount_volume') as mount:
            with self.assertRaisesRegex(ValueError, 'external mount'):
                replica.prepare(ID, 'pve', 1)
            mount.assert_not_called()

    def test_replication_requires_zfs_before_allocating(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), \
                patch.object(state, 'operation', side_effect=lambda *a: contextlib.nullcontext()), \
                patch.object(worker, 'locked', side_effect=contextlib.nullcontext), \
                patch.object(worker, 'detail', return_value={'id': ID}), \
                patch.object(worker, 'storage_config', return_value={'type': 'lvmthin'}), \
                patch.object(worker, 'pve_storage') as storage:
            with self.assertRaisesRegex(ValueError, 'managed ZFS'):
                replica.ensure(ID, 'pve', 1)
            storage.assert_not_called()

    def test_export_rejects_missing_checkpoint_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'capture') as capture, \
                patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'successful replication snapshot'):
                replica.export('zfs:vm-100-disk-1', Path(temporary) / 'out', 'head', '123')
            capture.assert_not_called()
            run.assert_not_called()

    def test_export_cleanup_after_corrupt_archive_uses_snapshot_clone(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), \
                patch.object(replica, 'recovery_source', return_value='pool/vm-100-disk-1') as source, \
                patch.object(worker, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(state, 'unpack_state', side_effect=ValueError('corrupt archive')):
            destination = Path(temporary) / 'out'
            snap = '__replicate_100-0_123__'
            with self.assertRaisesRegex(ValueError, 'corrupt archive'):
                replica.export('zfs:vm-100-disk-1', destination, snap, '456')
            self.assertFalse(destination.exists())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0][:2], ['zfs', 'clone'])
            self.assertIn('pool/vm-100-disk-1@' + snap, commands[0])
            self.assertEqual(commands[-1][:2], ['zfs', 'destroy'])
            self.assertEqual(list(Path(temporary).glob('*.json')), [])
            self.assertEqual(source.call_count, 2)
