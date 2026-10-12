"""Guest power semantics: graceful shutdown never escalates to SIGKILL."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_worker import worker

ID = 'a' * 32
RUNNING = {'id': 'b' * 64, 'service': 'app', 'state': 'running', 'stop_signal': 'SIGQUIT'}
STOPPED = dict(RUNNING, state='exited')


class PowerTests(unittest.TestCase):
    def test_shutdown_honors_image_stop_signal(self):
        with patch.object(worker, 'container_records', side_effect=[[RUNNING], [STOPPED]]), \
             patch.object(worker, 'run') as run:
            self.assertEqual(worker.graceful_shutdown(ID), [RUNNING['id']])
            self.assertEqual(run.call_args.args[0], ['docker', 'kill', '--signal', 'SIGQUIT', RUNNING['id']])

    def test_shutdown_timeout_never_kills_or_restarts(self):
        with patch.object(worker, 'container_records', return_value=[RUNNING]), \
             patch.object(worker.time, 'monotonic', side_effect=[10, 11]), patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'remains running'):
                worker.graceful_shutdown(ID, 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][2:4], ['--signal', 'SIGQUIT'])

    def test_shutdown_is_idempotent_when_already_stopped(self):
        with patch.object(worker, 'container_records', return_value=[STOPPED]), patch.object(worker, 'run') as run:
            worker.graceful_shutdown(ID)
            run.assert_not_called()

    def test_shutdown_tolerates_container_exiting_before_signal(self):
        with patch.object(worker, 'container_records', side_effect=[[RUNNING], [STOPPED], [STOPPED]]), \
             patch.object(worker, 'run', side_effect=subprocess.CalledProcessError(1, ['docker', 'kill'])):
            worker.graceful_shutdown(ID)

    def test_shutdown_reports_real_signal_failure(self):
        with patch.object(worker, 'container_records', return_value=[RUNNING]), \
             patch.object(worker, 'run', side_effect=subprocess.CalledProcessError(1, ['docker', 'kill'])):
            with self.assertRaises(subprocess.CalledProcessError):
                worker.graceful_shutdown(ID)

    def test_force_stop_targets_only_active_owned_containers(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={'id': ID}), \
             patch.object(worker, 'container_records', return_value=[RUNNING, dict(STOPPED, id='c' * 64)]), \
             patch.object(worker, 'refresh'), patch.object(worker, 'write_json'), patch.object(worker, 'run') as run:
            worker.action(ID, 'pve', 'force-stop')
            self.assertEqual(run.call_args.args[0], ['docker', 'kill', RUNNING['id']])

    def test_reboot_restarts_existing_ids_without_recreation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={'id': ID, 'address': '192.0.2.1'}), \
             patch.object(worker, 'container_records', return_value=[RUNNING]) as records, \
             patch.object(worker, 'graceful_shutdown', return_value=[RUNNING['id']]) as shutdown, \
             patch.object(worker, 'ensure_address'), patch.object(worker, 'rebind_runtime'), \
             patch.object(worker, 'refresh'), patch.object(worker, 'write_json'), patch.object(worker, 'run') as run:
            directory = Path(folder) / ID
            directory.mkdir()
            (directory / 'compose.runtime.json').write_text('{"services":{"app":{}}}')
            shutdown.side_effect = lambda *_: setattr(records, 'return_value', [STOPPED]) or [RUNNING['id']]
            worker.action(ID, 'pve', 'reboot', 20)
            shutdown.assert_called_once_with(ID, 20)
            self.assertEqual(run.call_args.args[0], ['docker', 'start', RUNNING['id']])

    def test_reboot_rejects_stopped_guest(self):
        with patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={}), \
             patch.object(worker, 'container_records', return_value=[STOPPED]), patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'not running'):
                worker.action(ID, 'pve', 'reboot')
            run.assert_not_called()

    def test_force_stop_is_idempotent_when_stopped(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={'id': ID}), \
             patch.object(worker, 'container_records', return_value=[STOPPED]), \
             patch.object(worker, 'refresh'), patch.object(worker, 'write_json'), patch.object(worker, 'run') as run:
            worker.action(ID, 'pve', 'force-stop')
            run.assert_not_called()

    def test_legacy_stop_retains_compose_semantics(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={'id': ID}), \
             patch.object(worker, 'refresh'), patch.object(worker, 'write_json'), patch.object(worker, 'run') as run:
            worker.action(ID, 'pve', 'stop')
            self.assertEqual(run.call_args.args[0][-1], 'stop')

    def test_remove_rejects_running_paused_and_restarting_guests(self):
        with patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={}), \
             patch.object(worker, 'run') as run:
            for state in ('running', 'paused', 'restarting'):
                with self.subTest(state=state), patch.object(worker, 'container_records', return_value=[dict(RUNNING, state=state)]):
                    with self.assertRaisesRegex(ValueError, 'Stop it before removal'):
                        worker.action(ID, 'pve', 'delete')
            run.assert_not_called()

    def test_service_shutdown_does_not_signal_other_services(self):
        app = dict(RUNNING, service='app')
        other = dict(RUNNING, id='c' * 64, service='other')
        with patch.object(worker, 'container_records', side_effect=[[app, other], [dict(app, state='exited'), other]]), \
             patch.object(worker, 'run') as run:
            worker.graceful_shutdown(ID, 60, 'app')
            self.assertEqual(run.call_args.args[0][-1], app['id'])
            self.assertEqual(run.call_count, 1)

    def test_service_force_stop_is_scoped_to_selected_service(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / ID
            directory.mkdir()
            (directory / 'compose.runtime.json').write_text('{"services":{"app":{},"other":{}}}')
            containers = [dict(RUNNING, service='app'), dict(RUNNING, id='c' * 64, service='other')]
            with patch.object(worker, 'ROOT', Path(folder)), patch.object(worker, 'locked'), \
                 patch.object(worker, 'detail', return_value={}), patch.object(worker, 'container_records', return_value=containers), \
                 patch.object(worker, 'refresh'), patch.object(worker, 'run') as run:
                worker.service_action(ID, 'pve', 'app', 'force-stop')
                self.assertEqual(run.call_args.args[0], ['docker', 'kill', RUNNING['id']])

    def test_start_keeps_existing_container_after_live_resource_changes(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={'id': ID, 'expected': 1, 'address': '10.0.0.2'}), \
             patch.object(worker, 'ensure_address'), patch.object(worker, 'rebind_runtime'), \
             patch.object(worker, 'write_json'), patch.object(worker, 'refresh'), \
             patch.object(worker, 'restored_start', return_value=False), \
             patch.object(worker, 'container_records', return_value=[STOPPED]), patch.object(worker, 'run') as run:
            directory = Path(folder) / ID
            directory.mkdir()
            (directory / 'compose.runtime.json').write_text('{"services":{"app":{}}}')
            worker.action(ID, 'pve', 'start')
            self.assertEqual(run.call_args.args[0], ['docker', 'start', STOPPED['id']])
