"""Public cluster metadata uses IPC; private control files keep anchored reads."""
import errno
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dctlib import secure_io, process


class ClusterMetadata(unittest.TestCase):
    def test_public_metadata_never_opens_a_fuse_handle(self):
        for name in ('a' * 32 + '.json', 'ids/100.json', 'nodes/pve.json'):
            with self.subTest(name=name):
                value = {'id': 'a' * 32, 'node': 'pve', 'vmid': 100}
                result = subprocess.CompletedProcess([], 0, json.dumps(value), '')
                with patch.object(secure_io, 'anchored') as anchored, \
                        patch.object(process, 'run', return_value=result) as run:
                    self.assertEqual(secure_io.read_json('/etc/pve/betterdocker/' + name), value)
                anchored.assert_not_called()
                self.assertEqual(run.call_args.args[0][-1], 'betterdocker/' + name)
                self.assertEqual(run.call_args.kwargs['output_limit'], secure_io.MAX_JSON)
                self.assertLessEqual(run.call_args.kwargs['timeout'], 10)

    def test_private_and_unknown_paths_never_use_ipc(self):
        for path in ('/var/lib/betterdocker/request.json', '/etc/pve/priv/authkey.key',
                     '/etc/pve/betterdocker/request.json', '/etc/pve/betterdocker/ids/1.json',
                     '/etc/pve/betterdocker/ids/100.json/extra'):
            with self.subTest(path=path), \
                    patch.object(secure_io, 'anchored', side_effect=FileNotFoundError()), \
                    patch.object(process, 'run') as run:
                with self.assertRaises(FileNotFoundError):
                    secure_io.read_json(path)
                run.assert_not_called()

    def test_traversal_and_private_symlinks_remain_rejected(self):
        with patch.object(process, 'run') as run:
            with self.assertRaises(ValueError):
                secure_io.read_json('/etc/pve/betterdocker/../priv/key.json')
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / 'target.json'
                target.write_text('{}')
                link = Path(folder) / 'request.json'
                link.symlink_to(target)
                with self.assertRaises(OSError):
                    secure_io.read_json(link)
            run.assert_not_called()

    def test_private_permission_denial_is_not_reported_as_absence(self):
        with patch.object(secure_io, 'anchored', side_effect=PermissionError()), \
                patch.object(process, 'run') as run:
            with self.assertRaises(PermissionError):
                secure_io.read_json('/var/lib/betterdocker/request.json')
            run.assert_not_called()

    def test_real_absence_remains_a_missing_file(self):
        path = '/etc/pve/betterdocker/' + 'a' * 32 + '.json'
        with patch.object(process, 'run', return_value=subprocess.CompletedProcess([], 44, '', '')):
            with self.assertRaises(FileNotFoundError) as caught:
                secure_io.read_json(path)
        self.assertEqual(caught.exception.errno, errno.ENOENT)
        self.assertEqual(caught.exception.filename, path)

    def test_ipc_failure_and_invalid_metadata_fail_closed(self):
        for code, content, error in ((1, '', subprocess.CalledProcessError),
                                     (0, '{invalid}', ValueError), (0, '{"cpu": NaN}', ValueError)):
            with self.subTest(code=code, content=content), \
                    patch.object(process, 'run', return_value=subprocess.CompletedProcess([], code, content, '')):
                with self.assertRaises(error):
                    secure_io.read_json('/etc/pve/betterdocker/' + 'a' * 32 + '.json')

    def test_text_reads_keep_the_callers_size_limit(self):
        with patch.object(process, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            self.assertEqual(secure_io.read_text('/etc/pve/betterdocker/ids/100.json', maximum=32), '{}')
        self.assertEqual(run.call_args.kwargs['output_limit'], 32)
