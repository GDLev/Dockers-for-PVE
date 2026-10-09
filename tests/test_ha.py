"""HA must leave Docker policies disabled and reject unavailable recovery state."""
import contextlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker
from test_guest_state import state
from test_replication import replica

ROOT = Path(__file__).resolve().parents[1]
transfer_spec = importlib.util.spec_from_file_location('transfer', ROOT / 'scripts/transfer.py')
transfer = importlib.util.module_from_spec(transfer_spec)
with patch.dict(sys.modules, {'provision': worker, 'guest_state': state}):
    transfer_spec.loader.exec_module(transfer)
spec = importlib.util.spec_from_file_location('ha', ROOT / 'scripts/ha.py')
ha = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'provision': worker, 'guest_state': state, 'replica': replica, 'transfer': transfer}):
    spec.loader.exec_module(ha)

ID = 'a' * 32


class HATests(unittest.TestCase):
    def test_boot_does_not_reclaim_ha_endpoint_from_stale_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'; (root / ID).mkdir(parents=True)
            (root / ID / 'ha.json').write_text('{}')
            ordinary = {'id': 'b' * 32, 'node': 'pve'}
            recovering = {'id': 'c' * 32, 'node': 'pve', 'ha_recovery': {'source': 'other'}}
            with patch.object(worker, 'ROOT', root), \
                    patch.object(worker, 'locked', side_effect=contextlib.nullcontext), \
                    patch.object(worker, 'summaries', return_value=[{'id': ID, 'node': 'pve'}, ordinary, recovering]), \
                    patch.object(worker, 'recover_storage_transaction'), \
                    patch.object(worker, 'recover_network_transaction', return_value=False), \
                    patch.object(worker, 'ensure_address') as address:
                worker.restore('pve')
                address.assert_called_once_with(ordinary, wait=False)

    def test_status_refresh_does_not_create_an_unowned_recovery_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'
            metadata = Path(temporary) / 'metadata'; metadata.mkdir()
            info = {'id': ID, 'node': 'pve', 'ha_recovery': {'source': 'other', 'token': 'token'}}
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'METADATA', metadata), \
                    patch.object(worker, 'summaries', return_value=[info]), patch.object(worker, 'capture', return_value=''), \
                    patch.object(worker, 'recover_network_transaction') as network, patch.object(worker, 'management') as management:
                worker.refresh('pve')
            network.assert_not_called()
            management.assert_not_called()
            self.assertFalse((root / ID).exists())

    def test_enrollment_requires_a_complete_successful_replica(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), \
                patch.object(worker, 'operation', side_effect=lambda *a, **kw: contextlib.nullcontext()), \
                patch.object(worker, 'locked', side_effect=contextlib.nullcontext), \
                patch.object(worker, 'detail', return_value={'id': ID, 'ipv4mode': 'static'}), \
                patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Complete a DCT replication'):
                ha.enroll(ID, 'pve')
            run.assert_not_called()

    def test_policy_failure_retains_enrollment_marker_before_any_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'; (root / ID).mkdir(parents=True)
            with patch.object(worker, 'ROOT', root), \
                    patch.object(worker, 'operation', side_effect=lambda *a, **kw: contextlib.nullcontext()), \
                    patch.object(worker, 'locked', side_effect=contextlib.nullcontext), \
                    patch.object(worker, 'detail', return_value={'replication': {'targets': {'other': {}}}}), \
                    patch.object(worker, 'container_records', return_value=[{'id': 'container'}]), \
                    patch.object(worker, 'run', side_effect=ValueError('Docker unavailable')):
                with self.assertRaisesRegex(ValueError, 'Docker unavailable'):
                    ha.enroll(ID, 'pve')
                self.assertTrue((root / ID / 'ha.json').exists())
                self.assertTrue((root.parent / 'restart-paused' / (ID + '.json')).exists())

    def test_release_restores_policy_even_after_lifecycle_marker_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'; (root / ID).mkdir(parents=True)
            (root / ID / 'ha.json').write_text('{}')
            with patch.object(worker, 'ROOT', root), \
                    patch.object(worker, 'operation', side_effect=lambda *a, **kw: contextlib.nullcontext()), \
                    patch.object(worker, 'locked', side_effect=contextlib.nullcontext), \
                    patch.object(worker, 'detail'), patch.object(ha, 'info', return_value={'node': 'pve'}), \
                    patch.object(worker, 'current_node', return_value='pve'), patch.object(worker, 'restore_restart_policy') as restore:
                ha.release(ID, 'pve')
                self.assertFalse((root / ID / 'ha.json').exists())
                self.assertTrue((root.parent / 'restart-paused' / (ID + '.json')).exists())
                restore.assert_called_once_with(ID)

    def test_mismatched_recovery_guid_prevents_import_or_cleanup(self):
        current = {'node': 'pve', 'ha_recovery': {'source': 'other', 'token': 'token'},
                   'replication': {'volid': 'zfs:vm-100-disk-1', 'targets': {'pve': {'snapshot': 'snap', 'guid': '1'}}}}
        with patch.object(ha, 'info', return_value=current), patch.object(worker, 'current_node', return_value='pve'), \
                patch.object(worker, 'operation', side_effect=lambda *a, **kw: contextlib.nullcontext()), \
                patch.object(replica, 'recovery_source', side_effect=ValueError('GUID mismatch')), \
                patch.object(state, 'restore_state') as restore, patch.object(transfer, 'clean_local') as cleanup:
            with self.assertRaisesRegex(ValueError, 'GUID mismatch'):
                ha.recover_guest(ID, 'pve')
            restore.assert_not_called(); cleanup.assert_not_called()

    def test_recovered_container_policy_is_disabled_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), \
                patch.object(worker, 'container_records', return_value=[{'id': 'recovered'}]), patch.object(worker, 'run') as run:
            ha.pause_restart(ID)
            run.assert_called_once_with(['docker', 'update', '--restart=no', 'recovered'], capture_output=True)
            self.assertTrue((worker.ROOT / ID / 'ha.json').exists())

    def test_normal_start_does_not_restore_docker_restart_for_ha_guest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / ID).mkdir(); (root / ID / 'ha.json').write_text('{}')
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'run') as run:
                worker.restore_restart_policy(ID)
                run.assert_not_called()

    def test_recovery_publishes_only_after_restore_and_replication_direction_change(self):
        current = {'id': ID, 'vmid': 100, 'node': 'pve', 'name': 'app', 'bridge': 'vmbr0',
                   'address': '10.0.0.10', 'cidr': '10.0.0.10/24', 'firewall': 1,
                   'ha_recovery': {'source': 'other', 'token': 'token'},
                   'replication': {'volid': 'zfs:vm-100-disk-1', 'storage': 'zfs',
                                   'targets': {'pve': {'snapshot': 'snap', 'guid': '1'}}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'; root.mkdir()
            with tempfile.TemporaryFile() as lock, patch.object(worker, 'ROOT', root), \
                    patch.object(worker, 'operation', side_effect=lambda *a, **kw: contextlib.nullcontext(lock)), \
                    patch.object(worker, 'current_node', return_value='pve'), patch.object(ha, 'info', return_value=current), \
                    patch.object(ha, 'checkpoint', return_value=current['replication']['targets']['pve']), \
                    patch.object(replica, 'export'), patch.object(state, 'unpack_state'), \
                    patch.object(state, 'restore_state') as restore, patch.object(ha, 'switch_jobs') as switch, \
                    patch.object(ha, 'publish') as publish, patch.object(worker, 'container_records', return_value=[]):
                order = []
                def restored(*args, **kwargs):
                    order.append('restore'); (root / ID).mkdir()
                    self.assertRegex(str(worker.METADATA), r'/transfers/[0-9a-f]{32}/metadata$')
                    return {'id': ID, 'node': 'pve', 'vmid': 100}
                restore.side_effect = restored
                switch.side_effect = lambda *args: order.append('switch')
                publish.side_effect = lambda *args: order.append('publish')
                ha.recover_guest(ID, 'pve')
                self.assertEqual(order, ['restore', 'switch', 'publish'])
                self.assertEqual(restore.call_args.kwargs['live_firewall'], 1)
                self.assertTrue((root / ID / 'ha.json').exists())
                self.assertTrue((root.parent / 'restart-paused' / (ID + '.json')).exists())
                self.assertEqual(list((root.parent / 'transfers').iterdir()), [])
                self.assertFalse((root.parent / 'ha-recoveries' / (ID + '.json')).exists())
                result = publish.call_args.args[2]
                self.assertNotIn('pve', result['replication']['targets'])
                self.assertEqual(result['replication']['targets']['other']['guid'], '1')
                self.assertNotIn('ha_recovery', result)

    def test_ha_waits_for_existing_kernel_operation_lock(self):
        import fcntl
        import threading
        import time
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'
            folder = root.parent / 'operations'; folder.mkdir()
            import os
            proc = root.parent / 'proc'; (proc / str(os.getpid())).mkdir(parents=True)
            (proc / str(os.getpid()) / 'stat').write_text(str(os.getpid()) + ' (python) ' + '0 ' * 19 + '1')
            with (folder / (ID + '.lock')).open('w') as held, patch.object(worker, 'ROOT', root), \
                    patch.object(worker, 'METADATA', Path(temporary) / 'metadata'), patch.object(worker, 'PROC', proc):
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'Another operation'):
                    with worker.operation(ID, 'start'):
                        self.fail('Concurrent manual operation acquired a held lock')
                release = threading.Timer(0.1, lambda: fcntl.flock(held, fcntl.LOCK_UN))
                release.start()
                try:
                    started = time.monotonic()
                    with worker.operation(ID, 'ha-start', wait_timeout=2):
                        self.assertGreaterEqual(time.monotonic() - started, 0.08)
                        self.assertTrue(worker.operation_state(ID)['active'])
                    self.assertFalse(worker.operation_state(ID)['active'])
                finally:
                    release.join()

    def test_ha_recreation_overrides_restart_without_changing_canonical_configuration(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = {'services': {'app': {'restart': 'always'}, 'db': {'restart': 'unless-stopped'}}}
            (directory / 'compose.runtime.json').write_text(json.dumps(runtime))
            self.assertEqual(worker.compose_base(directory, ID).count('-f'), 1)
            (directory / 'ha.json').write_text('{}')
            command = worker.compose_base(directory, ID)
            self.assertEqual(command.count('-f'), 2)
            self.assertEqual(json.loads((directory / '.ha-compose.json').read_text()),
                             {'services': {'app': {'restart': 'no'}, 'db': {'restart': 'no'}}})
            self.assertEqual(json.loads((directory / 'compose.runtime.json').read_text()), runtime)
