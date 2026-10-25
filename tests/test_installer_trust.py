"""First-run trust setup executes the real standalone installer code."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TRUST = (ROOT / 'install.sh').read_text().split("<<'TRUST'\n", 1)[1].split('\nTRUST\n', 1)[0]


@unittest.skipUnless(os.geteuid() == 0 and sys.platform == 'linux', 'Requires disposable Linux root filesystem')
class InstallerTrust(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bd-trust-', dir='/run')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'trust'
        self.key = self.directory / 'release-key.pem'
        self.pin = self.directory / 'release-key.sha256'

    def command(self):
        return [sys.executable, '-c', 'import os; os.umask(0o077)\n' + TRUST, str(self.key), str(self.pin)]

    def run_setup(self, succeeds=True):
        result = subprocess.run(self.command(), capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def test_first_run_configures_pinned_key_and_repeat_preserves_files(self):
        self.run_setup()
        self.assertEqual(self.key.read_bytes(), (ROOT / 'packaging/release-key.pem').read_bytes())
        public = subprocess.check_output(['openssl', 'pkey', '-pubin', '-in', str(self.key), '-outform', 'DER'])
        self.assertEqual(self.pin.read_text().strip(), hashlib.sha256(public).hexdigest())
        before = [(p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in (self.key, self.pin)]
        self.run_setup()
        self.assertEqual(before, [(p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in (self.key, self.pin)])
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.directory.stat().st_mode & 0o022, 0)

    def test_wrong_fingerprint_is_rejected_without_replacing_key_or_pin(self):
        self.run_setup()
        self.pin.write_text('0' * 64 + '\n')
        before = self.key.read_bytes(), self.pin.read_bytes()
        result = self.run_setup(False)
        self.assertIn('fingerprint mismatch', result.stderr)
        self.assertEqual(before, (self.key.read_bytes(), self.pin.read_bytes()))

    def test_partial_setup_has_actionable_error_and_is_not_overwritten(self):
        self.directory.mkdir()
        self.key.write_text('existing trust must not be replaced')
        result = self.run_setup(False)
        self.assertIn('setup is incomplete', result.stderr)
        self.assertFalse(self.pin.exists())
        self.assertEqual(self.key.read_text(), 'existing trust must not be replaced')

    def test_invalid_existing_public_key_is_not_overwritten(self):
        self.directory.mkdir()
        self.key.write_text('invalid key'); self.pin.write_text('0' * 64)
        result = self.run_setup(False)
        self.assertIn('fingerprint mismatch', result.stderr)
        self.assertEqual(self.key.read_text(), 'invalid key')

    def test_unsafe_directory_and_symlink_are_rejected(self):
        self.directory.mkdir(); self.directory.chmod(0o777)
        self.run_setup(False)
        self.assertFalse(self.key.exists())
        self.directory.chmod(0o755)
        outside = self.root / 'outside'; outside.write_text('untouched')
        self.key.symlink_to(outside)
        self.run_setup(False)
        self.assertEqual(outside.read_text(), 'untouched')
        self.assertFalse(self.pin.exists())

    def test_concurrent_setup_converges_without_overwriting_trust(self):
        first = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for process in (first, second):
            out, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, out + error)
        self.assertEqual(self.key.read_bytes(), (ROOT / 'packaging/release-key.pem').read_bytes())
        self.run_setup()
