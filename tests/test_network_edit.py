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

    def test_boot_does_not_mount_a_retained_disk_owned_by_another_node(self):
        data = self.configure()
        worker.write_json(self.directory / 'storage.json', {'volid': 'local-lvm:vm-150-disk-0'})
        worker.write_json(self.metadata / (ID + '.json'), dict(data, id=ID, node='other', status='stopped'))
        with patch.object(worker, 'storage_mount') as mount, patch.object(worker, 'recover_storage_transaction') as recover:
            worker.restore('pve')
            mount.assert_not_called()
            recover.assert_not_called()

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

    def network_environment(self):
        from contextlib import ExitStack
        import ipaddress
        stack = ExitStack()
        for name in ('check_sdn_bridge', 'check_address', 'remove_network', 'ensure_address', 'rebind_runtime', 'reconcile_network_runtime'):
            stack.enter_context(patch.object(worker, name))
        stack.enter_context(patch.object(worker, 'bridge_addresses', return_value=([], [ipaddress.IPv4Network('10.0.0.0/20')])))
        stack.enter_context(patch.object(worker, 'capture', return_value='[{"mtu":1500}]'))
        return stack

    def test_running_queue_merges_changes_revert_and_digest_without_touching_runtime(self):
        self.configure()
        source = (self.directory/'request.json').read_text()
        runtime = (self.directory/'compose.runtime.json').read_text()
        digest = worker.detail(ID, 'pve')['digest']
        with self.network_environment(), patch.object(worker, 'container_records', return_value=[{'state':'running'}]):
            worker.project_network(ID, 'pve', {'mtu':1400}, digest)
            first = worker.network_config(ID, 'pve')
            self.assertEqual(first['mtu'], 0)
            self.assertEqual(first['pending'], {'mtu':1400})
            self.assertNotEqual(first['digest'], digest)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                worker.project_network(ID, 'pve', {'mtu':1401}, digest)
            worker.project_network(ID, 'pve', {'cidr':'192.0.2.51/20'}, first['digest'])
            self.assertEqual(worker.load_pending_network(self.directory), {'cidr':'192.0.2.51/20', 'mtu':1400})
            self.assertEqual((self.directory/'request.json').read_text(), source)
            self.assertEqual((self.directory/'compose.runtime.json').read_text(), runtime)
            worker.project_network(ID, 'pve', {'revert':1}, worker.detail(ID, 'pve')['digest'])
            self.assertEqual(worker.network_config(ID, 'pve')['pending'], {})
            self.assertEqual(worker.detail(ID, 'pve')['digest'], digest)

    def test_pending_network_revalidates_rolls_back_and_remains_retryable(self):
        self.configure()
        worker.save_pending_network(self.directory, {'cidr':'192.0.2.51/20'})
        source = (self.directory/'request.json').read_text()
        with self.network_environment():
            with patch.object(worker, 'check_address', side_effect=ValueError('Address became occupied')):
                with self.assertRaisesRegex(ValueError, 'occupied'): worker.apply_pending_network(ID, 'pve')
            self.assertEqual(worker.load_pending_network(self.directory), {'cidr':'192.0.2.51/20'})
            with patch.object(worker, 'rebind_runtime', side_effect=ValueError('Interrupted write')):
                with self.assertRaisesRegex(ValueError, 'Interrupted'): worker.apply_pending_network(ID, 'pve')
            self.assertEqual((self.directory/'request.json').read_text(), source)
            self.assertEqual(worker.detail(ID, 'pve')['address'], '192.0.2.50')
            self.assertEqual(worker.load_pending_network(self.directory), {'cidr':'192.0.2.51/20'})
            self.assertFalse((self.directory/'network-transaction.json').exists())
            worker.apply_pending_network(ID, 'pve')
            self.assertEqual(worker.detail(ID, 'pve')['address'], '192.0.2.51')
            self.assertEqual(worker.load_pending_network(self.directory), {})

    def test_queue_rejects_invalid_fields_templates_and_unauthorized_revert(self):
        self.configure()
        worker.save_pending_network(self.directory, {'mtu':1400})
        for values in ({'revert':1,'mtu':1401}, {'revert':0}, {'revert':'1'}):
            with self.assertRaisesRegex(ValueError, 'Revert'): worker.project_network(ID, 'pve', values)
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='cpus'):
            with self.assertRaisesRegex(ValueError, 'Permission denied'): worker.project_network(ID, 'pve', {'revert':1})
        with self.network_environment(), patch.object(worker, 'container_records', return_value=[{'state':'running'}]):
            with self.assertRaisesRegex(ValueError, 'MTU'): worker.project_network(ID, 'pve', {'mtu':9000})
        info = json.loads((self.metadata/(ID+'.json')).read_text())
        worker.write_json(self.metadata/(ID+'.json'), dict(info, template=True))
        with self.assertRaisesRegex(ValueError, 'template'): worker.project_network(ID, 'pve', {'revert':1})
        self.assertEqual(worker.load_pending_network(self.directory), {'mtu':1400})

    def test_full_reboot_stops_every_instance_before_applying_network(self):
        self.configure()
        calls = []
        running = [{'id':'a','state':'running','service':'app'}]
        def stop(*args):
            calls.append('stop')
            running[0]['state'] = 'exited'
        def apply(*args):
            self.assertEqual(running[0]['state'], 'exited')
            calls.append('network')
        with patch.object(worker, 'container_records', return_value=running), \
             patch.object(worker, 'graceful_shutdown', side_effect=stop), \
             patch.object(worker, 'apply_pending_network', side_effect=apply), \
             patch.object(worker, 'prepare_network_start', side_effect=lambda *args: calls.append('prepare')), \
             patch.object(worker, 'reconcile_network_runtime'), patch.object(worker, 'apply_pending_settings'), \
             patch.object(worker, 'prepare_firewall'), patch.object(worker, 'restored_start', return_value=False), \
             patch.object(worker, 'restore_restart_policy'):
            worker.action(ID, 'pve', 'reboot')
        self.assertEqual(calls, ['stop','network','prepare'])
