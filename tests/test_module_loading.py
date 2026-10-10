"""Feature modules must also work when imported before the command dispatcher."""
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ModuleLoadingTests(unittest.TestCase):
    def test_feature_modules_load_in_a_fresh_process(self):
        for name, operation in {
            'deployment': 'create',
            'lifecycle': 'daemon_resume',
            'metrics': 'rrddata',
            'network': 'network_config',
            'policy': 'delegated_access',
            'settings': 'service_settings',
            'snapshots': 'snapshot_capture',
            'storage': 'project_pve_storage',
        }.items():
            with self.subTest(module=name):
                code = (
                    'import sys; sys.path.insert(0, "scripts"); '
                    f'from dctlib import {name}; from dctlib import projects; '
                    f'assert projects.{operation} is {name}.{operation}; '
                    'assert (projects.HELPERS / "storage.pl").is_file(); '
                    'assert (projects.HELPERS / "hooks.pl").is_file()'
                )
                subprocess.run([sys.executable, '-c', code], cwd=ROOT, check=True, capture_output=True)

    def test_command_entry_point_loads_without_host_operations(self):
        result = subprocess.run(
            [sys.executable, 'scripts/provision.py', '--help'], cwd=ROOT,
            check=True, capture_output=True, text=True,
        )
        self.assertIn('snapshot', result.stdout)
        self.assertIn('network-config', result.stdout)
