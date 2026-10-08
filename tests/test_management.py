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

    def test_disk_setting_only_changes_selected_storage_size(self):
        config = {'services': {'web': {'storage_opt': {'other': 'retained'}}, 'db': {'storage_opt': {'size': '2G'}}}}
        worker.apply_settings(config, {'web': dict(VALUES, disk=512)})
        self.assertEqual(config['services']['web']['storage_opt'], {'other': 'retained', 'size': '512M'})
        self.assertEqual(config['services']['db']['storage_opt']['size'], '2G')
        worker.apply_settings(config, {'web': dict(VALUES, disk=0)})
        self.assertNotIn('size', config['services']['web']['storage_opt'])
        for disk in (-1, True, 0.5, 1048577):
            with self.assertRaises(ValueError):
                worker.validate_settings(dict(VALUES, disk=disk))

    def test_unsupported_container_quota_fails_before_runtime_change(self):
        with patch.object(worker, 'rootfs_quota_support', return_value={'supported': False}), patch.object(worker, 'locked') as lock:
            with self.assertRaisesRegex(ValueError, 'XFS'):
                worker.service_settings(ID, 'pve', 'web', dict(VALUES, disk=512))
            lock.assert_not_called()

    def test_data_disk_rejects_running_and_external_mounts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), patch.object(worker, 'locked'), patch.object(worker, 'detail', return_value={}):
            directory = Path(folder) / ID
            directory.mkdir()
            runtime = directory / 'compose.runtime.json'
            runtime.write_text(json.dumps({'services': {'web': {'volumes': [{'type': 'bind', 'source': '/etc', 'target': '/data'}]}}}))
            for state in ('running', 'paused', 'restarting', 'removing'):
                with self.subTest(state=state), patch.object(worker, 'container_records', return_value=[{'state': state}]):
                    with self.assertRaisesRegex(ValueError, 'Stop all'):
                        worker.project_storage_limit(ID, 'pve', 512)
            with patch.object(worker, 'container_records', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'external host paths'):
                    worker.project_storage_limit(ID, 'pve', 512)
            self.assertFalse((directory / 'storage.ext4').exists())

    def test_data_disk_mapping_persists_and_rejects_bypass(self):
        mapping = {'bind:/owned/data': '/owned/storage-data/data-0'}
        config = {'services': {'web': {'volumes': [{'type': 'bind', 'source': '/owned/data', 'target': '/data'}]}}}
        with patch.object(worker, 'storage_config', return_value={'mapping': mapping}), patch.object(worker, 'storage_mount'):
            worker.apply_storage(config, ID)
            self.assertEqual(config['services']['web']['volumes'][0]['source'], mapping['bind:/owned/data'])
            worker.apply_storage(config, ID)
            config['services']['web']['volumes'].append({'type': 'bind', 'source': '/external', 'target': '/new'})
            with self.assertRaisesRegex(ValueError, 'cannot be bypassed'):
                worker.apply_storage(config, ID)

    def test_data_disk_is_grow_only(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), patch.object(worker, 'locked'), patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'storage_config', return_value={'size_mib': 512}), patch.object(worker, 'run') as run:
            for size in (256, 512):
                with self.assertRaisesRegex(ValueError, 'only be enlarged'):
                    worker.project_storage_limit(ID, 'pve', size)
            run.assert_not_called()

    def test_disk_units(self):
        self.assertEqual(worker.disk_mib('512M'), 512)
        self.assertEqual(worker.disk_mib('2GiB'), 2048)
        self.assertEqual(worker.disk_mib('1073741824'), 1024)

    def test_console_cannot_exec_into_another_project_or_stopped_container(self):
        container = 'b' * 64
        with patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[]):
            with self.assertRaises(ValueError):
                worker.console_target(ID, 'pve', container, '/bin/sh')
        with patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[{'id': container, 'state': 'running'}]):
            self.assertEqual(worker.console_target(ID, 'pve', container, '/bin/sh'), ['docker', 'exec', '-it', container, '/bin/sh'])
            with self.assertRaises(ValueError):
                worker.console_target(ID, 'pve', container, 'sh;id')

    def test_stopped_service_is_not_started_and_running_dns_change_is_pending(self):
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
                     patch.object(worker, 'update_live_containers'), patch.object(worker, 'run') as run:
                    if state == 'running':
                        worker.service_settings(ID, 'pve', 'web', VALUES)
                        self.assertEqual(worker.load_pending(directory)['web']['dns'], VALUES['dns'])
                        run.assert_not_called()
                        continue
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


