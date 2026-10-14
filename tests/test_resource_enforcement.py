"""Budget mutation boundaries with ordinary temporary files and mocked Engine reads."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, Mock

from test_worker import worker as w
from dctlib import limits, hardening, settings, service_recreate, service_start
import guest_state
import transfer

ID = 'a' * 32
SOURCE = 'b' * 32


def service(cpus=1):
    return {'image': 'nginx', 'cpus': cpus, 'mem_limit': 64 * 1048576, 'pids_limit': 16}


def container(name='app', cpus=1, identifier='one'):
    return {'Id': identifier, 'Config': {'Labels': {'com.docker.compose.service': name}},
            'HostConfig': {'NanoCpus': int(cpus * 1000000000), 'Memory': 64 * 1048576, 'PidsLimit': 16}}


class ResourceEnforcement(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.directory = self.root / ID; self.directory.mkdir()
        self.config = {'services': {'app': service()}}
        (self.directory / 'limits.json').write_text(json.dumps(limits.DEFAULTS['project']))
        (self.directory / 'compose.runtime.json').write_text(json.dumps(self.config))
        self.stack.enter_context(patch.object(w, 'ROOT', self.root))
        self.stack.enter_context(patch.object(w, 'load_pending', return_value={}))
        self.stack.enter_context(patch.object(w, 'trusted_management', return_value=True))
        self.stack.enter_context(patch.object(w.node_runtime, 'load', return_value=SimpleNamespace(rootless=False, mode='rootful')))
        self.stack.enter_context(patch.object(w, 'container_records', return_value=[{'id': 'one', 'service': 'app', 'state': 'exited'}]))
        self.rows = [container()]
        self.capture = self.stack.enter_context(patch.object(w, 'capture', side_effect=lambda args: json.dumps(self.rows)))
        self.engine = self.stack.enter_context(patch.object(w.node_runtime, 'run'))
        self.stack.enter_context(patch.object(limits, 'POLICY', self.root / 'node-policy.json'))

    def test_live_caps_and_actual_replica_count_are_checked(self):
        limits.preflight(ID)
        self.rows[0]['HostConfig']['NanoCpus'] = 5000000000
        with self.assertRaisesRegex(ValueError, 'budget exceeded'): limits.preflight(ID)
        self.rows[:] = [container(cpus=.1, identifier=str(index)) for index in range(17)]
        with self.assertRaisesRegex(ValueError, 'replica count'): limits.preflight(ID)
        self.engine.assert_not_called()

    def test_pending_combination_is_checked_before_inspection(self):
        with patch.object(w, 'load_pending', return_value={'app': {'memory': 8192}}):
            with self.assertRaisesRegex(ValueError, 'budget exceeded'): limits.preflight(ID)
        self.capture.assert_not_called(); self.engine.assert_not_called()

    def test_actual_unknown_service_is_rejected(self):
        self.rows[:] = [container(name='other')]
        with self.assertRaisesRegex(ValueError, 'does not match'): limits.preflight(ID)

    def test_hotplug_uses_actual_caps_of_unchanged_services(self):
        self.config['services']['db'] = service()
        self.rows.append(container('db', 3.5, 'two'))
        with self.assertRaisesRegex(ValueError, 'live containers'): limits.preflight(ID, self.config, service='app')
        self.rows[1]['HostConfig']['NanoCpus'] = 2000000000
        limits.preflight(ID, self.config, service='app')
        self.engine.assert_not_called()

    def test_replacement_checks_proposed_engine_body_before_journal_or_space(self):
        body = {'Labels': {'com.docker.compose.service': 'app'}, 'HostConfig': {'NanoCpus': 0, 'Memory': 67108864, 'PidsLimit': 16}}
        entries = [{'source': container(), 'body': body}]
        with patch('dctlib.capacity.docker_space') as space:
            with self.assertRaisesRegex(ValueError, 'finite'):
                service_recreate.execute({'id': ID}, '*', entries, copy.deepcopy(self.config), {}, {}, {})
        space.assert_not_called(); self.engine.assert_not_called()
        self.assertFalse((self.directory / service_recreate.JOURNAL).exists())
        self.assertEqual(json.loads((self.directory / 'compose.runtime.json').read_text()), self.config)

    def test_compose_start_boundary_refuses_live_drift_before_command(self):
        self.rows[0]['HostConfig']['NanoCpus'] = 5000000000
        with self.assertRaisesRegex(ValueError, 'budget exceeded'):
            w.run(['docker', 'compose', '-f', str(self.directory / 'compose.runtime.json'), 'up', '-d'])
        self.engine.assert_not_called()

    def test_preserved_start_and_pending_apply_refuse_over_budget(self):
        self.rows[0]['HostConfig']['Memory'] = 8192 * 1048576
        with patch.object(hardening, 'check_start'):
            with self.assertRaisesRegex(ValueError, 'budget exceeded'): service_start.start(ID)
        with patch.object(w, '_service_settings') as change:
            with self.assertRaisesRegex(ValueError, 'budget exceeded'): settings.apply_pending_settings(ID, 'pve')
            change.assert_not_called()
        self.engine.assert_not_called()

    def test_delegated_import_does_not_inherit_exemption_or_larger_budget(self):
        (self.directory / 'limits.json').write_text(json.dumps(dict(limits.DEFAULTS['project'], cpus=8, exempt=True)))
        with patch.object(w, 'trusted_management', return_value=False):
            ceilings, defaults = limits.import_policy(self.directory, self.config, [container()], new_guest=True)
            self.assertEqual(ceilings, limits.DEFAULTS['project'])
            self.assertEqual(defaults, limits.DEFAULTS['container'])
            with self.assertRaisesRegex(ValueError, 'finite'):
                limits.import_policy(self.directory, self.config, [{'Config': container()['Config'], 'HostConfig': {}}], new_guest=True)
        self.assertTrue(limits.load(self.directory)['exempt'])
        ceilings, _ = limits.import_policy(self.directory, self.config, [container(cpus=8)], new_guest=False)
        self.assertTrue(ceilings['exempt'])
        self.engine.assert_not_called()

    def test_restore_refuses_saved_engine_caps_before_guest_writes(self):
        (self.root / 'configuration').mkdir()
        for name, value in (('compose.runtime.json', self.config), ('limits.json', limits.DEFAULTS['project'])):
            (self.root / 'configuration' / name).write_text(json.dumps(value))
        (self.root / 'containers.json').write_text(json.dumps([container(cpus=5)]))
        with patch.object(guest_state, 'check_state', return_value={'id': SOURCE}), patch.object(hardening, 'preserved', return_value=self.config), patch.object(w, 'write_json') as write:
            with self.assertRaisesRegex(ValueError, 'budget exceeded'):
                guest_state.restore_state(self.root, ID, 'pve', {})
            write.assert_not_called()
        self.engine.assert_not_called()

    def test_migration_refuses_live_drift_before_stopping_or_transfer(self):
        self.rows[0]['HostConfig']['NanoCpus'] = 5000000000
        with patch.object(w, 'operation'), patch.object(w, 'locked'), patch.object(w, 'detail', return_value={}), patch.object(w, 'assert_configuration_digest'), patch.object(w, 'load_pending_network', return_value={}), patch.object(transfer, 'check_runtime'), patch.object(w, 'recover_guest_hook'), patch.object(w, 'active_network_instances', return_value=[]), patch.object(w, 'write_json') as write, patch.object(w, 'action') as action:
            with self.assertRaisesRegex(ValueError, 'budget exceeded'):
                transfer.migrate(ID, 'pve', 'other', '')
            write.assert_not_called(); action.assert_not_called()
        self.engine.assert_not_called()

    def test_explicit_exception_and_legacy_guests_do_not_acquire_limits(self):
        (self.directory / 'limits.json').write_text(json.dumps({'exempt': True}))
        limits.preflight(ID)
        (self.directory / 'limits.json').unlink()
        limits.preflight(ID)
        self.capture.assert_not_called(); self.engine.assert_not_called()

    def test_scaling_and_replacement_count_all_proposed_replicas(self):
        candidate = copy.deepcopy(self.config)
        candidate['services']['app']['deploy'] = {'replicas': 4}
        entries = [{'source': container(), 'body': dict(container(cpus=1)['Config'], HostConfig=container(cpus=1)['HostConfig'])}]
        entries.extend({'body': copy.deepcopy(entries[0]['body'])} for _ in range(3))
        limits.preflight(ID, candidate, entries=entries)
        candidate['services']['app']['deploy']['replicas'] = 5
        with self.assertRaisesRegex(ValueError, 'budget exceeded'): limits.preflight(ID, candidate, entries=entries)
        candidate['services']['app']['cpus'] = .5
        entries.append({'body': copy.deepcopy(entries[0]['body'])})
        with self.assertRaisesRegex(ValueError, 'budget exceeded'): limits.preflight(ID, candidate, entries=entries)
        self.engine.assert_not_called()

    def test_clone_checks_source_before_export(self):
        self.rows[0]['HostConfig']['Memory'] = 8192 * 1048576
        with patch.object(guest_state, 'operation'), patch.object(w, 'locked'), patch.object(guest_state, 'capture_state') as capture:
            with self.assertRaisesRegex(ValueError, 'budget exceeded'):
                guest_state.clone(ID, 'pve', SOURCE, {'full': True})
            capture.assert_not_called()
        self.engine.assert_not_called()

    def test_partial_pending_changes_keep_other_caps_and_cpu_quota(self):
        config = {'services': {'app': {'cpu_quota': 50000, 'cpu_period': 100000, 'mem_limit': '64m', 'pids_limit': 16}}}
        with patch.object(w, 'load_pending', return_value={'app': {'memory': 128}}):
            queued = limits.pending_config(config, self.directory)
        totals = limits.check(queued, limits.DEFAULTS['project'])
        self.assertEqual(totals, {'cpus': .5, 'memory': 128, 'pids': 16})
        self.assertEqual(config['services']['app']['mem_limit'], '64m')
