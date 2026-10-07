"""Exercise cancellation through real flock handoff without sending host signals."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_worker import worker

ID = 'd' * 32


class ShutdownCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.proc = self.base / 'proc'
        pid = self.proc / str(os.getpid())
        pid.mkdir(parents=True)
        (pid / 'stat').write_text('1 (worker) ' + ' '.join(['0'] * 19 + ['123']))
        self.root_patch = patch.object(worker, 'ROOT', self.base / 'projects')
        self.proc_patch = patch.object(worker, 'PROC', self.proc)
        self.env_patch = patch.dict(os.environ, BETTERDOCKER_TASK_USER='alice@pve', BETTERDOCKER_TASK_SYS_MODIFY='0')
        self.root_patch.start(); self.proc_patch.start(); self.env_patch.start()
        self.threads = []

    def tearDown(self):
        for thread, release in self.threads:
            release.set()
            thread.join(3)
        self.env_patch.stop(); self.proc_patch.stop(); self.root_patch.stop()
        self.tmp.cleanup()

    def running_operation(self, name='shutdown', service=None):
        ready, release = threading.Event(), threading.Event()
        errors = []
        def run():
            try:
                with worker.operation(ID, name, service=service):
                    ready.set()
                    while not release.wait(.01):
                        if name == 'shutdown':
                            worker.check_shutdown_cancellation(ID)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        self.threads.append((thread, release))
        thread.start()
        self.assertTrue(ready.wait(3))
        return thread, release, errors

    def test_owner_cancels_shutdown_and_takes_lock_after_it_exits(self):
        thread, _, errors = self.running_operation()
        with worker.operation(ID, 'force-stop', overrule=True):
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(str(errors[0]), 'Shutdown interrupted by Stop.')
            self.assertEqual(worker.operation_state(ID)['action'], 'force-stop')
        self.assertFalse(worker.operation_state(ID)['active'])

    def test_other_user_cannot_cancel_or_write_a_request(self):
        self.running_operation()
        with patch.dict(os.environ, BETTERDOCKER_TASK_USER='bob@pve'):
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                with worker.operation(ID, 'force-stop', overrule=True):
                    self.fail('unauthorized lock acquired')
        self.assertFalse((self.base / 'operations' / (ID + '.cancel.json')).exists())
        self.assertTrue(worker.operation_state(ID)['active'])

    def test_sys_modify_can_cancel_another_users_shutdown(self):
        thread, _, errors = self.running_operation()
        with patch.dict(os.environ, BETTERDOCKER_TASK_USER='bob@pve', BETTERDOCKER_TASK_SYS_MODIFY='1'):
            with worker.operation(ID, 'force-stop', overrule=True):
                thread.join(1)
                self.assertIn('interrupted', str(errors[0]))

    def test_own_api_token_can_cancel_users_shutdown(self):
        self.running_operation()
        with patch.dict(os.environ, BETTERDOCKER_TASK_USER='alice@pve!automation'):
            with worker.operation(ID, 'force-stop', overrule=True):
                pass

    def test_stop_without_overrule_does_not_cancel(self):
        self.running_operation()
        with self.assertRaisesRegex(ValueError, 'Another operation'):
            with worker.operation(ID, 'force-stop'):
                self.fail('lock acquired without overrule')
        self.assertTrue(worker.operation_state(ID)['active'])

    def test_unrelated_operation_cannot_be_overruled(self):
        self.running_operation('settings')
        with self.assertRaisesRegex(ValueError, 'matching active Shutdown'):
            with worker.operation(ID, 'force-stop', overrule=True):
                self.fail('settings operation cancelled')

    def test_service_shutdown_requires_matching_scope(self):
        self.running_operation(service='web')
        for service in (None, 'redis'):
            with self.assertRaisesRegex(ValueError, 'matching active Shutdown'):
                with worker.operation(ID, 'force-stop', overrule=True, service=service):
                    self.fail('another scope acquired')
        with worker.operation(ID, 'force-stop', overrule=True, service='web'):
            pass

    def test_stale_cancellation_does_not_cancel_new_operation(self):
        with worker.operation(ID, 'shutdown'):
            cancel = self.base / 'operations' / (ID + '.cancel.json')
            worker.write_json(cancel, {'pid': os.getpid(), 'process_start': 'stale'})
            worker.check_shutdown_cancellation(ID)
        with worker.operation(ID, 'shutdown'):
            self.assertFalse(cancel.exists())
            worker.check_shutdown_cancellation(ID)
