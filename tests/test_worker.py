import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


worker = module('provision')
patcher = module('patch')


class WorkerTests(unittest.TestCase):
    def test_invalid_config_rejected_before_provisioning(self):
        bad = [[], {'mode': 'other'}, {'mode': 'image', 'image': '--privileged'},
               {'mode': 'compose', 'compose': ''}, {'mode': 'engine', 'ports': [{'host': 0, 'container': 80}]},
               {'mode': 'engine', 'command': 'touch /tmp/x'},
               {'mode': 'engine', 'environment': {'INVALID KEY': 'x'}}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                worker.validate(data)

    def test_compose_generation_preserves_literals_and_mapping(self):
        data = worker.validate({'mode': 'image', 'image': 'nginx:stable',
                                'environment': {'PASSWORD': 'a${SECRET}b'},
                                'command': ['echo', '$(touch /tmp/x)'],
                                'ports': [{'host': 8080, 'container': 80, 'protocol': 'udp'}]})
        service = json.loads(worker.image_compose(data))['services']['app']
        self.assertEqual(service['environment']['PASSWORD'], 'a$${SECRET}b')
        self.assertEqual(service['command'], ['echo', '$$(touch /tmp/x)'])
        self.assertEqual(service['ports'][0]['published'], '8080')
        self.assertEqual(service['ports'][0]['protocol'], 'udp')

    def test_cli_validation(self):
        with tempfile.NamedTemporaryFile(mode='w') as cfg:
            cfg.write('{"mode":"engine"}')
            cfg.flush()
            subprocess.run(['python3', str(ROOT / 'scripts/provision.py'), '--validate', cfg.name], check=True)

    def test_failed_native_creation_prevents_guest_commands(self):
        result = subprocess.CompletedProcess([], 0, stdout='{"status":"stopped","exitstatus":"disk full"}')
        with patch.object(worker, 'run', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                worker.provision({'mode': 'engine'}, '100', 'pve', 'UPID:pve:test')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], 'pvesh')

    def test_running_task_waits_for_success(self):
        values = [subprocess.CompletedProcess([], 0, stdout='{"status":"running"}'),
                  subprocess.CompletedProcess([], 0, stdout='{"status":"stopped","exitstatus":"OK"}')]
        with patch.object(worker, 'run', side_effect=values), patch.object(worker.time, 'sleep'):
            worker.wait_create('pve', 'UPID:pve:test')

    def test_patch_is_idempotent_and_removable(self):
        for kind, source in [('node', "use PVE::API2::LXC;\n__PACKAGE__->register_method({ path => 'lxc' });\n1;\n"),
                             ('ui', '<head>\n<script src="/pve2/js/pvemanagerlib.js?ver=9"></script>\n</head>')]:
            modified = patcher.transform(source, kind)
            self.assertEqual(patcher.transform(modified, kind), modified)
            restored = patcher.transform(modified, kind, remove=True)
            self.assertNotIn('BETTERDOCKER', restored)
            self.assertNotIn('betterdocker.js', restored)
            self.assertIn("path => 'lxc'" if kind == 'node' else 'pvemanagerlib.js', restored)

    def test_unsupported_package_refused(self):
        for kind in ['node', 'ui']:
            with self.assertRaises(RuntimeError):
                patcher.transform('unexpected package layout', kind)


if __name__ == '__main__':
    unittest.main()
