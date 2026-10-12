"""Pending changes are separate from active settings and are applied at explicit start."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker

ID = 'f' * 32


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / ID
        self.directory.mkdir()
        self.config = {'services': {'app': {'cpus': 1, 'mem_limit': 32 * 1048576,
            'memswap_limit': 64 * 1048576, 'cpu_shares': 1024, 'pids_limit': 100,
            'dns': [], 'dns_search': [], 'restart': 'no'}, 'peer': {'restart': 'no'}}}
        self.runtime = self.directory / 'compose.runtime.json'
        self.runtime.write_text(json.dumps(self.config))
        self.values = worker.service_values(self.config['services']['app'])
        self.context = contextlib.ExitStack()
        for name, value in [('ROOT', self.root), ('detail', None), ('locked', None), ('refresh', None)]:
            self.context.enter_context(patch.object(worker, name, value) if name == 'ROOT' else patch.object(worker, name))
        self.containers = self.context.enter_context(patch.object(worker, 'container_records', return_value=[
            {'id': 'a', 'service': 'app', 'state': 'running'}, {'id': 'b', 'service': 'peer', 'state': 'running'}]))
        self.live = self.context.enter_context(patch.object(worker, 'update_live_containers'))
        self.run = self.context.enter_context(patch.object(worker, 'run'))

    def tearDown(self):
        self.context.close()
        self.tmp.cleanup()

    def save(self, **changes):
        worker.service_settings(ID, 'pve', 'app', dict(self.values, **changes))

    def test_dns_is_pending_without_runtime_or_container_change(self):
        original = self.runtime.read_bytes()
        self.save(dns=['1.1.1.1'])
        self.assertEqual(self.runtime.read_bytes(), original)
        self.assertEqual(worker.load_pending(self.directory), {'app': {'dns': ['1.1.1.1']}})
        self.live.assert_not_called()
        self.run.assert_not_called()
        row = next(r for r in worker.pending_settings(ID, 'pve', 'app') if r['key'] == 'dns')
        self.assertEqual(row, {'key': 'dns', 'value': [], 'pending': ['1.1.1.1']})

    def test_mixed_edit_applies_memory_and_queues_unlimited_cpu(self):
        self.save(memory=64, cpus=0)
        self.assertEqual(worker.load_settings(self.directory)['app']['memory'], 64)
        self.assertEqual(worker.load_settings(self.directory)['app']['cpus'], 1)
        self.assertEqual(worker.load_pending(self.directory), {'app': {'cpus': 0}})
        args = self.live.call_args.args[1]
        self.assertIn('--memory', args)
        self.assertNotIn('--cpus', args)
        self.assertEqual(self.live.call_args.args[0], [{'id': 'a', 'service': 'app', 'state': 'running'}])

    def test_live_failure_retains_requested_values_pending(self):
        original = self.runtime.read_bytes()
        self.live.side_effect = ValueError('Docker failed')
        with self.assertRaisesRegex(ValueError, 'Docker failed'):
            self.save(memory=64, dns=['1.1.1.1'])
        self.assertEqual(self.runtime.read_bytes(), original)
        self.assertEqual(worker.load_settings(self.directory), {})
        self.assertEqual(worker.load_pending(self.directory)['app'], {'memory': 64, 'dns': ['1.1.1.1']})

    def test_paused_instance_queues_even_live_settings(self):
        self.containers.return_value[0]['state'] = 'paused'
        self.save(memory=64)
        self.live.assert_not_called()
        self.assertEqual(worker.load_pending(self.directory)['app'], {'memory': 64})

    def test_revert_changes_only_selected_pending_value(self):
        self.save(dns=['1.1.1.1'], cpus=0)
        worker.revert_settings(ID, 'pve', 'app', 'dns')
        self.assertEqual(worker.load_pending(self.directory), {'app': {'cpus': 0}})
        self.run.assert_not_called()
        worker.revert_settings(ID, 'pve', 'app', 'cpus')
        self.assertFalse((self.directory / 'pending.json').exists())
        self.assertEqual(worker.load_settings(self.directory), {})

    def test_revert_invalid_field_does_not_change_queue(self):
        self.save(dns=['1.1.1.1'])
        original = (self.directory / 'pending.json').read_bytes()
        for key in ('image', '../request.json', 'dns,unknown'):
            with self.assertRaises(ValueError):
                worker.revert_settings(ID, 'pve', 'app', key)
            self.assertEqual((self.directory / 'pending.json').read_bytes(), original)

    def test_start_applies_only_selected_stopped_service(self):
        worker.save_pending(self.directory, {'app': {'dns': ['1.1.1.1']}, 'peer': {'dns': ['8.8.8.8']}})
        self.containers.return_value = [self.containers.return_value[1]]
        worker.apply_pending_settings(ID, 'pve', 'app')
        self.assertEqual(worker.load_pending(self.directory), {'peer': {'dns': ['8.8.8.8']}})
        self.assertEqual(self.run.call_args.args[0][-4:], ['up', '--no-start', '--no-deps', 'app'])
        self.assertEqual(worker.load_settings(self.directory)['app']['dns'], ['1.1.1.1'])

    def test_failed_apply_keeps_queue_and_current_file(self):
        worker.save_pending(self.directory, {'app': {'dns': ['1.1.1.1']}})
        self.containers.return_value = [self.containers.return_value[1]]
        original = self.runtime.read_bytes()
        self.run.side_effect = ValueError('Recreation failed')
        with self.assertRaisesRegex(ValueError, 'Recreation failed'):
            worker.apply_pending_settings(ID, 'pve', 'app')
        self.assertEqual(worker.load_pending(self.directory)['app'], {'dns': ['1.1.1.1']})
        self.assertEqual(self.runtime.read_bytes(), original)
        self.assertEqual(worker.load_settings(self.directory), {})

    def test_save_current_values_cancels_old_pending(self):
        self.save(dns=['1.1.1.1'])
        self.save()
        self.assertEqual(worker.load_pending(self.directory), {})

    def test_disk_pending_can_be_reverted_when_source_has_no_disk_limit(self):
        worker.save_pending(self.directory, {'app': {'disk': 512}})
        worker.revert_settings(ID, 'pve', 'app', 'disk')
        self.assertEqual(worker.load_pending(self.directory), {})

    def test_foreign_service_cannot_be_reverted(self):
        self.save(dns=['1.1.1.1'])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            worker.revert_settings(ID, 'pve', 'missing', 'dns')
        self.assertEqual(worker.load_pending(self.directory)['app'], {'dns': ['1.1.1.1']})

    def test_cpu_weight_applies_even_when_cpu_limit_waits(self):
        self.save(cpus=0, cpuunits=512)
        self.assertEqual(worker.load_pending(self.directory), {'app': {'cpus': 0}})
        self.assertEqual(worker.load_settings(self.directory)['app']['cpuunits'], 512)
        self.assertEqual(self.live.call_args.args[1], ['--cpu-shares', '512'])

    def test_wrong_node_revert_checks_identity_before_mutation(self):
        self.save(dns=['1.1.1.1'])
        original=(self.directory/'pending.json').read_bytes()
        with patch.object(worker, 'detail', side_effect=ValueError('Wrong node')):
            with self.assertRaisesRegex(ValueError, 'Wrong node'):
                worker.revert_settings(ID, 'other', 'app', 'dns')
        self.assertEqual(original,(self.directory/'pending.json').read_bytes())
