"""Delegated Docker requests must not become unrestricted host-root operations."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker

ID = 'a' * 32


class DeploymentSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / ID
        self.project.mkdir()
        self.patch = patch.object(worker, 'ROOT', self.root)
        self.patch.start()
        self.config = {'services': {'app': {'image': 'busybox:1.37'}},
                       'networks': {'default': {'name': 'bd-' + ID + '_default'}}}
        self.container = {'HostConfig': {'NetworkMode': 'bd-' + ID + '_default', 'IpcMode': 'private',
                           'CgroupnsMode': 'private', 'Runtime': 'runc'}, 'Mounts': []}

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_basic_compose_and_owned_mounts(self):
        self.config['services']['app']['volumes'] = [{'type': 'bind', 'source': str(self.project / 'data'), 'target': '/data'}]
        worker.validate_delegated_config(self.config, ID)
        worker.validate_delegated_source('services:\n  app:\n    image: busybox\n    env_file: .env\n')
        worker.validate_delegated_containers([self.container], ID)

    def test_file_read_directives_rejected_before_compose(self):
        for source in ('include: /etc/shadow\nservices: {}',
                       'services:\n  app:\n    image: busybox\n    env_file: /etc/shadow',
                       'services:\n  app:\n    image: busybox\n    label_file: /etc/shadow',
                       'services:\n  app:\n    extends: {file: /etc/pve/user.cfg, service: app}',
                       'configs: {secret: {file: /etc/shadow}}\nservices: {}'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                worker.validate_delegated_source(source)

    def test_unsafe_service_options_are_rejected(self):
        for option in ('privileged', 'pid', 'ipc', 'network_mode', 'devices', 'cap_add', 'security_opt',
                       'volumes_from', 'userns_mode', 'runtime', 'use_api_socket', 'post_start'):
            config = copy.deepcopy(self.config)
            config['services']['app'][option] = True
            with self.subTest(option=option), self.assertRaises(ValueError):
                worker.validate_delegated_config(config, ID)

    def test_host_and_control_mounts_are_rejected(self):
        for source in ('/', '/root', '/etc/pve', '/var/run/docker.sock', str(self.root / ('b' * 32) / 'data'),
                       str(self.project), str(self.project / '.env'), str(self.project / 'compose.runtime.json'),
                       str(self.project / 'snapshots' / 'old')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                worker.safe_bind(source, ID)

    def test_symlink_cannot_bypass_project_boundary(self):
        (self.project / 'data').symlink_to('/etc', target_is_directory=True)
        with self.assertRaises(ValueError):
            worker.safe_bind(str(self.project / 'data' / 'shadow'), ID)

    def test_shared_resources_and_driver_options_are_rejected(self):
        for kind, value in [('volumes', {'external': True}), ('volumes', {'driver_opts': {'device': '/'}}),
                            ('volumes', {'name': 'another-project-data'}), ('networks', {'driver': 'host'}),
                            ('networks', {'name': 'bridge'}), ('networks', {'external': True}),
                            ('networks', {'ipam': {'driver': 'custom'}}), ('networks', {'ipam': {'options': {'device': '/'}}})]:
            config = copy.deepcopy(self.config)
            config[kind] = {'data': value}
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                worker.validate_delegated_config(config, ID)

    def test_saved_host_config_cannot_override_safe_compose(self):
        for key, value in [('Privileged', True), ('CapAdd', ['SYS_ADMIN']), ('PidMode', 'host'),
                           ('IpcMode', 'host'), ('NetworkMode', 'host'), ('UsernsMode', 'host'),
                           ('Devices', [{'PathOnHost': '/dev/sda'}]), ('SecurityOpt', ['apparmor=unconfined']),
                           ('Binds', ['/:/host']), ('Runtime', 'custom')]:
            container = copy.deepcopy(self.container)
            container['HostConfig'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.validate_delegated_containers([container], ID)

    def test_raw_mount_driver_is_rejected(self):
        container = copy.deepcopy(self.container)
        container['HostConfig']['Mounts'] = [{'Type': 'volume', 'Source': 'bd-' + ID + '_data',
            'VolumeOptions': {'DriverConfig': {'Name': 'local', 'Options': {'device': '/'}}}}]
        with self.assertRaises(ValueError):
            worker.validate_delegated_containers([container], ID)

    def test_shared_bind_propagation_rejected(self):
        container = copy.deepcopy(self.container)
        container['HostConfig']['Binds'] = [str(self.project / 'data') + ':/data:rshared']
        with self.assertRaises(ValueError):
            worker.validate_delegated_containers([container], ID)

    def test_external_logging_and_device_reservations_rejected(self):
        for changes in ({'logging': {'driver': 'syslog'}},
                        {'deploy': {'resources': {'reservations': {'devices': [{'capabilities': ['gpu']}]}}}}):
            config = copy.deepcopy(self.config)
            config['services']['app'].update(changes)
            with self.assertRaises(ValueError):
                worker.validate_delegated_config(config, ID)

    def test_policy_identity_is_explicit(self):
        with patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '0'}):
            self.assertFalse(worker.trusted_management())
        with patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '1'}):
            self.assertTrue(worker.trusted_management())

    def test_console_rechecks_safe_state_and_container_ownership(self):
        cid = 'b' * 64
        with patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=[{'id': cid, 'state': 'running'}]), \
             patch.object(worker, 'delegated_access', side_effect=ValueError('unsafe')), \
             patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '0'}):
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                worker.console_target(ID, 'pve', cid, '/bin/sh')
            with self.assertRaisesRegex(ValueError, 'belonging'):
                worker.console_target(ID, 'pve', 'c' * 64, '/bin/sh')

    def test_saved_network_options_are_not_trusted(self):
        for value in ({'Driver': 'host'}, {'Driver': 'bridge', 'Options': {'com.docker.network.bridge.name': 'vmbr0'}},
                      {'Driver': 'bridge', 'IPAM': {'Driver': 'custom'}}):
            with self.assertRaises(ValueError):
                worker.validate_delegated_networks([value])
        worker.validate_delegated_networks([{'Driver': 'bridge', 'Options': {}}])

    def test_client_credentials_are_separate_and_identity_reaches_helpers(self):
        with patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '0', 'BETTERDOCKER_TASK_USER': 'owner@pve!token'}), \
             patch.object(worker.subprocess, 'run') as run, patch.object(worker, 'ROOT', self.root / 'projects'):
            worker.run(['docker', 'info'])
            environment = run.call_args.kwargs['env']
            self.assertEqual(environment['DOCKER_CONFIG'], str(self.root / 'delegated-client'))
            self.assertEqual(environment['BETTERDOCKER_TASK_USER'], 'owner@pve!token')
            self.assertEqual(environment['BETTERDOCKER_TRUSTED'], '0')

    def test_control_journals_are_not_application_mounts(self):
        for name in ('storage-transaction.json', 'allocated-volume.json', 'snapshot-tree.json', 'network-resume.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                worker.safe_bind(str(self.project / name), ID)

    def test_unsafe_archive_is_rejected_before_restore_allocates_or_imports(self):
        from test_guest_state import state
        folder = self.root / 'archive'
        (folder / 'configuration').mkdir(parents=True)
        worker.write_json(folder / 'configuration' / 'compose.runtime.json', self.config)
        worker.write_json(folder / 'networks.json', [])
        unsafe = copy.deepcopy(self.container)
        unsafe['HostConfig']['Privileged'] = True
        worker.write_json(folder / 'containers.json', [unsafe])
        with patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '0'}), \
             patch.object(state, 'check_state', return_value={'id': ID, 'mounts': []}), \
             patch.object(state, 'operation') as operation, patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Privileged'):
                state.restore_state(folder, 'b' * 32, 'pve', {})
            operation.assert_not_called()
            run.assert_not_called()

    def test_unsafe_snapshot_is_rejected_before_disk_or_container_changes(self):
        folder = self.root / 'snapshot'
        (folder / 'configuration').mkdir(parents=True)
        worker.write_json(folder / 'configuration' / 'compose.runtime.json', self.config)
        unsafe = copy.deepcopy(self.container)
        unsafe['HostConfig']['Binds'] = ['/:/host']
        worker.write_json(folder / 'containers.json', [unsafe])
        with patch.dict(os.environ, {'BETTERDOCKER_TRUSTED': '0'}), \
             patch.object(worker, 'snapshot_check', return_value=(folder, {'mounts': []})), \
             patch.object(worker, 'detail'), patch.object(worker, 'snapshot_containers', return_value=[]), \
             patch.object(worker, 'snapshot_mounts'), patch.object(worker, 'pve_storage') as storage, \
             patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'project-owned'):
                worker.snapshot_apply(ID, 'pve', 'unsafe')
            storage.assert_not_called()
            run.assert_not_called()
