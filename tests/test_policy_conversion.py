"""Policy conversion with a fake Engine and disposable control/data fixtures."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_service_recreate import SwapTests, ID, worker as w
from dctlib import hardening as h


class PolicyConversion(unittest.TestCase):
    docker_run = SwapTests.docker_run
    engine = SwapTests.engine
    assert_originals = SwapTests.assert_originals

    def setUp(self):
        SwapTests.setUp(self)
        self.metadata = self.root / 'metadata'; self.metadata.mkdir()
        self.info = {'id': ID, 'node': 'pve', 'digest': 'fixture', 'name': 'conversion'}
        w.write_json(self.metadata / (ID + '.json'), self.info)
        w.write_json(self.directory / 'pending.json', {})
        w.write_json(self.directory / 'settings.json', {'web': {'no_new_privileges': False, 'cap_drop': [], 'apparmor': ''}})
        self.selected = SimpleNamespace(rootless=False)
        for item in (patch.object(w, 'METADATA', self.metadata), patch.object(w, 'operation'), patch.object(w, 'detail', return_value=self.info), patch.object(w, 'check_protection'), patch.object(w, 'assert_configuration_digest'), patch.object(w, 'refresh'), patch.object(w, 'trusted_management', return_value=True), patch.object(w.node_runtime, 'load', side_effect=lambda: self.selected), patch.object(w.node_runtime, 'service', return_value=subprocess.CompletedProcess([], 0, '', '')), patch.object(h, 'available')):
            item.start(); self.addCleanup(item.stop)
        self.data = self.directory / 'data'; self.data.mkdir()
        (self.data / 'retained.txt').write_text('fixture data')
        for row in self.rows.values():
            row['HostConfig']['Binds'] = [str(self.data) + ':/application:rw']
            row['Config']['Env'] = ['FIXTURE=value']

    def test_pending_service_or_network_changes_block_before_commits(self):
        for filename, changes in (('pending.json', {'web': {'read_only': True}}), ('pending-network.json', {'mtu': 1400})):
            path = self.directory / filename
            w.write_json(path, changes)
            with self.assertRaisesRegex(ValueError, 'pending changes'):
                h.convert(ID, 'pve', 'hardened', 'fixture')
            self.assertEqual(w.load_pending(self.directory), changes if filename == 'pending.json' else {})
            self.assertEqual(self.calls, [])
            w.write_json(path, {})

    def test_running_guest_blocks_before_commits(self):
        with patch.object(w, 'container_records', return_value=[{'id': '1' * 64, 'state': 'running', 'service': 'web'}]):
            with self.assertRaisesRegex(ValueError, 'Stop the affected'):
                h.convert(ID, 'pve', 'hardened', 'fixture')
        self.assertEqual(self.calls, [])

    def test_stale_digest_blocks_before_commits(self):
        with patch.object(w, 'assert_configuration_digest', side_effect=ValueError('stale digest')):
            with self.assertRaisesRegex(ValueError, 'stale digest'):
                h.convert(ID, 'pve', 'hardened', 'old')
        self.assertEqual(self.calls, [])

    def test_rootful_conversion_preserves_bind_and_anonymous_mount_requests(self):
        h.convert(ID, 'pve', 'hardened', 'fixture')
        self.verify_conversion()

    def test_rootless_conversion_preserves_bind_and_anonymous_mount_requests(self):
        self.selected.rootless = True
        h.convert(ID, 'pve', 'hardened', 'fixture')
        self.verify_conversion()

    def verify_conversion(self):
        self.assertEqual({row['HostConfig']['Mounts'][0]['Source'] for row in self.rows.values()}, {'anonymous-1', 'anonymous-2'})
        for row in self.rows.values():
            host = row['HostConfig']
            self.assertEqual(host['Binds'], [str(self.data) + ':/application:rw'])
            self.assertEqual(row['Config']['Env'], ['FIXTURE=value'])
            self.assertIn('no-new-privileges', host['SecurityOpt'])
            self.assertEqual([option for option in host['SecurityOpt'] if option.startswith('apparmor')], [] if self.selected.rootless else ['apparmor=' + h.PROFILE])
        self.assertEqual((self.data / 'retained.txt').read_text(), 'fixture data')
        commits = [index for index, args in enumerate(self.calls) if args[1] == 'commit']
        renames = [index for index, args in enumerate(self.calls) if args[1] == 'rename']
        self.assertEqual(len(commits), 2)
        self.assertLess(max(commits), min(renames))
        saved = w.load_settings(self.directory)['web']
        self.assertTrue(saved['no_new_privileges'])
        self.assertIn('ALL', saved['cap_drop'])
        self.assertEqual(saved['apparmor'], '' if self.selected.rootless else h.PROFILE)
        self.assertEqual(h.mode(self.directory), 'hardened')
        self.assertEqual(w.load_pending(self.directory), {})

    def test_failed_conversion_restores_policy_configuration_and_mounts(self):
        before = (self.directory / 'settings.json').read_bytes()
        self.fail_at = 2
        with self.assertRaisesRegex(ValueError, 'Injected create failure'):
            h.convert(ID, 'pve', 'hardened', 'fixture')
        self.assert_originals()
        self.assertEqual(h.mode(self.directory), 'standard')
        self.assertEqual((self.directory / 'settings.json').read_bytes(), before)
        self.assertEqual((self.data / 'retained.txt').read_text(), 'fixture data')

    def test_preview_reports_pending_running_and_actual_host_incompatibility(self):
        w.write_json(self.directory / 'pending.json', {'web': {'read_only': True}})
        self.rows['1' * 64]['HostConfig']['Sysctls'] = {'fixture': '1'}
        value = h.state(ID, 'pve')
        self.assertTrue(value['pending'])
        self.assertFalse(value['running'])
        self.assertTrue(any('incompatible' in issue for issue in value['incompatible']))
        self.assertEqual(value['digest'], 'fixture')
        self.assertEqual(self.calls, [])
