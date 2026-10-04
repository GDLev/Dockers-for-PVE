"""Service limits, lifecycle preservation and project-scoped console access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker

ID = 'a' * 32
VALUES = dict(cpus=1.5, memory=512, swap=256, cpuunits=1024, pids=200,
              dns=['1.1.1.1'], dns_search=['example.test'], restart='unless-stopped')


class ManagementTests(unittest.TestCase):
    def test_limits_override_conflicting_compose_values_only_for_selected_service(self):
        config = {'services': {'web': {'cpus': 4, 'cpu_quota': 800000, 'mem_limit': 4096,
                   'deploy': {'resources': {'limits': {'cpus': '4', 'memory': '4096'},
                                             'reservations': {'memory': '2048'}}}},
                               'db': {'mem_limit': 123}}}
        worker.apply_settings(config, {'web': VALUES})
        web = config['services']['web']
        self.assertEqual(web['cpus'], 1.5)
        self.assertEqual(web['mem_limit'], 512 * 1048576)
        self.assertEqual(web['memswap_limit'], 768 * 1048576)
        self.assertNotIn('cpu_quota', web)
        self.assertNotIn('resources', web['deploy'])
        self.assertEqual(config['services']['db']['mem_limit'], 123)

    def test_swap_and_input_validation(self):
        for change in ({'memory': -1}, {'cpus': True}, {'cpuunits': 1}, {'pids': 2.5},
                       {'dns': ['not-an-address']}, {'dns_search': ['$(id)']},
                       {'restart': 'arbitrary'}, {'memory': 0, 'swap': 200}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                worker.validate_settings(dict(VALUES, **change))
        config = {'services': {'app': {}}}
        worker.apply_settings(config, {'app': dict(VALUES, swap=-1)})
        self.assertEqual(config['services']['app']['memswap_limit'], -1)

    def test_console_cannot_exec_into_another_project_or_stopped_container(self):
        container = 'b' * 64
        with patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[]):
            with self.assertRaises(ValueError):
                worker.console_target(ID, 'pve', container, '/bin/sh')
        with patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[{'id': container, 'state': 'running'}]):
            self.assertEqual(worker.console_target(ID, 'pve', container, '/bin/sh'), ['docker', 'exec', '-it', container, '/bin/sh'])
            with self.assertRaises(ValueError):
                worker.console_target(ID, 'pve', container, 'sh;id')

    def test_stopped_service_is_not_started_and_running_service_is_recreated(self):
        for state in ('stopped', 'running'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                directory = root / ID
                directory.mkdir()
                (directory / '.env').touch()
                (directory / 'compose.runtime.json').write_text(json.dumps({'services': {'web': {}, 'db': {}}}))
                with patch.object(worker, 'ROOT', root), patch.object(worker, 'locked') as lock, \
                     patch.object(worker, 'detail', return_value={}), patch.object(worker, 'refresh'), \
                     patch.object(worker, 'container_records', return_value=[{'service': 'web', 'state': state}]), \
                     patch.object(worker, 'run') as run:
                    worker.service_settings(ID, 'pve', 'web', VALUES)
                    args = run.call_args.args[0]
                    self.assertIn('-d' if state == 'running' else '--no-start', args)
                    self.assertEqual(args[-1], 'web')
                    self.assertIn('--no-deps', args)
                    self.assertEqual(worker.load_settings(directory)['web'], VALUES)

    def test_failed_change_restores_runtime_and_does_not_persist_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / ID
            directory.mkdir()
            runtime = directory / 'compose.runtime.json'
            original = json.dumps({'services': {'web': {}}})
            runtime.write_text(original)
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'locked'), \
                 patch.object(worker, 'detail', return_value={}), patch.object(worker, 'container_records', return_value=[]), \
                 patch.object(worker, 'run', side_effect=ValueError('failed')):
                with self.assertRaises(ValueError):
                    worker.service_settings(ID, 'pve', 'web', VALUES)
            self.assertEqual(runtime.read_text(), original)
            self.assertFalse((directory / 'settings.json').exists())
