"""Mandatory policy through mutation boundaries with mocked Docker responses."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import hardening as h, images, settings, service_recreate
import guest_state

ID = 'a' * 32
IMAGE = 'sha256:' + 'b' * 64


class HardenedMutations(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.directory = self.base / ID; self.directory.mkdir()
        (self.directory / h.FILE).write_text(json.dumps({'mode': 'hardened'}))
        self.stack.enter_context(patch.object(w, 'ROOT', self.base))
        self.stack.enter_context(patch.object(w, 'locked'))
        self.selected = SimpleNamespace(rootless=False, mode='rootful')
        self.stack.enter_context(patch.object(w.node_runtime, 'load', side_effect=lambda: self.selected))
        self.stack.enter_context(patch.object(h, 'available'))
        self.engine = self.stack.enter_context(patch.object(w, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')))
        self.stack.enter_context(patch.object(w, 'trusted_management', return_value=True))

    def assert_policy(self, config):
        service = config['services']['app']
        self.assertIn('ALL', service['cap_drop'])
        self.assertLessEqual(set(service['cap_add']), h.CAPS)
        self.assertIn('no-new-privileges:true', service['security_opt'])
        profiles = [value for value in service['security_opt'] if value.startswith('apparmor')]
        self.assertEqual(profiles, [] if self.selected.rootless else ['apparmor:' + h.PROFILE])

    def test_yaml_canonicalization_keeps_required_flags_in_both_modes(self):
        for rootless in (False, True):
            self.selected.rootless = rootless
            config = {'services': {'app': {'image': 'nginx'}}}
            h.defaults(config)
            h.apply(config, self.selected)
            self.assert_policy(config)
            self.assertEqual(h.apply(copy.deepcopy(config), self.selected), config)

    def test_pending_and_internal_settings_refuse_removed_flags_before_writes(self):
        for rootless in (False, True):
            self.selected.rootless = rootless
            for changes in ({'no_new_privileges': False}, {'cap_drop': []}, {'apparmor': 'unconfined'}):
                with self.subTest(rootless=rootless, changes=changes), patch.object(w, 'load_pending', return_value={'app': changes}), patch.object(w, 'write_json') as write, patch.object(w, 'require_stopped') as stopped:
                    with self.assertRaisesRegex(ValueError, 'Mandatory'):
                        settings.apply_pending_settings(ID, 'pve')
                    with self.assertRaisesRegex(ValueError, 'Mandatory'):
                        settings._service_settings(ID, 'pve', 'app', changes)
                    write.assert_not_called(); stopped.assert_not_called()
        self.engine.assert_not_called()

    def image_plan(self, config):
        (self.directory / 'compose.runtime.json').write_text(json.dumps(config))
        plan = {'phase': 'preview', 'token': 'fixture', 'digest': 'same', 'created': time.time(), 'containers': [],
                'changes': [{'service': 'app', 'candidate': IMAGE, 'image': 'nginx', 'changed': True}]}
        (self.directory / 'image-update.json').write_text(json.dumps(plan))
        for name, value in (('detail', {'digest': 'same'}), ('container_records', []), ('load_pending', {}), ('load_pending_network', {})):
            self.stack.enter_context(patch.object(w, name, return_value=value))
        for name in ('assert_configuration_digest', 'require_stopped', 'snapshot_name', 'refresh', 'validate_delegated_config'):
            self.stack.enter_context(patch.object(w, name))
        self.stack.enter_context(patch.object(images, 'inspect', return_value={'Id': IMAGE}))
        return self.stack.enter_context(patch.object(w, 'project_snapshot'))

    def test_image_update_refuses_disabled_protection_before_snapshot(self):
        snapshot = self.image_plan({'services': {'app': {'image': 'nginx', 'security_opt': ['no-new-privileges:false']}}})
        with self.assertRaisesRegex(ValueError, 'Incompatible hardened'):
            images.apply(ID, 'pve', 'fixture', 'recovery', 'same')
        snapshot.assert_not_called(); self.engine.assert_not_called()
        self.assertFalse((self.directory / 'images.json').exists())

    def test_image_update_writes_hardened_config_and_keeps_guest_stopped(self):
        for rootless in (False, True):
            self.selected.rootless = rootless
            self.image_plan({'services': {'app': {'image': 'nginx'}}})
            images.apply(ID, 'pve', 'fixture', 'recovery', 'same')
            config = json.loads((self.directory / 'compose.runtime.json').read_text())
            self.assert_policy(config)
            self.assertIn('--no-start', self.engine.call_args.args[0])
            self.assertEqual(json.loads((self.directory / h.FILE).read_text())['mode'], 'hardened')

    def test_transaction_checks_candidate_before_journal_or_resources(self):
        candidate = {'services': {'app': {'security_opt': ['no-new-privileges:false']}}}
        prepare = unittest.mock.Mock()
        with patch('dctlib.capacity.docker_space') as space:
            with self.assertRaisesRegex(ValueError, 'Incompatible hardened'):
                service_recreate.execute({'id': ID}, '*', [], candidate, {}, {}, {}, prepare=prepare)
            space.assert_not_called(); prepare.assert_not_called()
        self.assertFalse((self.directory / service_recreate.JOURNAL).exists())
        self.engine.assert_not_called()

    def test_preserved_body_retains_extra_capability_drops(self):
        body = {'HostConfig': {'CapDrop': ['ALL', 'CHOWN'], 'CapAdd': ['CHOWN', 'SETUID'], 'NetworkMode': 'none'}}
        h.host(body, self.selected, 'hardened')
        self.assertEqual(body['HostConfig']['CapAdd'], ['SETUID'])
        self.assertEqual(body['HostConfig']['CapDrop'], ['ALL', 'CHOWN'])

    def test_clone_and_restore_engine_boundary_enforces_destination_policy(self):
        for rootless in (False, True):
            self.selected.rootless = rootless
            body = {'Labels': {'betterdocker.id': ID}, 'HostConfig': {'NetworkMode': 'none'}}
            with patch.object(w.node_runtime, 'request', return_value={'Id': 'fixture'}) as request:
                w.docker_engine('POST', '/containers/create?name=fixture', body)
                actual = request.call_args.args[2]['HostConfig']
                self.assertIn('ALL', actual['CapDrop'])
                self.assertIn('no-new-privileges', actual['SecurityOpt'])
                self.assertLessEqual(set(actual['CapAdd']), h.CAPS)
                self.assertEqual([value for value in actual['SecurityOpt'] if value.startswith('apparmor')], [] if rootless else ['apparmor=' + h.PROFILE])

    def test_preserved_body_refuses_unrepresented_privileged_settings(self):
        for extra in ({'Sysctls': {'fixture': '1'}}, {'DeviceCgroupRules': ['fixture']}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'incompatible'):
                h.host({'HostConfig': extra}, self.selected, 'hardened')

    def test_saved_managed_profile_adapts_to_rootless_without_mutating_source(self):
        config = h.apply({'services': {'app': {'image': 'nginx'}}}, self.selected)
        before = copy.deepcopy(config)
        self.selected.rootless = True
        result = h.preserved(config, self.selected, 'hardened')
        self.assert_policy(result); self.assertEqual(config, before)

    def test_saved_rootless_policy_gets_managed_rootful_profile(self):
        config = h.apply({'services': {'app': {'image': 'nginx'}}}, SimpleNamespace(rootless=True))
        self.assert_policy(h.preserved(config, self.selected, 'hardened'))

    def test_raw_rootless_yaml_does_not_gain_saved_profile_exception(self):
        self.selected.rootless = True
        config = {'services': {'app': {'security_opt': ['apparmor:' + h.PROFILE]}}}
        with self.assertRaisesRegex(ValueError, 'unsupported AppArmor'):
            h.apply(config, self.selected)

    def test_restore_adapts_policy_before_allocating_destination(self):
        config = h.apply({'services': {'app': {'image': 'nginx'}}}, self.selected)
        self.selected.rootless = True
        container = {'Config': {'Labels': {'betterdocker.id': ID}}, 'HostConfig': {'CapDrop': ['ALL'], 'CapAdd': []}}
        def read(path):
            return [container] if Path(path).name == 'containers.json' else config
        with patch.object(guest_state, 'check_state', return_value={'id': ID, 'mounts': []}), patch.object(guest_state, 'read', side_effect=read), patch.object(h, 'mode', return_value='hardened'), patch.object(w, 'current_node', return_value='pve'), patch('service_security.check') as check, patch.object(w, 'validate_delegated_config') as validate, patch.object(w, 'validate_delegated_containers'), patch.object(w, 'validate_delegated_networks'), patch.object(w, 'write_json') as write:
            with self.assertRaisesRegex(ValueError, 'wrong node'):
                guest_state.restore_state(self.directory, 'c' * 32, 'other', {})
            self.assert_policy(check.call_args.args[0])
            self.assert_policy(validate.call_args.args[0])
            write.assert_not_called(); self.engine.assert_not_called()

    def test_rollback_preflight_keeps_stronger_current_policy(self):
        folder = self.base / 'snapshot'; configs = folder / 'configuration'; configs.mkdir(parents=True)
        (configs / h.FILE).write_text(json.dumps({'mode': 'standard'}))
        (configs / 'compose.runtime.json').write_text(json.dumps({'services': {'app': {'image': 'nginx'}}}))
        (folder / 'containers.json').write_text('[]')
        for rootless in (False, True):
            self.selected.rootless = rootless
            with patch.object(w, 'snapshot_check', return_value=(folder, {'mounts': []})), patch.object(w, 'detail', return_value={}), patch.object(w, 'snapshot_containers', return_value=[]), patch.object(w, 'snapshot_mounts'), patch.object(w, 'validate_delegated_config') as validate, patch.object(w, 'validate_delegated_containers'), patch.object(w.node_runtime, 'check_resources', side_effect=RuntimeError('preflight complete')), patch.object(w, 'write_json') as write:
                with self.assertRaisesRegex(RuntimeError, 'preflight complete'):
                    w.snapshot_apply(ID, 'pve', 'fixture', check_only=True)
                self.assert_policy(validate.call_args.args[0])
                write.assert_not_called(); self.engine.assert_not_called()
        self.assertEqual(h.mode(self.directory), 'hardened')

    def test_rollback_checks_saved_container_before_changes(self):
        folder = self.base / 'snapshot'; configs = folder / 'configuration'; configs.mkdir(parents=True)
        (configs / 'compose.runtime.json').write_text(json.dumps({'services': {'app': {'image': 'nginx'}}}))
        (folder / 'containers.json').write_text(json.dumps([{'Config': {}, 'HostConfig': {'Sysctls': {'fixture': '1'}}}]))
        with patch.object(w, 'snapshot_check', return_value=(folder, {'mounts': []})), patch.object(w, 'detail', return_value={}), patch.object(w, 'snapshot_containers', return_value=[]), patch.object(w, 'snapshot_mounts'), patch.object(w, 'write_json') as write:
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                w.snapshot_apply(ID, 'pve', 'fixture', check_only=True)
            write.assert_not_called(); self.engine.assert_not_called()