class LiveSettingsTests(unittest.TestCase):
    def test_live_flags_and_unlimited_pids(self):
        args = worker.live_settings_arguments({'cpus': 1, 'mem_limit': 32, 'pids_limit': 100},
                                               {'cpus': 2, 'mem_limit': 64})
        self.assertEqual(args, ['--cpus', '2', '--memory', '64', '--pids-limit', '-1'])

    def test_unsupported_changes_reject_before_docker_update(self):
        for old, new in [({'cpus': 1}, {}), ({}, {'cpus': 1}), ({}, {'mem_limit': 32}), ({'mem_limit': 32}, {}), ({'cpu_shares': 100}, {}),
                         ({}, {'dns': ['1.1.1.1']}), ({}, {'storage_opt': {'size': '512M'}})]:
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, 'Stop'):
                worker.live_settings_arguments(old, new)

    def test_failed_live_update_rolls_back_all_touched_replicas(self):
        containers = [{'id': 'a'}, {'id': 'b'}]
        inspected = [{'Id': c['id'], 'HostConfig': {'NanoCpus': 1000000000}} for c in containers]
        with patch.object(worker, 'capture', return_value=json.dumps(inspected)), \
             patch.object(worker, 'run', side_effect=[None, ValueError('failed'), None, None]) as run:
            with self.assertRaisesRegex(ValueError, 'failed'):
                worker.update_live_containers(containers, ['--cpus', '2'])
            self.assertEqual([c.args[0] for c in run.call_args_list],
                             [['docker', 'update', '--cpus', value, c] for value, c in
                              [('2', 'a'), ('2', 'b'), ('1.0', 'a'), ('1.0', 'b')]])

    def test_running_resource_save_uses_docker_update_without_compose(self):
        values = dict(VALUES, dns=[], dns_search=[], restart='no')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); directory = root / ID; directory.mkdir()
            config = {'services': {'web': {'cpus': 1, 'mem_limit': 32 * 1048576,
                                          'memswap_limit': 32 * 1048576, 'restart': 'no'}}}
            runtime = directory / 'compose.runtime.json'; runtime.write_text(json.dumps(config))
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'locked'), \
                 patch.object(worker, 'detail', return_value={}), patch.object(worker, 'refresh'), \
                 patch.object(worker, 'container_records', return_value=[{'id': 'a', 'service': 'web', 'state': 'running'}]), \
                 patch.object(worker, 'update_live_containers') as update, patch.object(worker, 'run') as run:
                worker.service_settings(ID, 'pve', 'web', values)
                update.assert_called_once()
                run.assert_not_called()
                self.assertEqual(worker.load_settings(directory)['web'], values)
                self.assertEqual(json.loads(runtime.read_text())['services']['web']['cpus'], 1.5)

    def test_all_transitional_states_require_stop(self):
        for state in ('running', 'paused', 'restarting', 'removing'):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'Stop'):
                worker.require_stopped([{'state': state}])
        worker.require_stopped([{'state': s} for s in ('created', 'exited', 'dead')])

    def test_failed_replica_update_restores_default_cpu_weight(self):
        containers = [{'id': 'a'}, {'id': 'b'}]
        inspected = [{'Id': c['id'], 'HostConfig': {'CpuShares': 0}} for c in containers]
        with patch.object(worker, 'capture', return_value=json.dumps(inspected)), \
             patch.object(worker, 'run', side_effect=[None, ValueError('failed'), None, None]) as run:
            with self.assertRaisesRegex(ValueError, 'failed'):
                worker.update_live_containers(containers, ['--cpu-shares', '512'])
            self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'update', '--cpu-shares', '1024', 'b'])

    def test_actual_unlimited_container_rejected_before_update(self):
        with patch.object(worker, 'capture', return_value=json.dumps([{'Id': 'a', 'HostConfig': {'NanoCpus': 0}}])), \
             patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Stop'):
                worker.update_live_containers([{'id': 'a'}], ['--cpus', '1'])
            run.assert_not_called()
