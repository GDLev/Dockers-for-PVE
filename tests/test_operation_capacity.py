"""Small child processes exercise admission locks in a private temporary tree."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dctlib import operation_capacity as c
from test_worker import worker as w

CHILD = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from dctlib import operation_capacity as c
c.ROOT=Path(sys.argv[2]);c.POLICY=Path(sys.argv[3])
try:
 with c.reserve():
  print('admitted',flush=True)
  sys.stdin.readline()
except ValueError as error:
 print(str(error),flush=True)
 sys.exit(3)
'''


class OperationCapacity(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(c, 'ROOT', self.base / 'slots'))
        self.stack.enter_context(patch.object(c, 'POLICY', self.base / 'policy.json'))
        self.stack.enter_context(patch.dict(os.environ))
        os.environ.pop(c.ENV, None)
        self.children = []; self.addCleanup(self.close_children)

    def child(self, inherited=None):
        env = dict(os.environ)
        if inherited is not None: env[c.ENV] = str(inherited)
        command = [sys.executable, '-c', CHILD, str(Path(c.__file__).resolve().parents[1]), str(c.ROOT), str(c.POLICY)]
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=env, pass_fds=() if inherited is None else (inherited,))
        self.children.append(child)
        return child

    def finish(self, child):
        out, err = child.communicate('\n', timeout=10)
        self.assertEqual(child.returncode, 0, err)

    def close_children(self):
        for child in self.children:
            if child.poll() is None: child.communicate('\n', timeout=10)

    def policy(self, maximum):
        c.POLICY.write_text(json.dumps({'max_concurrent': maximum}))

    def test_two_independent_workers_and_atomic_refusal(self):
        first, second = self.child(), self.child()
        ready = first.stdout.readline().strip()
        self.assertEqual(ready, 'admitted', first.stderr.read() if not ready else '')
        self.assertEqual(second.stdout.readline().strip(), 'admitted')
        third = self.child(); out, _ = third.communicate(timeout=10)
        self.assertEqual(third.returncode, 3)
        self.assertIn('limit reached', out)
        with self.assertRaisesRegex(ValueError, 'limit reached'):
            with c.reserve(): pass
        self.finish(first)
        with c.reserve(): self.assertIsNotNone(c.descriptor())
        self.finish(second)

    def test_nested_calls_and_inherited_helpers_share_one_slot(self):
        self.policy(1)
        with c.reserve():
            original = c.descriptor()
            with c.reserve(): self.assertEqual(c.descriptor(), original)
            child = self.child(original)
            self.assertEqual(child.stdout.readline().strip(), 'admitted')
            self.finish(child)
        self.assertIsNone(c.descriptor())
        with c.reserve(): pass

    def test_child_keeps_slot_until_it_exits(self):
        self.policy(1)
        with c.reserve():
            child = self.child(c.descriptor())
            self.assertEqual(child.stdout.readline().strip(), 'admitted')
        with self.assertRaisesRegex(ValueError, 'limit reached'):
            with c.reserve(): pass
        self.finish(child)
        with c.reserve(): pass

    def test_exception_releases_reservation(self):
        self.policy(1)
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with c.reserve(): raise RuntimeError('fixture')
        with c.reserve(): pass

    def test_reduced_policy_counts_existing_slots(self):
        first = self.child(); self.assertEqual(first.stdout.readline().strip(), 'admitted')
        second = self.child(); self.assertEqual(second.stdout.readline().strip(), 'admitted')
        self.policy(1); self.finish(first)
        with self.assertRaisesRegex(ValueError, 'limit reached'):
            with c.reserve(): pass
        self.finish(second)
        with c.reserve(): pass

    def test_invalid_policy_fails_before_work(self):
        for value in (0, 65, True, 1.5):
            self.policy(value)
            with self.assertRaisesRegex(ValueError, 'max_concurrent'):
                with c.reserve(): self.fail('Invalid policy admitted work')

    def test_command_scope_keeps_stop_and_status_available(self):
        self.policy(1)
        holder = self.child(); self.assertEqual(holder.stdout.readline().strip(), 'admitted')
        calls = []
        command = c.bounded_command(lambda args: calls.append(args))
        command(['docker', 'stop', 'fixture'])
        command(['docker', 'inspect', 'fixture'])
        with self.assertRaisesRegex(ValueError, 'limit reached'): command(['docker', 'pull', 'nginx'])
        self.assertEqual(len(calls), 2)
        self.finish(holder)

    def test_worker_passes_slot_to_local_helper(self):
        with c.reserve(), patch.object(w.node_runtime, 'run') as run:
            w.run(['/usr/local/lib/betterdocker/guest_state.py', 'pack', 'fixture'])
            self.assertIn(c.descriptor(), run.call_args.kwargs['pass_fds'])
            self.assertEqual(run.call_args.kwargs['env'][c.ENV], str(c.descriptor()))

    def test_bounded_function_refuses_before_any_body_work(self):
        self.policy(1)
        holder = self.child(); self.assertEqual(holder.stdout.readline().strip(), 'admitted')
        calls = []
        function = c.bounded(lambda: calls.append('work'))
        with self.assertRaisesRegex(ValueError, 'limit reached'): function()
        self.assertEqual(calls, [])
        self.finish(holder)

    def test_streamed_local_helper_inherits_reservation(self):
        from dctlib.transfer_io import send
        self.policy(1)
        source = self.base / 'stream'; source.write_text('\n')
        code = CHILD.replace("print('admitted',flush=True)", 'pass')
        command = [sys.executable, '-c', code, str(Path(c.__file__).resolve().parents[1]), str(c.ROOT), str(c.POLICY)]
        for rate in (0, 1024):
            with source.open('rb') as stream:
                send(command, stream, rate=rate, timeout=10)
