"""Reject unsupported hosts and runtimes before installing integration."""
import json
import subprocess
import unittest
from unittest.mock import patch

from dctlib import requirements as policy
import installer


class RequirementsTests(unittest.TestCase):
    def test_host_baseline_and_series(self):
        for version, kernel in [('9.2.21', '7.0.14-23-pve'), ('9.2.22', '7.0.15-1-pve')]:
            policy.require_host('pve-manager/' + version + '/fixture', kernel)
        for version, kernel in [('9.1.99', '7.0.14-23-pve'), ('9.2.20', '7.0.14-23-pve'),
                                ('9.3.0', '7.0.14-23-pve'), ('9.2.21', '7.0.14-22-pve'),
                                ('9.2.21', '6.17.13-21-pve'), ('9.2.21', '7.0.14-custom')]:
            with self.subTest(version=version, kernel=kernel), self.assertRaisesRegex(ValueError, 'Update PVE and reboot'):
                policy.require_host('pve-manager/' + version + '/fixture', kernel)

    def test_uninstall_remains_available_on_unsupported_host(self):
        node = dict(error='', version='pve-manager/9.1.1/fixture')
        self.assertTrue(installer.supported(node, 'uninstall'))
        self.assertTrue(installer.supported(node, 'finish'))
        self.assertFalse(installer.supported(node, 'install'))
        node['version'] = 'pve-manager/9.2.21/fixture'
        self.assertTrue(installer.supported(node, 'install'))

    def test_runtime_minima_and_unverified_series(self):
        for engine, containerd in [('29.9.1', 'v2.4.2'), ('29.10.0', '2.4.3+vendor')]:
            policy.require_runtime(engine, containerd)
        for engine, containerd in [('29.9.0', '2.4.2'), ('29.9.1', '2.4.1'),
                                  ('30.0.0', '2.4.2'), ('29.9.1', '2.5.0'),
                                  ('29.9.1-rc.1', '2.4.2'), ('development', '2.4.2'),
                                  ('29.9.1', 'unknown')]:
            with self.subTest(engine=engine, containerd=containerd), self.assertRaisesRegex(ValueError, 'maintenance window'):
                policy.require_runtime(engine, containerd)

    def test_actual_daemon_components_are_checked(self):
        for engine, containerd, accepted in [('29.9.1', 'v2.4.2', True), ('29.8.2', 'v2.4.2', False), ('29.9.1', 'v2.3.6', False)]:
            output = dict(Version=engine, Components=[dict(Name='containerd', Version=containerd)])
            with self.subTest(engine=engine, containerd=containerd), patch('runtime.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(output), '')) as run:
                if accepted:
                    policy.require_daemon()
                else:
                    with self.assertRaises(ValueError):
                        policy.require_daemon()
                self.assertEqual(run.call_args.args[0], ['docker', 'version', '--format', '{{json .Server}}'])

    def test_binary_output_parsing(self):
        with patch.object(policy.subprocess, 'check_output', side_effect=['Docker version 29.9.1, build fixture\n', 'containerd containerd.io v2.4.2 fixture\n']):
            policy.require_binaries()
        with patch.object(policy.subprocess, 'check_output', side_effect=['unexpected', 'containerd containerd.io v2.4.2 fixture\n']):
            with self.assertRaises(ValueError):
                policy.require_binaries()
