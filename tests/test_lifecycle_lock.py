"""Concurrent recovery callers share the real node configuration lock."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux', 'Linux process descriptors and flock')
class ResumeLockTests(unittest.TestCase):
    def test_journal_consumed_while_waiting_for_lock_is_already_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'projects').mkdir()
            (base / 'metadata').mkdir()
            journal = base / 'lifecycle.json'
            journal.write_text(json.dumps({'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'containers': []}))
            lock = base / 'node.lock'
            code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dctlib import projects as w
base = Path(sys.argv[2])
w.ROOT = base / 'projects'
w.METADATA = base / 'metadata'
w.LOCK = base / 'node.lock'
w.daemon_resume('fixture')
'''
            with lock.open('w') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                child = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), folder],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        self.assertIsNone(child.poll(), 'Recovery exited before acquiring its node lock')
                        descriptors = Path('/proc') / str(child.pid) / 'fd'
                        waiting = False
                        for fd in descriptors.iterdir():
                            try:
                                waiting |= os.readlink(fd) == str(lock)
                            except FileNotFoundError:
                                continue
                        if waiting:
                            break
                        time.sleep(.01)
                    else:
                        self.fail('Recovery did not reach the node lock')
                    journal.unlink()
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    _, error = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, error)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)
