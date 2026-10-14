"""Protection blocks destructive configuration writes, but permits power and rollback."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_worker import worker

ID = 'f' * 32

class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.metadata = base / 'projects', base / 'metadata'
        self.directory = self.root / ID
        self.directory.mkdir(parents=True)
        self.metadata.mkdir()
        self.patches = [patch.object(worker, 'ROOT', self.root), patch.object(worker, 'METADATA', self.metadata),
                        patch.object(worker, 'current_node', return_value='pve'), patch.object(worker, 'locked')]
        for item in self.patches:
            item.start()
        worker.write_json(self.directory / 'request.json', {'name': 'protection-test'})
        worker.write_json(self.metadata / (ID + '.json'), {'id': ID, 'node': 'pve', 'status': 'stopped'})
        self.config = {'services': {'app': {'image': 'busybox:1.37', 'volumes': []}}}
        worker.write_json(self.directory / 'compose.runtime.json', self.config)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_default_off_enable_digest_stale_disable_and_persistence(self):
        original = worker.project_protection(ID, 'pve')
        self.assertEqual(original['protection'], 0)
        enabled = worker.project_protection(ID, 'pve', '1', original['digest'])
        self.assertNotEqual(enabled['digest'], original['digest'])
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            worker.project_protection(ID, 'pve', '0', original['digest'])
        self.assertEqual(worker.detail(ID, 'pve')['protection'], 1)
        with self.assertRaisesRegex(ValueError, 'Protection must'):
            worker.project_protection(ID, 'pve', 'true')
        disabled = worker.project_protection(ID, 'pve', '0', enabled['digest'])
        self.assertEqual(disabled, original)

    def test_delete_refused_before_docker_or_filesystem_mutations(self):
        worker.project_protection(ID, 'pve', '1')
        with patch.object(worker, 'run') as run, patch.object(worker, 'container_records') as records:
            with self.assertRaisesRegex(ValueError, 'protection mode enabled'):
                worker.action(ID, 'pve', 'delete')
            run.assert_not_called()
            records.assert_not_called()
        self.assertTrue((self.metadata / (ID + '.json')).exists())

    def test_layout_guards_removal_replacement_mounts_and_replicas(self):
        info = {'protection': 1}
        for proposed in [ {'services': {}}, {'services': {'app': {'image': 'nginx', 'volumes': []}}},
                         {'services': {'app': {'image': 'busybox:1.37', 'volumes': [{'target': '/data'}]}}},
                         {'services': {'app': {'image': 'busybox:1.37', 'deploy': {'replicas': 2}}}} ]:
            with self.subTest(proposed=proposed), self.assertRaisesRegex(ValueError, 'protection mode enabled'):
                worker.check_protected_layout(info, self.config, proposed)
        worker.check_protected_layout(info, self.config, {'services': {'app': {'image': 'busybox:1.37', 'volumes': [], 'cpus': .5, 'dns': ['1.1.1.1']}}})
        worker.check_protected_layout({}, self.config, {'services': {}})

    def test_disk_requests_and_pending_application_refused_without_losing_queue(self):
        worker.project_protection(ID, 'pve', '1')
        worker.write_json(self.directory / 'pending.json', {'app': {'disk': 256}})
        original = (self.directory / 'compose.runtime.json').read_text()
        with patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'run') as run:
            values = dict(worker.service_values(self.config['services']['app']), disk=256)
            with self.assertRaisesRegex(ValueError, 'protection mode enabled'):
                worker._service_settings(ID, 'pve', 'app', values)
            with self.assertRaisesRegex(ValueError, 'protection mode enabled'):
                worker.apply_pending_settings(ID, 'pve')
            with self.assertRaisesRegex(ValueError, 'protection mode enabled'):
                worker.project_storage_limit(ID, 'pve', 256)
            run.assert_not_called()
        self.assertEqual((self.directory / 'compose.runtime.json').read_text(), original)
        self.assertEqual(worker.load_pending(self.directory), {'app': {'disk': 256}})
