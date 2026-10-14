"""Independent guests proceed while the same guest serializes; dead workers release locks."""
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker

ROOT = Path(__file__).resolve().parents[1]
ID = 'a' * 32
OTHER = 'b' * 32
CODE = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dctlib import projects as worker
worker.ROOT = Path(sys.argv[2]) / 'projects'
worker.LOCK = Path(sys.argv[2]) / 'node.lock'
with worker.locked(sys.argv[3] if sys.argv[3] != 'node' else None):
 print('acquired', flush=True)
 sys.stdin.read()
'''


@unittest.skipUnless(os.name == 'posix', 'requires POSIX flock')
class GuestLockTests(unittest.TestCase):
    def child(self, directory, identifier):
        process = subprocess.Popen([sys.executable, '-c', CODE, str(ROOT / 'scripts'), directory, identifier],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()
        self.addCleanup(cleanup)
        return process

    def acquired(self, process, timeout):
        if not select.select([process.stdout], [], [], timeout)[0]: return False
        line = process.stdout.readline()
        self.assertEqual(line.strip(), 'acquired', process.stderr.read() if process.poll() is not None else '')
        return True

    def test_same_guest_waits_other_guest_proceeds_and_crash_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.child(directory, ID)
            self.assertTrue(self.acquired(first, 5))
            same = self.child(directory, ID)
            other = self.child(directory, OTHER)
            self.assertTrue(self.acquired(other, 5))
            self.assertFalse(self.acquired(same, .2))
            first.kill(); first.wait(timeout=5)
            self.assertTrue(self.acquired(same, 5))

    def test_shared_node_lock_still_serializes_shared_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.child(directory, 'node')
            self.assertTrue(self.acquired(first, 5))
            second = self.child(directory, 'node')
            self.assertFalse(self.acquired(second, .2))
            first.stdin.close(); first.wait(timeout=5)
            self.assertTrue(self.acquired(second, 5))

    def test_shared_lock_cannot_follow_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'important'; target.write_text('keep')
            lock = Path(directory) / 'node.lock'; lock.symlink_to(target)
            with patch.object(worker, 'LOCK', lock), self.assertRaises(OSError):
                with worker.locked(): pass
            self.assertEqual(target.read_text(), 'keep')

    def test_reentrant_guest_to_node_order_and_exception_release(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'ROOT', Path(directory) / 'projects'), \
             patch.object(worker, 'LOCK', Path(directory) / 'node.lock'):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                with worker.locked(ID), worker.locked(ID), worker.locked():
                    raise RuntimeError('cancelled')
            with worker.locked(ID), worker.locked(): pass
            with worker.locked(), self.assertRaisesRegex(ValueError, 'guest lock'):
                with worker.locked(ID): pass
