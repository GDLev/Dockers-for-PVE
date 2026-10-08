"""Delegated configuration writes must stay within granted fields under the worker lock."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_worker import worker

ID = 'd' * 32

class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.metadata = base / 'projects', base / 'metadata'
        self.directory = self.root / ID
        self.directory.mkdir(parents=True)
        self.metadata.mkdir()
        self.patches = [patch.object(worker, 'ROOT', self.root), patch.object(worker, 'METADATA', self.metadata),
                        patch.object(worker, 'current_node', return_value='pve'), patch.object(worker, 'locked'),
                        patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'run'),
                        patch.object(worker, 'refresh')]
        for item in self.patches:
            item.start()
        worker.write_json(self.directory / 'request.json', {'name': 'delegation-test'})
        worker.write_json(self.metadata / (ID + '.json'), {'id': ID, 'node': 'pve', 'status': 'stopped'})
        worker.write_json(self.directory / 'compose.runtime.json', {'services': {'app': {'image': 'busybox:1.37', 'cpus': .2,
            'mem_limit': 33554432, 'memswap_limit': 67108864, 'restart': 'no'}}})

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def effective(self):
        return {row['key']: row.get('pending', row['value']) for row in worker.pending_settings(ID, 'pve', 'app')}

    def test_partial_cpu_save_preserves_other_fields_and_updates_digest(self):
        before = self.effective()
        digest = worker.detail(ID, 'pve')['digest']
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='cpus,cpuunits'):
            worker.service_settings(ID, 'pve', 'app', {'cpus': .5}, digest)
        expected = dict(before, cpus=.5)
        self.assertEqual(self.effective(), expected)
        self.assertNotEqual(worker.detail(ID, 'pve')['digest'], digest)

    def test_full_legacy_payload_allows_unchanged_fields_but_denies_extra_changes(self):
        before = self.effective()
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='cpus,cpuunits'):
            worker.service_settings(ID, 'pve', 'app', dict(before, cpus=.3))
            current = self.effective()
            for changes in ({'memory': 64}, {'dns': ['1.1.1.1']}, {'disk': 0, 'restart': 'always'}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Permission denied'):
                    worker.service_settings(ID, 'pve', 'app', dict(current, **changes))
            self.assertEqual(self.effective(), current)
            self.assertEqual(worker.load_pending(self.directory), {})

    def test_revert_requires_every_requested_field_permission(self):
        pending = {'app': {'cpus': 0, 'memory': 64, 'dns': ['1.1.1.1']}}
        worker.save_pending(self.directory, pending)
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='cpus,cpuunits'):
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                worker.revert_settings(ID, 'pve', 'app', 'cpus,memory')
            self.assertEqual(worker.load_pending(self.directory), pending)
            worker.revert_settings(ID, 'pve', 'app', 'cpus')
        self.assertEqual(worker.load_pending(self.directory), {'app': {'memory': 64, 'dns': ['1.1.1.1']}})

    def test_no_digest_legacy_request_still_checks_fields_under_lock(self):
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='memory,swap'):
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                worker.service_settings(ID, 'pve', 'app', {'cpus': .8})
        self.assertEqual(self.effective()['cpus'], .2)

    def test_config_validation_and_stale_digest_precede_all_writes(self):
        worker.project_config(ID, 'pve', {'description': 'old notes', 'tags': 'test', 'protection': 1})
        original = (self.metadata / (ID + '.json')).read_text()
        for change in ({'description': 'new notes', 'tags': 'bad tag'}, {'description': 'new notes', 'protection': 2}):
            with self.assertRaises(ValueError):
                worker.project_config(ID, 'pve', change)
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            worker.project_config(ID, 'pve', {'description': 'new notes'}, '0' * 64)
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='cpus,cpuunits'):
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                worker.project_config(ID, 'pve', {'description': 'new notes'})
        self.assertEqual((self.directory / 'notes.txt').read_text(), 'old notes')
        self.assertEqual((self.metadata / (ID + '.json')).read_text(), original)

    def test_metadata_write_failure_restores_notes(self):
        worker.project_config(ID, 'pve', {'description': 'old notes'})
        with patch.object(worker, 'write_json', side_effect=OSError('metadata unavailable')):
            with self.assertRaisesRegex(OSError, 'metadata unavailable'):
                worker.project_config(ID, 'pve', {'description': 'new notes', 'protection': 1})
        self.assertEqual((self.directory / 'notes.txt').read_text(), 'old notes')
        self.assertFalse(worker.detail(ID, 'pve').get('protection'))

    def test_notes_and_tags_participate_in_optimistic_concurrency(self):
        first = worker.detail(ID, 'pve')['digest']
        worker.project_config(ID, 'pve', {'description': 'notes'}, first)
        second = worker.detail(ID, 'pve')['digest']
        self.assertNotEqual(first, second)
        worker.project_config(ID, 'pve', {'tags': 'test'}, second)
        self.assertNotEqual(second, worker.detail(ID, 'pve')['digest'])

    def test_native_tag_separator_round_trip_preserves_digest(self):
        worker.project_config(ID, 'pve', {'tags': 'web,Demo;web'})
        self.assertEqual(worker.project_tags(ID, 'pve')['tags'], 'demo;web')
        digest = worker.detail(ID, 'pve')['digest']
        worker.project_config(ID, 'pve', {'tags': 'demo;web'}, digest)
        self.assertEqual(worker.detail(ID, 'pve')['digest'], digest)
        worker.project_tags(ID, 'pve', 'web,demo')
        self.assertEqual(worker.detail(ID, 'pve')['digest'], digest)
