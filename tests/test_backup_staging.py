"""Restore admission and cleanup with disposable files, without mounting or PBS."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import backup_restore as b, operation_capacity as admission, retention, space
import guest_state

ID = 'a' * 32


class BackupStaging(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for mod, name, value in ((b, 'ROOT', self.base / 'journals'), (b, 'TEMP', self.base / 'tmp'),
                                 (w, 'ROOT', self.base / 'projects'),
                                 (admission, 'ROOT', self.base / 'slots'), (admission, 'POLICY', self.base / 'operations.json'),
                                 (space, 'ROOT', self.base / 'space'), (space, 'POLICY', self.base / 'space.json'),
                                 (retention, 'ROOT', self.base / 'retention'), (retention, 'POLICY', self.base / 'retention.json')):
            self.stack.enter_context(patch.object(mod, name, value))
        b.TEMP.mkdir()
        space.POLICY.write_text(json.dumps({'reserve_bytes': 5, 'overhead_percent': 10}))
        self.stack.enter_context(patch.object(w, 'current_node', return_value='pve'))
        self.stack.enter_context(patch.object(b, 'capacity', return_value=16 * b.MiB))
        self.options = self.base / 'options.json'; self.options.write_text('{}')

    def test_operation_limit_refuses_before_transport_or_staging(self):
        with patch.object(admission, 'reserve', side_effect=ValueError('Operation limit')), patch.object(w, 'capture') as source, patch.object(b, 'staging') as staging:
            with self.assertRaisesRegex(ValueError, 'Operation limit'):
                b.restore('pbs:backup', ID, 'pve', str(self.options))
            source.assert_not_called(); staging.assert_not_called()

    def test_space_limit_refuses_before_download_and_cleans_journal(self):
        with patch.object(space, 'reserve', side_effect=ValueError('Insufficient peak')), patch.object(w, 'capture') as source, patch.object(w, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Insufficient peak'):
                b.restore('pbs:backup', ID, 'pve', str(self.options))
            source.assert_not_called(); run.assert_not_called()
        self.assertEqual(list(b.TEMP.iterdir()), [])
        self.assertEqual(list(b.ROOT.glob('*.json')), [])

    def test_admission_and_physical_allocation_precede_pbs_restore(self):
        calls = []
        slot = None
        def run(args, **kwargs):
            nonlocal slot
            self.assertIsNotNone(admission.descriptor())
            slot = slot or admission.descriptor(); self.assertEqual(admission.descriptor(), slot)
            calls.append(args[0])
            if args[0] == 'fallocate':
                self.assertTrue(list(space.ROOT.glob('*.json')))
                self.assertTrue(list(retention.ROOT.glob('*.json')))
                Path(args[-1]).write_bytes(b'x' * (16 * b.MiB))
            if args[0] == 'perl':
                self.assertIn('mount', calls)
                self.assertEqual(list(space.ROOT.glob('*.json')), [])
                Path(args[-1]).mkdir()
            return subprocess.CompletedProcess(args, 0, '', '')
        def restore(folder, *args):
            self.assertEqual(admission.descriptor(), slot)
            self.assertTrue(folder.is_dir())
        with patch.object(w, 'capture', return_value='{"type":"pbs"}'), patch.object(w, 'run', side_effect=run), patch.object(guest_state, 'restore_state', side_effect=restore):
            b.restore('pbs:backup', ID, 'pve', str(self.options))
        self.assertEqual(calls[:3], ['fallocate', 'mkfs.ext4', 'mount'])
        self.assertEqual(list(b.TEMP.iterdir()), [])

    def test_download_failure_never_imports_and_removes_owned_staging(self):
        def run(args, **kwargs):
            if args[0] == 'fallocate':
                Path(args[-1]).write_bytes(b'x' * (16 * b.MiB))
            if args[0] == 'perl':
                raise subprocess.CalledProcessError(1, args, stderr='No space left on bounded staging')
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(w, 'capture', return_value='{"type":"pbs"}'), patch.object(w, 'run', side_effect=run), patch.object(guest_state, 'restore_state') as restore:
            with self.assertRaises(subprocess.CalledProcessError):
                b.restore('pbs:backup', ID, 'pve', str(self.options))
            restore.assert_not_called()
        self.assertEqual(list(b.TEMP.iterdir()), [])
        self.assertEqual(list(b.ROOT.glob('*.json')), [])

    def test_local_decompressor_writes_only_to_bounded_filesystem(self):
        archive = self.base / 'backup.gz'; archive.write_bytes(b'fixture')
        source = json.dumps({'type': 'file', 'path': str(archive), 'compression': 'gz'})
        def run(args, **kwargs):
            if args[0] == 'fallocate':
                Path(args[-1]).write_bytes(b'x' * (16 * b.MiB))
            if args[0] == 'gzip':
                path = Path(kwargs['stdout'].name)
                self.assertEqual(path.parent.name, 'filesystem')
                self.assertTrue(path.is_relative_to(b.TEMP))
                kwargs['stdout'].write(b'ordinary fixture')
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(w, 'capture', return_value=source), patch.object(w, 'run', side_effect=run), patch.object(guest_state, 'unpack_state') as unpack, patch.object(guest_state, 'restore_state'):
            b.restore('local:backup', ID, 'pve', str(self.options))
            self.assertEqual(unpack.call_count, 1)
            self.assertEqual(unpack.call_args.args[0].name, 'archive.tar')
        self.assertEqual(list(b.TEMP.iterdir()), [])

    def test_cleanup_preserves_a_directory_with_changed_identity(self):
        parent = b.TEMP / ('betterdocker-restore-' + ID); parent.mkdir()
        marker = parent / 'keep'; marker.write_text('owned by someone else')
        with self.assertRaisesRegex(ValueError, 'ownership changed'):
            b.cleanup({'token': ID, 'directory': [-1, -1]})
        self.assertTrue(marker.exists())

    def test_sweep_preserves_live_staging_and_cleans_abandoned_receipt(self):
        b.ROOT.mkdir(); parent = b.TEMP / ('betterdocker-restore-' + ID); parent.mkdir()
        info = parent.stat(); receipt = b.ROOT / (ID + '.json')
        receipt.write_text(json.dumps({'token': ID, 'directory': [info.st_dev, info.st_ino], 'owner': {}}))
        with patch.object(retention, 'active', return_value=True):
            self.assertEqual(b.sweep(), 0); self.assertTrue(parent.exists())
        with patch.object(retention, 'active', return_value=False):
            self.assertEqual(b.sweep(), 1)
        self.assertFalse(parent.exists()); self.assertFalse(receipt.exists())

    def test_api_restore_uses_supervisor_before_native_transport(self):
        source = (Path(__file__).resolve().parents[1] / 'backend/PVE/API2/BetterDocker.pm').read_text()
        body = source.split('sub restore_backup {', 1)[1].split('sub console_helper_command', 1)[0]
        self.assertIn("guest_state.py', 'restore-backup'", body)
        self.assertNotIn("backup.pl', 'restore'", body)
