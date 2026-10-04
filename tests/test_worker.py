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
BASE = {'name': 'web', 'mode': 'compose', 'compose': 'services:\n  web:\n    image: nginx',
        'address': '192.0.2.50', 'bridge': 'vmbr0'}
INTERFACES = [{'ifname': 'vmbr0', 'addr_info': [{'local': '192.0.2.28', 'prefixlen': 24}]}]


class WorkerTests(unittest.TestCase):
    def test_compose_does_not_require_image_or_redundant_port_setup(self):
        data = worker.validate(dict(BASE, image='', ports='invalid stale image field'))
        self.assertEqual(data['mode'], 'compose')

    def test_invalid_network_and_image_parameters(self):
        for change in [{'address': '127.0.0.1'}, {'address': '::1'}, {'bridge': '../../etc'},
                       {'mode': 'image', 'image': '--privileged'},
                       {'mode': 'image', 'image': 'nginx', 'ports': '65536:80'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                worker.validate(dict(BASE, **change))

    def test_ports_come_from_compose_and_bind_only_to_project_ip(self):
        config = {'services': {'web': {'image': 'nginx', 'ports': [
            {'published': '8080', 'target': 80, 'host_ip': '0.0.0.0'}], 'networks': {'private': {}}},
            'db': {'image': 'postgres', 'volumes': [{'source': 'database', 'target': '/data', 'type': 'volume'}]}},
            'volumes': {'database': {}}, 'networks': {'private': {}}}
        result = worker.bind_ports(config, BASE['address'], 'a' * 32)
        self.assertEqual(result['services']['web']['ports'][0]['host_ip'], '192.0.2.50')
        self.assertEqual(result['services']['web']['ports'][0]['target'], 80)
        self.assertEqual(result['volumes'], {'database': {}})
        self.assertEqual(result['networks'], {'private': {}})
        self.assertNotIn('ports', result['services']['db'])

    def test_host_network_and_duplicate_ports_rejected(self):
        for config in [{'services': {'web': {'network_mode': 'host'}}},
                       {'services': {'a': {'ports': [{'published': '80', 'target': 80}]},
                                     'b': {'ports': [{'published': '80', 'target': 81}]}}}]:
            with self.assertRaises(ValueError):
                worker.bind_ports(config, BASE['address'], 'a' * 32)

    def test_host_management_address_cannot_be_used(self):
        with patch.object(worker, 'capture', return_value=json.dumps(INTERFACES)), patch.object(Path, 'is_dir', return_value=True):
            with self.assertRaisesRegex(ValueError, 'already belongs to the host'):
                worker.network_preflight(dict(BASE, address='192.0.2.28'))

    def test_network_probe_failure_prevents_address_allocation(self):
        with patch.object(worker, 'capture', return_value=json.dumps(INTERFACES)), patch.object(Path, 'is_dir', return_value=True), \
             patch.object(worker, 'summaries', return_value=[]), \
             patch.object(worker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as probe:
            with self.assertRaisesRegex(ValueError, 'duplicate-address'):
                worker.network_preflight(BASE)
            self.assertEqual(probe.call_args.args[0][0:2], ['arping', '-D'])

    def test_prepare_preserves_literal_dollars_and_does_not_log_secrets(self):
        canonical = {'services': {'web': {'image': 'nginx', 'environment': {'SECRET': 'a${literal}b'},
                                         'ports': [{'published': '8080', 'target': 80}]}}}
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'capture', return_value=json.dumps(canonical)) as capture:
            directory = Path(folder)
            worker.prepare(dict(BASE, environment='SECRET=value'), 'a' * 32, directory)
            runtime = json.loads((directory / 'compose.runtime.json').read_text())
            self.assertEqual(runtime['services']['web']['environment']['SECRET'], 'a$${literal}b')
            self.assertEqual(runtime['services']['web']['ports'][0]['host_ip'], BASE['address'])
            self.assertNotIn('SECRET=value', ' '.join(capture.call_args.args[0]))
            self.assertEqual((directory / '.env').stat().st_mode & 0o777, 0o600)

    def test_failed_compose_validation_does_not_allocate_host_ip(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked', tempfile.TemporaryDirectory), patch.object(worker, 'current_node', return_value='pve'), \
             patch.object(worker, 'network_preflight'), patch.object(worker, 'prepare', side_effect=ValueError('Invalid YAML')), \
             patch.object(worker, 'ensure_address') as address:
            with self.assertRaisesRegex(ValueError, 'Invalid YAML'):
                worker.create(BASE, 'pve')
            address.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_invalid_update_leaves_existing_deployment_intact(self):
        identifier = 'a' * 32
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'locked', tempfile.TemporaryDirectory), \
             patch.object(worker, 'detail', return_value=dict(BASE, id=identifier, deployment=BASE)), \
             patch.object(worker, 'prepare', side_effect=ValueError('Invalid YAML')), \
             patch.object(worker, 'ensure_address') as address:
            directory = Path(folder) / identifier
            directory.mkdir()
            source = directory / 'compose.source.yaml'
            source.write_text('previous deployment')
            with self.assertRaisesRegex(ValueError, 'Invalid YAML'):
                worker.update(identifier, 'pve', BASE)
            self.assertEqual(source.read_text(), 'previous deployment')
            address.assert_not_called()

    def test_boot_restore_only_adds_addresses_for_this_node(self):
        records = [dict(BASE, node='pve'), dict(BASE, node='other', address='192.0.2.51')]
        with patch.object(worker, 'locked', tempfile.TemporaryDirectory), \
             patch.object(worker, 'summaries', return_value=records), patch.object(worker, 'ensure_address') as add:
            worker.restore('pve')
            add.assert_called_once_with(records[0], wait=False)

    def test_patch_is_idempotent_and_removable(self):
        for kind, source in [('node', "use PVE::API2::LXC;\n__PACKAGE__->register_method({ path => 'lxc' });\n1;\n"),
                             ('cluster', "__PACKAGE__->register_method({ name => 'resources' });\n1;\n"),
                             ('ui', '<head>\n<script src="/pve2/js/pvemanagerlib.js?ver=9"></script>\n</head>')]:
            modified = patcher.transform(source, kind)
            self.assertEqual(patcher.transform(modified, kind), modified)
            restored = patcher.transform(modified, kind, remove=True)
            self.assertEqual(restored.split(), source.split())

    def test_unsupported_package_refused(self):
        for kind in ['node', 'ui', 'cluster']:
            with self.assertRaises(RuntimeError):
                patcher.transform('unexpected package layout', kind)


if __name__ == '__main__':
    unittest.main()
