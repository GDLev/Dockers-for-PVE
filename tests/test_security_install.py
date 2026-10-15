"""Missing recovery keys must not be replaced while journals retain data."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import security_install


class RecoveryKeyInstallation(unittest.TestCase):
    def test_pending_operation_requires_original_key_without_changing_files(self):
        for relative in ('projects/' + 'a' * 32 + '/network-transaction.json', 'imports/' + 'a' * 32 + '.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                journal = root / relative
                journal.parent.mkdir(parents=True)
                journal.write_text('{"pending": true}')
                key = root / 'recovery.key'
                account = SimpleNamespace(pw_name='betterdocker-parser', pw_uid=1001, pw_dir='/nonexistent', pw_shell='/usr/sbin/nologin')
                def path(value):
                    return root if value == '/var/lib/betterdocker' else Path(value)
                with patch.object(security_install.os, 'geteuid', return_value=0), patch.object(security_install.pwd, 'getpwnam', return_value=account), patch.object(security_install.subprocess, 'check_output', return_value='betterdocker-parser\n'), patch.object(security_install, 'Path', side_effect=path), patch.object(security_install, 'KEY', key), patch.object(security_install, 'atomic_text') as write:
                    with self.assertRaisesRegex(ValueError, 'Restore the original key from backup'):
                        security_install.initialize('rootless', '')
                write.assert_not_called()
                self.assertFalse(key.exists())
                self.assertEqual(journal.read_text(), '{"pending": true}')


if __name__ == '__main__':
    unittest.main()
