"""Boot ordering must not expose host hooks or restart unrelated/stopped instances."""
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import test_delegation as delegation
ID, worker = delegation.ID, delegation.worker

class LifecycleTests(unittest.TestCase):
    setUp = delegation.DelegationTests.setUp
    tearDown = delegation.DelegationTests.tearDown
    def boot_file(self, boot='same'):
        proc = self.root.parent / 'proc'
        path = proc / 'sys/kernel/random/boot_id'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(boot)
        return patch.object(worker, 'PROC', proc)

    def test_startup_validation_digest_and_configuration_permissions(self):
        original = worker.detail(ID, 'pve')['digest']
        worker.project_config(ID, 'pve', {'onboot': 1, 'startup': '3,up=2,down=5'}, original)
        self.assertEqual(worker.lifecycle_config(ID, 'pve'), {'onboot': 1, 'startup': 'order=3,up=2,down=5', 'hookscript': ''})
        current = worker.detail(ID, 'pve')['digest']
        self.assertNotEqual(current, original)
        for changes in ({'onboot': 2}, {'startup': 'order=-1'}, {'startup': 'up=1;id'}, {'hookscript':'host.sh'}):
            with self.assertRaises(ValueError):worker.project_config(ID, 'pve', changes)
        import os
        with patch.dict(os.environ, BETTERDOCKER_CONFIG_KEYS='memory,swap'):
            with self.assertRaisesRegex(ValueError,'Permission denied'):
                worker.project_config(ID, 'pve', {'onboot':0})
        self.assertEqual(worker.detail(ID, 'pve')['digest'], current)
        worker.project_config(ID, 'pve', {'onboot':0,'startup':''}, current)
        self.assertEqual(worker.detail(ID, 'pve')['digest'], original)

    def journal(self, boot='same'):
        state={'boot_id':boot,'containers':[
            {'id':'a'*64,'project':ID,'state':'running','policy':{'Name':'always'}},
            {'id':'b'*64,'project':ID,'state':'exited','policy':{'Name':'on-failure','MaximumRetryCount':3}},
            {'id':'c'*64,'project':'e'*32,'state':'running','policy':{'Name':'always'}}]}
        path=self.root.parent/'lifecycle.json';worker.write_json(path,state)
        return path

    def test_daemon_resume_preserves_stopped_and_foreign_instances(self):
        path=self.journal()
        with self.boot_file(),patch.object(worker,'container_records',return_value=[{'id':'a'*64},{'id':'b'*64}]),patch.object(worker,'run') as run:
            worker.daemon_resume('pve')
        starts=[call.args[0] for call in run.call_args_list if call.args[0][1]=='start']
        self.assertEqual(starts,[['docker','start','a'*64]])
        self.assertIn(['docker','update','--restart=on-failure:3','b'*64],[call.args[0] for call in run.call_args_list])
        self.assertFalse(path.exists())

    def test_new_boot_leaves_start_to_pve(self):
        path=self.journal('previous')
        with self.boot_file(),patch.object(worker,'run') as run:worker.daemon_resume('pve')
        run.assert_not_called();self.assertFalse(path.exists())

    def test_failed_resume_retains_recovery_journal(self):
        path=self.journal()
        with self.boot_file(),patch.object(worker,'container_records',return_value=[{'id':'a'*64}]),patch.object(worker,'run',side_effect=OSError('Docker unavailable')):
            with self.assertRaises(OSError):worker.daemon_resume('pve')
        self.assertTrue(path.exists())

    def test_node_shutdown_forces_only_after_timeout_when_requested(self):
        for force in (0,1):
            with patch.object(worker,'operation'),patch.object(worker,'action',side_effect=[ValueError('Shutdown timed out.'),None]) as action:
                if force:worker.lifecycle_stop(ID,'pve',1,force)
                else:
                    with self.assertRaises(ValueError):worker.lifecycle_stop(ID,'pve',1,force)
                self.assertEqual(action.call_count,2 if force else 1)
        with patch.object(worker,'operation'),patch.object(worker,'action',side_effect=ValueError('Another operation')) as action:
            with self.assertRaisesRegex(ValueError,'Another operation'):worker.lifecycle_stop(ID,'pve',1,1)
            self.assertEqual(action.call_count,1)

    def test_shutdown_checks_runtime_even_after_failed_deployment(self):
        info=json.loads((self.metadata/(ID+'.json')).read_text());info['status']='error'
        worker.write_json(self.metadata/(ID+'.json'),info)
        with patch.object(worker,'container_records',return_value=[{'state':'running'}]):
            self.assertEqual(worker.access_state(ID,'pve')['active_containers'],1)
        self.assertEqual(worker.access_state(ID,'pve')['active_containers'],0)

    def test_context_state_contains_action_restrictions_without_deployment_secrets(self):
        info = {'status': 'stopped', 'template': True, 'protection': 1,
                'migration': {'target': 'pve2'}, 'replication': {'targets': {'pve2': {}}},
                'deployment': {'environment': 'secret'}}
        with patch.object(worker, 'detail', return_value=info), \
             patch.object(worker, 'load_pending_network', return_value={'mtu': 1400}), \
             patch.object(worker, 'load_pending', return_value={'app': {'memory': 128}}):
            state = worker.access_state(ID, 'pve')
        self.assertTrue(state['template'])
        self.assertTrue(state['replication'])
        self.assertTrue(state['pending_settings'])
        self.assertEqual(state['migration'], {'target': 'pve2'})
        self.assertEqual(state['pending_network'], {'mtu': 1400})
        self.assertNotIn('deployment', state)
