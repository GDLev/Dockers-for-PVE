"""Mode preservation, process isolation and portable ownership contracts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import runtime
import runtime_install

ROOTLESS = runtime.Runtime('rootless', 'betterdocker', 997, 995,
                           '/var/lib/betterdocker/runtime', '/run/user/997/docker.sock',
                           '/var/lib/betterdocker/runtime/docker', 1048576, 1114112, 65536)


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, 'Runtime installation requires host root')
    def test_fresh_private_runtime_parent_allows_traversal_without_exposing_siblings(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            parent = base / 'state'
            parent.mkdir(mode=0o700)
            home = parent / 'runtime'
            account = SimpleNamespace(pw_uid=12345, pw_gid=12345, pw_dir=str(home))
            with patch.object(runtime_install, 'HOME', str(home)), \
                    patch.object(runtime, 'CONFIG', base / 'runtime.json'), \
                    patch.object(runtime_install, 'preflight'), \
                    patch.object(runtime_install.pwd, 'getpwnam', return_value=account), \
                    patch.object(runtime_install, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='betterdocker')), \
                    patch.object(runtime_install, 'subordinate', return_value=1048576), \
                    patch.object(runtime, 'load', side_effect=RuntimeError('fixture stops before daemon startup')):
                with self.assertRaisesRegex(RuntimeError, 'fixture stops before daemon startup'):
                    runtime_install.initialize('rootless')
            self.assertEqual(parent.stat().st_mode & 0o777, 0o711)
            self.assertEqual(parent.stat().st_uid, 0)
            self.assertEqual(home.stat().st_uid, 0)
            for name in ('docker', '.docker'):
                self.assertEqual((home / name).stat().st_mode & 0o777, 0o700)
                self.assertEqual((home / name).stat().st_uid, account.pw_uid)

    def test_legacy_retained_projects_keep_rootful_mode_on_reinstall(self):
        with patch.object(Path, 'exists', return_value=False), \
                patch.object(Path, 'glob', return_value=[Path('/retained/request.json')]):
            self.assertEqual(runtime_install.select(None), 'rootful')
            with self.assertRaisesRegex(ValueError, 'fixed'):
                runtime_install.select('rootless')

    def test_fresh_choice_defaults_to_rootless_and_unattended_shell_requires_choice(self):
        terminal = MagicMock()
        terminal.__enter__.return_value = terminal
        terminal.readline.return_value = ''
        with patch.object(Path, 'exists', return_value=False), patch.object(Path, 'glob', return_value=[]), \
                patch('builtins.open', return_value=terminal):
            self.assertEqual(runtime_install.select(None, interactive=True), 'rootless')
            self.assertEqual(runtime_install.select('rootful'), 'rootful')
            with self.assertRaisesRegex(ValueError, 'unattended installation requires'):
                runtime_install.select(None)

    def test_rootless_lifecycle_does_not_pull_in_host_docker(self):
        for mode in ('rootful', 'rootless'):
            with patch.object(runtime_install, 'save') as saved, patch.object(Path, 'unlink'):
                runtime_install.lifecycle(mode)
            unit = next(call.args[1] for call in saved.call_args_list if call.args[0].endswith('betterdocker-lifecycle.service'))
            firewall = next(call.args[1] for call in saved.call_args_list if call.args[0].endswith('betterdocker-firewall.service'))
            if mode == 'rootless':
                self.assertNotIn('docker.service', unit)
                self.assertIn('Requires=betterdocker-rootless.service', unit)
                self.assertIn('After=pve-cluster.service betterdocker-rootless.service', firewall)
            else:
                self.assertIn('BindsTo=docker.service', unit)
                self.assertIn('WantedBy=docker.service', unit)

    def test_rootless_ownership_roundtrip_including_distinct_root_and_gid(self):
        for identifier in (0, 1, 999, 65536):
            for group in (False, True):
                self.assertEqual(ROOTLESS.container_id(ROOTLESS.host_id(identifier, group), group), identifier)
        self.assertEqual(ROOTLESS.host_id(0), 997)
        self.assertEqual(ROOTLESS.host_id(0, True), 995)
        self.assertEqual(ROOTLESS.host_id(1), 1048576)

    def test_unmapped_owners_are_rejected_instead_of_becoming_host_root(self):
        for owner in (0, 500, 1048575, 1048576 + 65536):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                ROOTLESS.container_id(owner)
        with self.assertRaises(ValueError):
            ROOTLESS.host_id(65537)

    def test_rootless_child_has_no_supplementary_groups_or_inherited_docker_credentials(self):
        with patch.object(runtime, 'load', return_value=ROOTLESS), patch.object(runtime.os, 'geteuid', return_value=0), \
             patch.object(runtime, 'execute', return_value=subprocess.CompletedProcess([], 0, 'OK', '')) as run, \
             patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONFIG': '/root/.docker', 'SECRET': 'must-not-pass'}):
            runtime.run(['docker', 'info'], text=True, capture_output=True)
        values = run.call_args.kwargs
        self.assertEqual((values['user'], values['group'], values['extra_groups']), (997, 995, []))
        self.assertEqual(values['env']['DOCKER_HOST'], 'unix:///run/user/997/docker.sock')
        self.assertNotIn('SECRET', values['env'])
        self.assertNotEqual(values['env']['DOCKER_CONFIG'], '/root/.docker')

    def test_existing_mode_cannot_be_changed_by_install_or_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'runtime.json'; config.write_text('{}')
            with patch.object(runtime, 'CONFIG', config), patch.object(runtime, 'load', return_value=ROOTLESS):
                self.assertEqual(runtime_install.select(None, interactive=False), 'rootless')
                self.assertEqual(runtime_install.select('rootless'), 'rootless')
                with self.assertRaisesRegex(ValueError, 'fixed'):
                    runtime_install.select('rootful')

    def test_runtime_config_cannot_be_a_link_or_writable_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'target'; target.write_text(json.dumps({'mode': 'rootful'})); target.chmod(0o666)
            link = Path(folder) / 'runtime.json'; link.symlink_to(target)
            with patch.object(runtime, 'CONFIG', link), self.assertRaises(OSError):
                runtime.load()
            with patch.object(runtime, 'CONFIG', target), self.assertRaisesRegex(ValueError, 'writable|root-owned'):
                runtime.load()

    def test_target_mode_preflight_does_not_convert_or_ignore_capacity(self):
        with patch.object(runtime, 'load', return_value=ROOTLESS):
            runtime.assert_compatible(dict(mode='rootless', count=65536))
            with self.assertRaisesRegex(ValueError, 'modes differ'):
                runtime.assert_compatible(dict(mode='rootful'))
            with self.assertRaisesRegex(ValueError, 'capacities differ'):
                runtime.assert_compatible(dict(mode='rootless', count=65535))

    def test_stable_subordinate_range_skips_other_users_and_reuses_own_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'subuid'; path.write_text('root:100000:65536\nother:1048576:65536\n')
            value = runtime_install.subordinate('betterdocker', str(path))
            self.assertEqual(value, 1114112)
            original = path.read_text()
            self.assertEqual(runtime_install.subordinate('betterdocker', str(path)), value)
            self.assertEqual(path.read_text(), original)

    def test_port_ranges_keep_external_ports_and_reject_ambiguous_ranges(self):
        ports = [{'published': '80-81', 'target': '8080-8081', 'protocol': 'tcp'}]
        self.assertEqual(runtime.port_bindings(ports, '192.0.2.211'), {
            '8080/tcp': [{'HostIp': '192.0.2.211', 'HostPort': '80'}],
            '8081/tcp': [{'HostIp': '192.0.2.211', 'HostPort': '81'}]})
        with self.assertRaises(ValueError):
            runtime.port_bindings([dict(published='80-81', target=8080)], '192.0.2.211')

    def test_volume_metadata_cannot_redirect_privileged_copy_to_host_files(self):
        with patch.object(runtime, 'load', return_value=ROOTLESS):
            with self.assertRaisesRegex(ValueError, 'outside'):
                runtime.check_volume_path('/etc', 'bd-owned')

    def test_volume_name_is_one_component_before_joining_runtime_storage(self):
        with patch.object(Path, 'is_symlink', return_value=False):
            for name in ('/fixture', '../fixture', 'a/b', '.', '', None):
                path = Path(ROOTLESS.data) / 'volumes' / str(name) / '_data'
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'name'):
                    runtime.check_volume_path(path, name, ROOTLESS)
            runtime.check_volume_path(Path(ROOTLESS.data) / 'volumes/good-volume/_data',
                                      'good-volume', ROOTLESS)

    def test_requested_limits_fail_when_controllers_are_unavailable(self):
        with patch.object(Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(ValueError, 'not delegated'):
                runtime.check_resources({'services': {'app': {'mem_limit': 67108864}}}, ROOTLESS)
            with self.assertRaisesRegex(ValueError, 'quotas'):
                runtime.check_resources({'services': {'app': {'storage_opt': {'size': '1G'}}}}, ROOTLESS)

    def test_runtime_data_cannot_redirect_a_copy_through_an_ancestor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'actual').mkdir()
            (root / 'actual' / 'file').write_text('data')
            (root / 'link').symlink_to(root / 'actual')
            with self.assertRaises(OSError):
                with runtime.anchored(root / 'link' / 'file'):
                    self.fail('Symlink ancestor was followed')
            with runtime.anchored(root / 'actual' / 'file') as fd:
                self.assertEqual(os.read(fd, 4), b'data')

    def test_atomic_config_retry_ignores_abandoned_previous_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'runtime.json'
            path.with_suffix('.json.new').write_text('interrupted')
            runtime_install.save(path, 'first')
            runtime_install.save(path, 'second')
            self.assertEqual(path.read_text(), 'second')

    def test_rootless_forwarding_does_not_change_authored_port_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'compose.runtime.json'
            config = {'services': {'app': {'image': 'busybox', 'ports': [{'published': '80', 'target': 8080}]}}}
            path.write_text(json.dumps(config))
            with patch.object(runtime, 'load', return_value=ROOTLESS), patch.object(runtime, 'read_access'), \
                 patch.object(runtime, 'check_resources'), patch.object(runtime, 'drop_options', return_value={'user': ROOTLESS.uid, 'group': ROOTLESS.gid, 'extra_groups': []}), \
                 patch.object(runtime, 'execute') as run:
                runtime.run(['docker', 'compose', '-f', str(path), '--project-directory', folder, 'up', '--no-start'])
            actual = json.loads(path.with_name('.rootless-compose.json').read_text())
            self.assertNotIn('ports', actual['services']['app'])
            self.assertEqual(actual['networks']['default']['driver_opts']['com.docker.network.bridge.gateway_mode_ipv4'], 'nat-unprotected')
            self.assertEqual(json.loads(path.read_text()), config)
            self.assertEqual(run.call_args.kwargs['user'], ROOTLESS.uid)

    def test_failed_install_restores_owned_service_files_and_enable_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, enabled, added = root / 'docker.service', root / 'enabled', root / 'new.conf'
            service.write_text('original service')
            service.chmod(0o644)
            enabled.symlink_to('docker.service')
            with patch.object(runtime_install, 'support_files', return_value=[service, enabled, added]), \
                 patch.object(runtime_install.os, 'chown'), patch.object(runtime_install, 'command'):
                runtime_install.snapshot_support(root)
                service.write_text('failed installation')
                enabled.unlink(); enabled.symlink_to('unexpected.service')
                added.write_text('new delegation')
                runtime_install.restore_support(root)
            self.assertEqual(service.read_text(), 'original service')
            self.assertEqual(os.readlink(enabled), 'docker.service')
            self.assertFalse(added.exists())


if __name__ == '__main__':
    unittest.main()
