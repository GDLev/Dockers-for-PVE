"""Deletion must be scoped to owned data, even after guest removal."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker
from dctlib import data

ID = 'a' * 32


class DataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('ROOT', 'METADATA', 'LOCK'):
            value = self.root / {'ROOT': 'projects', 'METADATA': 'metadata', 'LOCK': 'node.lock'}[name]
            p = patch.object(worker, name, value); p.start(); self.addCleanup(p.stop)
        proc = self.root / 'proc'
        (proc / str(os.getpid())).mkdir(parents=True)
        (proc / str(os.getpid()) / 'stat').write_text('0 (test) ' + '0 ' * 21)
        patcher = patch.object(worker, 'PROC', proc); patcher.start(); self.addCleanup(patcher.stop)
        (worker.ROOT / ID).mkdir(parents=True)
        (worker.METADATA / 'ids').mkdir(parents=True)
        (worker.METADATA / 'ids/100.json').write_text(json.dumps({'id': ID, 'node': 'pve'}))
        (worker.ROOT / ID / 'compose.runtime.json').write_text('{"services":{}}')

    def test_refresh_preserves_template_and_configuration_changed_during_poll(self):
        initial = {'id': ID, 'node': 'pve', 'vmid': 100, 'status': 'stopped',
                   'expected': 1, 'template': False, 'protection': False, 'notes': 'before', 'hookscript': 'local:snippets/old.sh'}
        path = worker.METADATA / (ID + '.json')
        path.write_text(json.dumps(initial))
        (worker.ROOT / ID / 'metrics.json').write_text('{}')
        def poll(*args, **kwargs):
            current = json.loads(path.read_text())
            current.update(template=True, protection=True, notes='after', expected=2)
            current.pop('hookscript')
            path.write_text(json.dumps(current))
            return ''
        with patch.object(worker, 'capture', side_effect=poll), \
                patch.object(worker, 'operation_state', return_value={'active': False}), \
                patch.object(worker, 'sync_dhcp'), patch.object(worker, 'recover_network_transaction'):
            worker.refresh('pve')
        current = json.loads(path.read_text())
        self.assertTrue(current['template'])
        self.assertTrue(current['protection'])
        self.assertEqual((current['notes'], current['expected']), ('after', 2))
        self.assertNotIn('hookscript', current)

    def test_refresh_does_not_resurrect_configuration_deleted_before_reconciliation(self):
        previous = {'id': ID, 'node': 'pve', 'vmid': 100, 'status': 'stopped',
                    'hookscript': 'local:snippets/old.sh'}
        current = {key: value for key, value in previous.items() if key != 'hookscript'}
        path = worker.METADATA / (ID + '.json'); path.write_text(json.dumps(current))
        (worker.ROOT / ID / 'metrics.json').write_text('{}')
        def reconcile(info):
            path.write_text(json.dumps(info))
        with patch.object(worker, 'summaries', return_value=[previous]), \
                patch.object(worker, 'capture', return_value=''), \
                patch.object(worker, 'operation_state', return_value={'active': False}), \
                patch.object(worker, 'sync_dhcp', side_effect=reconcile), \
                patch.object(worker, 'recover_network_transaction'):
            worker.refresh('pve')
        self.assertNotIn('hookscript', json.loads(path.read_text()))

    def test_refresh_rechecks_active_operation_after_acquiring_metadata_lock(self):
        from contextlib import contextmanager
        info = {'id': ID, 'node': 'pve', 'vmid': 100, 'status': 'stopped'}
        path = worker.METADATA / (ID + '.json'); path.write_text(json.dumps(info))
        busy = False
        @contextmanager
        def acquired(*args, **kwargs):
            nonlocal busy
            busy = True
            yield
        with patch.object(worker, 'locked', side_effect=acquired), \
                patch.object(worker, 'operation_state', side_effect=lambda identifier: {'active': busy}), \
                patch.object(worker, 'capture', return_value=''), \
                patch.object(worker, 'sync_dhcp') as sync, \
                patch.object(worker, 'recover_network_transaction') as recover, \
                patch.object(worker, 'management') as management:
            worker.refresh('pve')
        sync.assert_not_called(); recover.assert_not_called(); management.assert_not_called()
        self.assertEqual(json.loads(path.read_text()), info)

    def test_refresh_does_not_recover_an_active_snapshot(self):
        info = {'id': ID, 'node': 'pve', 'vmid': 100, 'status': 'stopped'}
        (worker.METADATA / (ID + '.json')).write_text(json.dumps(info))
        (worker.ROOT / ID / 'snapshot-transaction.json').write_text('{"action":"create"}')
        with patch.object(worker, 'summaries', return_value=[info]), patch.object(worker, 'operation_state', return_value={'active': True}), \
             patch.object(worker, 'capture', return_value=''), patch.object(worker, 'recover_snapshot_transaction') as recover, \
             patch.object(worker, 'management') as management:
            worker.refresh('pve')
            recover.assert_not_called()
            management.assert_not_called()
            self.assertTrue((worker.ROOT / ID / 'snapshot-transaction.json').exists())

    def test_delete_keeps_guest_id_reserved_until_native_access_cleanup(self):
        import subprocess
        info = {'id': ID, 'node': 'pve', 'vmid': 100, 'name': 'owned', 'status': 'stopped'}
        metadata = worker.METADATA / (ID + '.json'); metadata.write_text(json.dumps(info))
        reservation = worker.METADATA / 'ids/100.json'
        cleanups = []
        def run(args, **kwargs):
            if args[0] == 'perl' and any('remove_vm_access' in word for word in args):
                self.assertTrue(metadata.exists())
                self.assertTrue(reservation.exists())
                self.assertEqual(args[-1], '100')
                cleanups.append(True)
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(worker, 'detail', return_value=info), patch.object(worker, 'remove_network'), \
                patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'run', side_effect=run):
            worker.action(ID, 'pve', 'delete')
        self.assertEqual(cleanups, [True])
        self.assertFalse(metadata.exists())
        self.assertFalse(reservation.exists())

    def test_failed_access_cleanup_retains_guest_id_and_owned_data(self):
        import subprocess
        info = {'id': ID, 'node': 'pve', 'vmid': 100, 'name': 'owned', 'status': 'stopped'}
        metadata = worker.METADATA / (ID + '.json'); metadata.write_text(json.dumps(info))
        marker = worker.ROOT / ID / 'data'; marker.write_text('keep')
        def run(args, **kwargs):
            if args[0] == 'perl' and any('remove_vm_access' in word for word in args):
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(worker, 'detail', return_value=info), patch.object(worker, 'remove_network'), \
                patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'run', side_effect=run), \
                patch.object(worker, 'owned_data_plan', return_value=[]), patch.object(worker, 'remove_owned_data') as remove:
            with self.assertRaises(subprocess.CalledProcessError):
                worker.action(ID, 'pve', 'delete', destroy_data=True)
        remove.assert_not_called()
        self.assertTrue(metadata.exists())
        self.assertTrue((worker.METADATA / 'ids/100.json').exists())
        self.assertEqual(marker.read_text(), 'keep')

    def test_create_publishes_actual_status_before_its_operation_finishes(self):
        import contextlib
        import shutil
        import subprocess
        from dctlib import hardening
        nodes = self.root / 'pve-nodes'; nodes.mkdir()
        for start, states, replicas, expected in ((False, ['created'], 1, 'stopped'),
                                                 (True, ['running'], 1, 'running'),
                                                 (True, ['running', 'exited'], 2, 'partial')):
            with self.subTest(start=start, states=states), contextlib.ExitStack() as controls:
                shutil.rmtree(worker.ROOT / ID, ignore_errors=True)
                (worker.METADATA / (ID + '.json')).unlink(missing_ok=True)
                controls.enter_context(patch.object(worker, 'PVE_NODES', nodes))
                controls.enter_context(patch.object(worker, 'current_node', return_value='pve'))
                controls.enter_context(patch.object(hardening, 'mode', return_value='standard'))
                controls.enter_context(patch.object(worker.project_limits, 'initialize'))
                controls.enter_context(patch.object(worker, 'prepare', return_value={'services': {'app': {'deploy': {'replicas': replicas}}}}))
                controls.enter_context(patch.object(worker, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')))
                controls.enter_context(patch.object(worker, 'container_records', return_value=[{'id': str(i) * 64, 'state': state} for i, state in enumerate(states, 1)]))
                for name in ('network_preflight', 'ensure_address', 'rebind_runtime', 'prepare_firewall',
                             'finish_network_start', 'guest_hook', 'expect_guest_hook'):
                    controls.enter_context(patch.object(worker, name))
                with worker.operation(ID, 'create'):
                    worker.create({'name': 'owned', 'bridge': 'vmbr0', 'address': '0.0.0.0'}, 'pve', ID, start=start)
                    self.assertTrue(worker.operation_state(ID)['active'])
                    self.assertEqual(json.loads((worker.METADATA / (ID + '.json')).read_text())['status'], expected)

    def test_attached_and_foreign_disks_never_reach_free(self):
        (worker.METADATA / (ID + '.json')).write_text('{"node":"pve"}')
        with patch.object(worker, 'detail', return_value={}), patch.object(worker, 'storage_config', return_value={'volid': 'local:current', 'unused': ['local:old']}), patch.object(worker, 'pve_storage') as free:
            for volume in ('local:current', 'local:foreign'):
                with self.assertRaisesRegex(ValueError, 'attached or unowned'):
                    worker.delete_storage_volume(100, 'pve', volume)
            free.assert_not_called()

    def test_protection_and_snapshots_block_cleanup(self):
        with self.assertRaises(ValueError): data.check_data_dependencies(ID, {'protection': True})
        folder = worker.snapshot_root(ID) / 'before'; folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'snapshots'): data.check_data_dependencies(ID)

    def test_node_ownership_cannot_be_bypassed(self):
        with self.assertRaisesRegex(ValueError, 'another node'): data.storage_owner(100, 'other')

    def test_shared_volume_is_not_removed(self):
        volume = {'Driver': 'local', 'Labels': {'com.docker.compose.project': 'bd-' + ID}}
        with patch.object(worker, 'capture', side_effect=['private', json.dumps([volume]), 'foreign-container']), patch.object(worker, 'container_records', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'shared'): data.owned_data_plan(ID, {})

    def test_volume_backed_by_external_storage_is_retained(self):
        volume = {'Driver': 'local', 'Options': {'device': '/external'}}
        with patch.object(worker, 'capture', side_effect=['external', json.dumps([volume]), '{"filesystems":[]}']), \
             patch.object(worker, 'container_records', return_value=[]):
            self.assertEqual(data.owned_data_plan(ID, {}), [])

    def test_external_volume_is_retained_even_with_project_label(self):
        (worker.ROOT / ID / 'compose.runtime.json').write_text(json.dumps({'volumes': {'db': {'external': True, 'name': 'shared-db'}}}))
        with patch.object(worker, 'capture', side_effect=['shared-db', '{"filesystems":[]}']), patch.object(worker, 'container_records', return_value=[]):
            self.assertEqual(data.owned_data_plan(ID, {}), [])

    def test_retained_disk_freed_once_and_reservation_released(self):
        settings = {'volid': 'local:owned', 'vmid': 100, 'node': 'pve'}
        (worker.ROOT / ID / 'storage.json').write_text(json.dumps(settings))
        with patch.object(worker, 'pve_storage') as free, patch.object(worker, 'storage_unmount'), patch.object(worker, 'capture', return_value='fixture.mount'), patch.object(worker, 'run'):
            worker.delete_storage_volume(100, 'pve', 'local:owned')
            self.assertEqual([c.args[1] for c in free.call_args_list], ['free-check', 'free'])
            self.assertFalse((worker.METADATA / 'ids/100.json').exists())
            with self.assertRaises(FileNotFoundError): worker.delete_storage_volume(100, 'pve', 'local:owned')

    def test_cleanup_of_a_local_migration_copy_keeps_remote_guest_reservation(self):
        (worker.METADATA / 'ids/100.json').write_text(json.dumps({'id': ID, 'node': 'destination'}))
        (worker.METADATA / (ID + '.json')).write_text('{"node":"destination"}')
        (worker.ROOT / ID / 'storage.json').write_text(json.dumps({'vmid': 100, 'node': 'pve', 'volid': 'local:old'}))
        with patch.object(worker, 'pve_storage'), patch.object(worker, 'storage_unmount'), \
             patch.object(worker, 'capture', return_value='fixture.mount'), patch.object(worker, 'run'):
            worker.delete_storage_volume(100, 'pve', 'local:old')
        self.assertTrue((worker.METADATA / 'ids/100.json').exists())
        self.assertTrue((worker.METADATA / (ID + '.json')).exists())

    def test_unused_disks_keep_guest_id_reserved(self):
        (worker.ROOT / ID / 'storage.json').write_text('{"unused":["local:old"]}')
        worker.release_id(ID)
        self.assertTrue((worker.METADATA / 'ids/100.json').exists())
