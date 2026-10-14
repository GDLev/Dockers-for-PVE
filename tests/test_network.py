import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker, BASE

IDENTIFIER = 'a' * 32
INFO = dict(BASE, id=IDENTIFIER, node='pve', interface='bdaaaaaaaaaaaa', hwaddr='02:aa:aa:aa:aa:aa',
            ipv4mode='dhcp', address='', status='creating')


class NetworkTests(unittest.TestCase):
    def test_incoming_dhcp_requires_matching_guest_and_target(self):
        import stat
        from types import SimpleNamespace
        from dctlib.network import dhcp_info
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); metadata = root / 'metadata'; metadata.mkdir()
            projects = root / 'projects'; projects.mkdir()
            token = 'b' * 32
            staged = root / 'transfers' / token / 'metadata'; staged.mkdir(parents=True)
            worker.write_json(metadata / (IDENTIFIER + '.json'), dict(INFO, vmid=100, node='source', migration=token))
            for vmid in (101, 100):
                worker.write_json(staged / (IDENTIFIER + '.json'), dict(INFO, vmid=vmid, node='pve'))
                with patch.object(worker, 'METADATA', metadata), patch.object(worker, 'ROOT', projects), \
                     patch.object(worker, 'current_node', return_value='pve'), \
                     patch('dctlib.network.os.fstat', return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_size=10000)):
                    if vmid != 100:
                        with self.assertRaisesRegex(ValueError, 'ownership differs'):
                            dhcp_info(IDENTIFIER)
                    else:
                        self.assertEqual(dhcp_info(IDENTIFIER)['node'], 'pve')

    def test_dhcp_ignores_static_fields(self):
        data = worker.validate(dict(BASE, ipv4mode='dhcp', address='', cidr=''))
        self.assertEqual(data['address'], '')
        self.assertEqual(data['ipv4mode'], 'dhcp')

    def test_static_cidr_is_normalized(self):
        data = worker.validate(dict(BASE, address='192.0.2.50/24'))
        self.assertEqual(data['address'], '192.0.2.50')
        self.assertEqual(data['cidr'], '192.0.2.50/24')

    def test_macvlan_has_own_mac_and_does_not_reconfigure_bridge(self):
        with patch.object(worker, 'capture', return_value='[]'), patch.object(worker, 'run') as run:
            worker.ensure_link(INFO)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(['ip', 'link', 'add', 'link', 'vmbr0', 'name', INFO['interface'],
                       'address', INFO['hwaddr'], 'type', 'macvlan', 'mode', 'bridge'], calls)
        for args in calls[1:]:
            self.assertIn(INFO['interface'], args)
            self.assertNotIn('vmbr0', args)

    def test_dhcp_hook_only_configures_managed_address_and_records_lease(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'summaries', return_value=[INFO]), patch.object(worker, 'current_node', return_value='pve'), \
             patch.object(worker, 'check_address'), patch.object(worker, 'add_address') as add, \
             patch.object(worker, 'run') as run:
            worker.dhcp_event({'reason': 'BOUND', 'interface': INFO['interface'],
                               'new_ip_address': '192.0.2.90', 'new_dhcp_lease_time': '3600',
                               'new_routers': '192.0.2.1', 'new_domain_name_servers': '192.0.2.1'})
            add.assert_called_once_with(INFO['interface'], '192.0.2.90')
            self.assertEqual(worker.lease(INFO)['address'], '192.0.2.90')
            run.assert_not_called()  # no gateway, routes or DNS writes

    def test_hook_rejects_management_and_unmanaged_interfaces(self):
        with patch.object(worker, 'summaries', return_value=[INFO]), patch.object(worker, 'current_node', return_value='pve'):
            with self.assertRaisesRegex(ValueError, 'unmanaged'):
                worker.dhcp_event({'reason': 'BOUND', 'interface': 'vmbr0', 'new_ip_address': '192.0.2.28'})

    def test_rebinding_updates_ports_without_reinterpolating_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            worker.write_json(directory / 'compose.runtime.json', {'services': {'web': {
                'ports': [{'published': '8080', 'target': 80, 'host_ip': '192.0.2.50'}],
                'environment': {'PASSWORD': 'a$${literal}b'}}}})
            worker.rebind_runtime(directory, '192.0.2.90')
            config = json.loads((directory / 'compose.runtime.json').read_text())['services']['web']
            self.assertEqual(config['ports'][0]['host_ip'], '192.0.2.90')
            self.assertEqual(config['environment']['PASSWORD'], 'a$${literal}b')
            with self.assertRaises(ValueError):
                worker.rebind_runtime(directory, '0.0.0.0')

    def test_dhcp_address_change_preserves_exact_active_replicas(self):
        records = [dict(id='a', service='app', number='1', state='running'),
                   dict(id='b', service='app', number='2', state='exited'),
                   dict(id='c', service='peer', number='1', state='exited')]
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'METADATA', Path(folder) / 'metadata'), \
             patch.object(worker, 'lease', return_value={'address': '192.0.2.91'}), \
             patch.object(worker, 'rebind_runtime') as rebind, patch.object(worker, 'ensure_link'), \
             patch.object(worker, 'start_dhcp'), patch.object(worker, 'restore_restart_policy'), \
             patch.object(worker, 'container_records', side_effect=[records, records, records]), \
             patch.object(worker, 'reconcile_network_runtime') as reconcile, patch.object(worker, 'run') as run:
            info = dict(INFO, status='partial', address='192.0.2.90')
            worker.sync_dhcp(info)
            rebind.assert_called_once_with(Path(folder) / IDENTIFIER, '192.0.2.91')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(['docker', 'start', 'a'], commands)
            self.assertNotIn(['docker', 'start', 'b'], commands)
            self.assertNotIn(['docker', 'start', 'c'], commands)
            reconcile.assert_called_once_with(info)
            self.assertEqual(info['address'], '192.0.2.91')

    def test_dhcp_does_not_restart_an_explicitly_stopped_project(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'METADATA', Path(folder) / 'metadata'), \
             patch.object(worker, 'lease', return_value={'address': '192.0.2.91'}), \
             patch.object(worker, 'rebind_runtime'), patch.object(worker, 'ensure_link'), \
             patch.object(worker, 'start_dhcp'), patch.object(worker, 'container_records', return_value=[]), \
             patch.object(worker, 'reconcile_network_runtime'), patch.object(worker, 'run') as run:
            worker.sync_dhcp(dict(INFO, status='stopped', address='192.0.2.90'))
            self.assertFalse(any(call.args[0][:2] == ['docker', 'start'] for call in run.call_args_list))

    def test_failed_rebind_retains_exact_resume_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'lease', return_value={'address': '192.0.2.91'}), \
             patch.object(worker, 'rebind_runtime', side_effect=OSError('Disk full')), \
             patch.object(worker, 'ensure_link'), patch.object(worker, 'start_dhcp'), \
             patch.object(worker, 'container_records', return_value=[dict(id='a', service='app', number='2', state='paused')]):
            with self.assertRaises(OSError): worker.sync_dhcp(dict(INFO, address='192.0.2.90'))
            marker = worker.ROOT / IDENTIFIER / 'network-resume.json'
            self.assertEqual(worker.read_network_resume(marker), [{'service':'app','number':'2','paused':True}])
            worker.clear_network_resume(IDENTIFIER, 'peer')
            self.assertTrue(marker.exists())
            worker.clear_network_resume(IDENTIFIER, 'app')
            self.assertFalse(marker.exists())

    def test_renewing_same_lease_does_not_recreate_instances(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'lease', return_value={'address':'192.0.2.90'}), \
             patch.object(worker, 'ensure_link'), patch.object(worker, 'start_dhcp'), \
             patch.object(worker, 'run') as run:
            worker.sync_dhcp(dict(INFO, address='192.0.2.90'))
            run.assert_not_called()

    def test_invalid_mtu_is_rejected_instead_of_truncated(self):
        for value in (1500.9, '1500.9', True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.validate(dict(BASE, mtu=value))

    def test_remove_retains_relative_bind_mount_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder) / 'projects'), \
             patch.object(worker, 'METADATA', Path(folder) / 'metadata'), \
             patch.object(worker, 'locked', tempfile.TemporaryDirectory), \
             patch.object(worker, 'detail', return_value=INFO), patch.object(worker, 'run'), patch.object(worker, 'remove_network'), patch.object(worker, 'container_records', return_value=[]):
            directory = worker.ROOT / IDENTIFIER / 'data'
            directory.mkdir(parents=True)
            (directory / 'database').write_text('valuable data')
            worker.write_json(worker.METADATA / (IDENTIFIER + '.json'), INFO)
            worker.action(IDENTIFIER, 'pve', 'delete')
            self.assertEqual((directory / 'database').read_text(), 'valuable data')
            self.assertFalse((worker.METADATA / (IDENTIFIER + '.json')).exists())
