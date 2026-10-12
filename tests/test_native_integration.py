"""New UI API contracts: scope, non-secret previews and operation recovery."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker, BASE
from test_management import VALUES

ID = 'a' * 32


class NativeIntegrationTests(unittest.TestCase):
    def test_preview_does_not_return_environment_secrets_or_host_paths(self):
        config = {'services': {'web': {'image': 'nginx', 'environment': {'PASSWORD': 'private'},
                  'volumes': [{'type': 'bind', 'source': '/private/secret', 'target': '/data'}],
                  'ports': [{'published': '8080', 'target': 80}]}}}
        with patch.object(worker, 'prepare', return_value=config):
            result = worker.preview(BASE)
        self.assertEqual(result[0]['service'], 'web')
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('PASSWORD', json.dumps(result))

    def test_service_actions_cannot_target_unknown_service_or_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ID).mkdir()
            (root / ID / 'compose.runtime.json').write_text(json.dumps({'services': {'web': {}}}))
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'locked'), \
                 patch.object(worker, 'detail', return_value={}), patch.object(worker, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'Invalid service'):
                    worker.service_action(ID, 'pve', '--privileged', 'start')
                run.assert_not_called()
                with patch.object(worker, 'refresh'):
                    worker.service_action(ID, 'pve', 'web', 'stop')
                self.assertEqual(run.call_args.args[0][-2:], ['stop', 'web'])

    def test_operation_excludes_conflicting_requests_and_retains_safe_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / 'proc'
            (proc / str(os.getpid())).mkdir(parents=True)
            (proc / str(os.getpid()) / 'stat').write_text('1 (python) ' + ' '.join(['S'] + ['0'] * 18 + ['123']))
            with patch.object(worker, 'ROOT', root / 'projects'), patch.object(worker, 'PROC', proc):
                with self.assertRaisesRegex(ValueError, 'private password'):
                    with worker.operation(ID, 'update'):
                        self.assertTrue(worker.operation_state(ID)['active'])
                        with self.assertRaisesRegex(ValueError, 'Another operation'):
                            with worker.operation(ID, 'stop'):
                                pass
                        raise ValueError('private password')
                state = worker.operation_state(ID)
                self.assertFalse(state['active'])
                self.assertTrue(state['failed'])
                self.assertNotIn('password', json.dumps(state))
                with worker.operation(ID, 'start'):
                    self.assertNotIn('failed', worker.operation_state(ID))

    def test_dead_operation_is_reported_as_interrupted(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder) / 'projects'):
            worker.write_json(Path(folder) / 'operations' / (ID + '.json'), {'active': True, 'pid': 0})
            self.assertTrue(worker.operation_state(ID)['failed'])
            self.assertFalse(worker.operation_state(ID)['active'])

    def test_tags_validate_and_do_not_copy_deployment_to_cluster_metadata(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'METADATA', Path(folder)), \
             patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={'id': ID, 'deployment': {'password': 'private'}}):
            self.assertEqual(worker.project_tags(ID, 'pve', 'web;prod;web')['tags'], 'prod;web')
            self.assertNotIn('deployment', json.loads((Path(folder) / (ID + '.json')).read_text()))
            with self.assertRaises(ValueError):
                worker.project_tags(ID, 'pve', '<script>')

    def test_instance_logs_include_stderr_and_check_ownership(self):
        container = 'b' * 64
        with patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[{'id': container}]), \
             patch.object(worker, 'run', return_value=subprocess.CompletedProcess([], 0, '2026 info\n', '2025 error\n')):
            self.assertEqual(worker.project_logs(ID, 'pve', container=container), '2025 error\n2026 info\n')
            with self.assertRaises(ValueError):
                worker.project_logs(ID, 'pve', container='c' * 64)

    def test_reset_preserves_stopped_service_and_rolls_back_failed_recreation(self):
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                directory = root / ID
                directory.mkdir()
                worker.write_json(directory / 'settings.json', {'web': VALUES})
                worker.write_json(directory / 'compose.runtime.json', {'services': {'web': {'cpus': 1.5}}})
                original = (directory / 'compose.runtime.json').read_text()
                def prepare(data, identifier, staging, project_directory, settings=None):
                    worker.write_json(staging / 'compose.runtime.json', {'services': {'web': {}}})
                with patch.object(worker, 'ROOT', root), patch.object(worker, 'locked'), \
                     patch.object(worker, 'detail', return_value={'deployment': BASE}), \
                     patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'prepare', side_effect=prepare), \
                     patch.object(worker, 'refresh'), patch.object(worker, 'run', side_effect=ValueError('failure') if fails else None) as run:
                    if fails:
                        with self.assertRaises(ValueError):
                            worker.reset_settings(ID, 'pve', 'web')
                        self.assertEqual(worker.load_settings(directory)['web'], VALUES)
                        self.assertEqual((directory / 'compose.runtime.json').read_text(), original)
                    else:
                        worker.reset_settings(ID, 'pve', 'web')
                        self.assertEqual(worker.load_settings(directory), {})
                        self.assertIn('--no-start', run.call_args.args[0])
                        self.assertIn('--no-deps', run.call_args.args[0])
