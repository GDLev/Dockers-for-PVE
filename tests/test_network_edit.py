"""Network transactions must roll back before any workload can be started."""
import json
import os
import unittest
from unittest.mock import patch
import test_delegation as delegation
from test_worker import BASE
ID, worker = delegation.ID, delegation.worker
refresh_network = worker.refresh

class NetworkEditTests(unittest.TestCase):
    setUp = delegation.DelegationTests.setUp
    tearDown = delegation.DelegationTests.tearDown

    def configure(self):
        data = worker.validate(dict(BASE, address='192.0.2.50/20', hwaddr='02:aa:aa:aa:aa:aa'))
        worker.write_json(self.directory/'request.json', data)
        worker.write_json(self.metadata/(ID+'.json'), dict(data, id=ID, node='pve', status='stopped', interface='bddddddddddddd'))
        return data

    def test_firewall_toggle_preserves_containers_and_rolls_back_failed_apply(self):
        self.configure()
        with patch.object(worker, 'firewall_check') as validate, patch.object(worker, 'remove_network') as remove:
            worker.project_network(ID, 'pve', {'firewall': 1})
            validate.assert_called_once_with(ID, 'pve')
            remove.assert_not_called()
        self.assertEqual(worker.detail(ID, 'pve')['firewall'], 1)
        with patch.object(worker, 'run', side_effect=[ValueError('rejected rules'), None]), \
                patch.object(worker, 'remove_network') as remove:
            with self.assertRaisesRegex(ValueError, 'rejected rules'):
                worker.project_network(ID, 'pve', {'firewall': 0})
            remove.assert_not_called()
        self.assertEqual(worker.detail(ID, 'pve')['firewall'], 1)
        self.assertEqual(json.loads((self.directory / 'request.json').read_text())['firewall'], 1)

    def test_network_write_requires_network_field_privileges(self):
        self.configure()
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='dns,dns_search'):
            with self.assertRaisesRegex(ValueError, 'Permission denied'):
                worker.project_network(ID, 'pve', {'cidr':'192.0.2.51/20'})

    def test_running_stale_unsupported_and_restored_writes_do_nothing(self):
        self.configure()
        before = (self.directory/'request.json').read_text()
        for key in ('ip6', 'tag', 'rate', 'net1'):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                worker.project_network(ID, 'pve', {key:1})
        with self.assertRaisesRegex(ValueError, 'checksum'):
            worker.project_network(ID, 'pve', {'cidr':'192.0.2.51/20'}, 'a'*64)
        with patch.object(worker, 'container_records', return_value=[{'state':'running'}]):
            with self.assertRaises(ValueError): worker.project_network(ID, 'pve', {'mtu':1400})
        (self.directory/'snapshot-restored.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'restored'):
            worker.project_network(ID, 'pve', {'mtu':1400})
        self.assertEqual((self.directory/'request.json').read_text(), before)

    def test_interrupted_transaction_restores_source_metadata_and_runtime(self):
        self.configure()
        files = {name:(self.directory/name).read_text() for name in ('request.json','compose.runtime.json')}
        metadata = json.loads((self.metadata/(ID+'.json')).read_text())
        worker.write_json(self.directory/'network-transaction.json', {'metadata':metadata,'files':files})
        for name in files: (self.directory/name).write_text('{}')
        worker.write_json(self.metadata/(ID+'.json'), dict(metadata, address='192.0.2.99'))
        with patch.object(worker, 'remove_network'), patch.object(worker, 'ensure_address'):
            worker.recover_network_transaction(ID, 'pve')
        for name, content in files.items(): self.assertEqual((self.directory/name).read_text(), content)
        self.assertEqual(worker.detail(ID,'pve')['address'], '192.0.2.50')
        self.assertFalse((self.directory/'network-transaction.json').exists())
        self.assertTrue((self.directory/'network-reconcile.json').exists())

    def test_recovery_refuses_cross_node_or_running_instances(self):
        data = self.configure()
        worker.write_json(self.directory/'network-transaction.json', {'metadata':dict(data,id=ID,node='pve'),'files':{}})
        with patch.object(worker, 'remove_network') as remove, patch.object(worker, 'run', return_value=type('Result', (), {'returncode':0})()):
            with self.assertRaises(ValueError): worker.recover_network_transaction(ID, 'other')
            with patch.object(worker, 'container_records', return_value=[{'state':'running'}]):
                with self.assertRaises(ValueError): worker.recover_network_transaction(ID, 'pve')
            remove.assert_not_called()

    def test_new_boot_discards_dhcp_resume_instead_of_bypassing_onboot(self):
        self.configure()
        marker = self.directory/'network-resume.json'
        worker.write_json(marker, {'boot_id':'previous','instances':[{'service':'app','number':'1'}]})
        info = dict(worker.detail(ID,'pve'), ipv4mode='dhcp', network_resume=True)
        with patch.object(worker, 'network_boot_id', return_value='current'), patch.object(worker, 'ensure_link'), \
             patch.object(worker, 'start_dhcp'), patch.object(worker, 'lease', return_value=None), \
             patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'run') as run:
            worker.sync_dhcp(info)
            run.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(info['status'],'stopped')

    def test_boot_recovery_can_restore_addresses_before_docker_starts(self):
        self.configure()
        metadata = json.loads((self.metadata/(ID+'.json')).read_text())
        worker.write_json(self.directory/'network-transaction.json', {'metadata':metadata,'files':{}})
        with patch.object(worker, 'run', return_value=type('Result', (), {'returncode':3})()), \
             patch.object(worker, 'container_records', side_effect=AssertionError('Docker is not running')), \
             patch.object(worker, 'remove_network'), patch.object(worker, 'ensure_address'):
            self.assertTrue(worker.recover_network_transaction(ID,'pve'))

    def test_one_failed_dhcp_project_does_not_block_other_status_refresh(self):
        self.configure()
        peer = 'e'*32
        (self.root/peer).mkdir()
        worker.write_json(self.metadata/(peer+'.json'), {'id':peer,'node':'pve','status':'stopped'})
        for identifier in (ID,peer):worker.write_json(self.root/identifier/'metrics.json', [])
        from contextlib import redirect_stderr
        import io
        with patch.object(worker,'sync_dhcp',side_effect=[ValueError('Bad lease'),True]) as sync, \
             patch.object(worker,'capture',return_value=''),redirect_stderr(io.StringIO()):
            refresh_network('pve')
        self.assertEqual(sync.call_count,2)
        states={row['id']:row['status'] for row in worker.summaries()}
        self.assertEqual(sorted(states.values()),['network-error','stopped'])

    def test_existing_snapshots_block_endpoint_changes_before_any_write(self):
        self.configure()
        manifest=worker.snapshot_root(ID)/'before'/'manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
        before=(self.directory/'request.json').read_text()
        with patch.object(worker,'remove_network') as remove:
            with self.assertRaisesRegex(ValueError,'Remove snapshots'):
                worker.project_network(ID,'pve',{'cidr':'192.0.2.51/20'})
            remove.assert_not_called()
        self.assertEqual((self.directory/'request.json').read_text(),before)
