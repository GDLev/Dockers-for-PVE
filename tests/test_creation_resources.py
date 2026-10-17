"""Initial resource overrides follow the same policy as existing service settings."""
import contextlib
import copy
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from test_worker import worker, BASE
from dctlib import hardening, operation_capacity

ID = 'a' * 32
MIB = 1048576


class CreationResourceTests(TestCase):
    def setUp(self):
        self.controls = contextlib.ExitStack()
        self.addCleanup(self.controls.close)
        self.folder = Path(self.controls.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.controls.enter_context(patch.object(operation_capacity, 'ROOT', self.folder / 'operation-slots'))
        self.controls.enter_context(patch.object(operation_capacity, 'POLICY', self.folder / 'operation-policy.json'))
        self.config = {'services': {
            'web': {'image': 'nginx', 'cpus': 2, 'mem_limit': 256 * MIB, 'memswap_limit': 512 * MIB,
                    'restart': 'on-failure', 'dns': ['192.0.2.53'], 'environment': {'TOKEN': 'not-for-preview'}},
            'db': {'image': 'postgres', 'cpus': .5, 'mem_limit': 128 * MIB, 'deploy': {'replicas': 2}},
        }}
        self.controls.enter_context(patch.object(worker, 'ROOT', self.folder))
        self.controls.enter_context(patch.object(worker, 'trusted_management', return_value=True))
        self.controls.enter_context(patch.object(worker, 'capture', side_effect=lambda *a, **k: json.dumps(copy.deepcopy(self.config))))
        self.controls.enter_context(patch.object(hardening, 'mode', return_value='standard'))
        self.controls.enter_context(patch.object(hardening, 'apply'))
        self.controls.enter_context(patch.object(worker, 'apply_storage'))
        self.controls.enter_context(patch.object(worker.project_limits, 'apply_defaults'))
        self.budget = self.controls.enter_context(patch.object(worker.project_limits, 'check_directory'))
        self.controls.enter_context(patch.object(worker.node_runtime, 'check_resources'))
        self.controls.enter_context(patch.object(worker, 'rootfs_quota_support', return_value={'supported': False}))

    def prepare(self, **changes):
        return worker.prepare(worker.validate(dict(BASE, **changes)), ID, self.folder / ID)

    def test_cpu_override_preserves_other_limits_and_other_services(self):
        result = self.prepare(resources={'web': {'cpus': .75}})
        web, db = result['services']['web'], result['services']['db']
        self.assertEqual(web['cpus'], .75)
        self.assertEqual(web['mem_limit'], 256 * MIB)
        self.assertEqual(web['memswap_limit'], 512 * MIB)
        self.assertEqual(web['dns'], ['192.0.2.53'])
        self.assertEqual(web['restart'], 'on-failure')
        self.assertEqual(db['cpus'], .5)
        self.assertEqual(db['deploy']['replicas'], 2)
        self.assertEqual(self.budget.call_args.args[1]['services']['web']['cpus'], .75)

    def test_memory_and_swap_are_applied_before_budget_admission(self):
        result = self.prepare(resources={'web': {'memory': 96, 'swap': 32}, 'db': {'memory': 64, 'swap': 0}})
        self.assertEqual(result['services']['web']['mem_limit'], 96 * MIB)
        self.assertEqual(result['services']['web']['memswap_limit'], 128 * MIB)
        self.assertEqual(result['services']['db']['memswap_limit'], 64 * MIB)
        self.budget.side_effect = ValueError('Project resource budget exceeded')
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.prepare(resources={'web': {'cpus': 8192}})

    def test_invalid_or_unknown_resources_are_rejected(self):
        for resources in ([], {'web': {'privileged': True}}, {'web': {'memory': -1}},
                          {'web': {'cpus': True}}, {'web': {'cpus': float('nan')}},
                          {'web': {'cpuunits': 1}}, {'web': {'memory': 0, 'swap': 32}}):
            with self.subTest(resources=resources), self.assertRaises(ValueError):
                worker.validate(dict(BASE, resources=resources))
        with self.assertRaisesRegex(ValueError, 'Unknown Compose service'):
            self.prepare(resources={'missing': {'cpus': 1}})
        with self.assertRaisesRegex(ValueError, 'Writable-layer quotas'):
            self.prepare(resources={'web': {'disk': 512}})

    def test_omitted_overrides_keep_source_limits(self):
        result = self.prepare()
        self.assertEqual(result['services']['web']['cpus'], 2)
        self.assertEqual(result['services']['web']['mem_limit'], 256 * MIB)
        self.assertFalse((self.folder / ID / 'settings.json').exists())

    def test_preview_exposes_limits_without_environment_or_host_paths(self):
        rows = worker.preview(dict(BASE))
        self.assertEqual(rows[0]['resources']['memory'], 256)
        self.assertFalse(rows[0]['rootfs_quota'])
        self.assertNotIn('not-for-preview', json.dumps(rows))
        self.assertNotIn(str(self.folder), json.dumps(rows))

    def test_creation_defaults_only_fill_unspecified_limits(self):
        self.config = {'services': {'app': {'image': 'busybox'}}}
        row = worker.preview(dict(BASE), resources_only=True)[0]
        self.assertEqual(row['resource_defaults'], ['cpus', 'memory', 'swap'])
        self.config['services']['app'].update(cpus=0, mem_limit=0, memswap_limit=0)
        row = worker.preview(dict(BASE), resources_only=True)[0]
        self.assertEqual(row['resource_defaults'], [])
        self.config['services']['app'] = {'image': 'busybox', 'deploy': {'resources': {'limits': {'cpus': .5, 'memory': 128 * MIB}}}}
        row = worker.preview(dict(BASE), resources_only=True)[0]
        self.assertEqual(row['resource_defaults'], [])

    def test_resource_preview_needs_no_network_but_create_still_validates_it(self):
        data = {key: value for key, value in BASE.items() if key not in ('bridge', 'address')}
        self.config['services']['web']['ports'] = [{'target': 80, 'published': '8080'}]
        rows = worker.preview(data, resources_only=True)
        self.assertEqual(rows[0]['resources']['memory'], 256)
        self.assertEqual(rows[0]['ports'][0]['host_ip'], '0.0.0.0')
        with self.assertRaisesRegex(ValueError, 'Linux bridge'):
            worker.validate(data)

    def test_image_data_disk_uses_a_private_named_mount(self):
        disk = {'storage': 'local-lvm', 'size_mib': 256, 'target': '/var/lib/app'}
        data = worker.validate(dict(BASE, mode='image', image='busybox:1.37', data_disk=disk))
        source = json.loads(worker.image_compose(data))
        self.assertEqual(source['services']['app']['volumes'], [{'type': 'volume', 'source': 'data', 'target': '/var/lib/app'}])
        self.assertEqual(source['volumes'], {'data': {}})
        for invalid in ({**disk, 'storage': '../local'}, {**disk, 'size_mib': True},
                        {**disk, 'size_mib': 255}, {**disk, 'target': '/'},
                        {**disk, 'target': '/data/../etc'}, {**disk, 'target': '/proc/x'}):
            with self.subTest(disk=invalid), self.assertRaises(ValueError):
                worker.validate(dict(BASE, mode='image', image='busybox', data_disk=invalid))

    def test_data_disk_rejects_missing_external_or_foreign_mounts_before_allocation(self):
        disk = {'storage': 'local-lvm', 'size_mib': 256}
        with self.assertRaisesRegex(ValueError, 'requires a writable'):
            self.prepare(data_disk=disk)
        self.config['services']['web']['volumes'] = [{'type': 'volume', 'source': 'data', 'target': '/data'}]
        self.config['volumes'] = {'data': {'external': True}}
        with self.assertRaisesRegex(ValueError, 'private local named volumes'):
            self.prepare(data_disk=disk)
        self.config['volumes'] = {'data': {'name': 'another-project-data'}}
        with self.assertRaisesRegex(ValueError, 'private local named volumes'):
            self.prepare(data_disk=disk)
        self.config['services']['web']['volumes'] = [{'type': 'bind', 'source': '/srv/shared', 'target': '/data'}]
        with self.assertRaisesRegex(ValueError, 'private project data directories'):
            self.prepare(data_disk=disk)
