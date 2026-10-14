"""Guest hooks run at lifecycle boundaries, never for an idempotent power request."""
import json
import os
import unittest
from unittest.mock import patch
import test_delegation as delegation

ID, worker = delegation.ID, delegation.worker


class HookTests(unittest.TestCase):
    setUp = delegation.DelegationTests.setUp
    tearDown = delegation.DelegationTests.tearDown

    def configure(self):
        info = json.loads((self.metadata / (ID + '.json')).read_text())
        info.update(vmid=150, expected=1, hookscript='local:snippets/test.pl')
        worker.write_json(self.metadata / (ID + '.json'), info)
        return info

    def test_hook_changes_are_root_only_and_change_digest(self):
        before = worker.detail(ID, 'pve')['digest']
        with patch.dict(os.environ, BETTERDOCKER_TASK_USER='owner@pve', BETTERDOCKER_CONFIG_KEYS='*'):
            with self.assertRaisesRegex(ValueError, 'root@pam'):
                worker.project_config(ID, 'pve', {'hookscript': 'local:snippets/test.pl'})
            with self.assertRaisesRegex(ValueError, 'root@pam'):
                worker.project_config(ID, 'pve', {'hookscript': ''})
        with patch.object(worker, 'pve_hook'):
            worker.project_config(ID, 'pve', {'hookscript': 'local:snippets/test.pl'}, before)
        self.assertNotEqual(worker.detail(ID, 'pve')['digest'], before)
        worker.project_config(ID, 'pve', {'hookscript': ''})
        self.assertEqual(worker.detail(ID, 'pve')['digest'], before)

    def test_pre_start_failure_aborts_and_allows_explicit_retry(self):
        info = self.configure()
        with patch.object(worker, 'pve_hook', side_effect=ValueError('failed')):
            with self.assertRaisesRegex(ValueError, 'failed'):
                worker.guest_hook(info, 'pre-start')
        self.assertFalse(worker.hook_journal(ID).exists())
        self.assertEqual(json.loads((self.metadata / (ID + '.json')).read_text())['status'], 'stopped')
        with patch.object(worker, 'pve_hook') as hook:
            worker.guest_hook(info, 'pre-start')
        hook.assert_called_once_with('local:snippets/test.pl', 150, 'pre-start')

    def test_other_hook_failures_warn_and_do_not_abort(self):
        info = self.configure()
        with patch.object(worker, 'pve_hook', side_effect=ValueError('failed')):
            for phase in ('post-start', 'pre-stop', 'post-stop'):
                worker.guest_hook(info, phase)
        self.assertFalse(worker.hook_journal(ID).exists())

    def test_interrupted_execution_is_not_repeated(self):
        info = self.configure()
        worker.write_json(worker.hook_journal(ID), {'phase': 'pre-start', 'executing': True})
        with patch.object(worker, 'pve_hook') as hook:
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                worker.recover_guest_hook(info)
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                worker.guest_hook(info, 'pre-start')
        hook.assert_not_called()
        with patch.object(worker, 'pve_hook'):
            worker.project_config(ID, 'pve', {'hookscript': info['hookscript']})
        self.assertFalse(worker.hook_journal(ID).exists())

    def test_recovery_finishes_post_phase_once_when_runtime_changed(self):
        info = self.configure()
        for phase, records in [('post-start', [{'state': 'running'}]), ('post-stop', [])]:
            worker.expect_guest_hook(info, phase)
            with patch.object(worker, 'container_records', return_value=records), patch.object(worker, 'pve_hook') as hook:
                worker.recover_guest_hook(info)
                worker.recover_guest_hook(info)
            hook.assert_called_once_with(info['hookscript'], info['vmid'], phase)

    def test_repeated_start_has_no_hook_or_runtime_mutation(self):
        self.configure()
        with patch.object(worker, 'recover_network_transaction'), patch.object(worker, 'recover_storage_transaction'), \
             patch.object(worker, 'recover_snapshot_transaction'), patch.object(worker, 'pve_hook') as hook, \
             patch.object(worker, 'container_records', return_value=[{'id': 'a' * 64, 'state': 'running'}]), \
             patch.object(worker, 'run') as run:
            worker.action(ID, 'pve', 'start')
        hook.assert_not_called()
        run.assert_not_called()
