"""Creation defaults and administrator exceptions with mocked runtime calls."""
from contextlib import ExitStack
import copy
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import limits, hardening, parsing

ID = 'a' * 32


class ResourceDefaults(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.directory = self.base / 'projects' / ID
        self.directory.mkdir(parents=True)
        self.stack.enter_context(patch.object(limits, 'POLICY', self.base / 'node-policy.json'))
        self.stack.enter_context(patch.object(w, 'ROOT', self.directory.parent))
        self.stack.enter_context(patch.object(w, 'trusted_management', return_value=False))
        self.selected = SimpleNamespace(rootless=False)
        self.stack.enter_context(patch.object(w.node_runtime, 'load', side_effect=lambda: self.selected))
        self.stack.enter_context(patch.object(w.node_runtime, 'check_resources'))
        self.stack.enter_context(patch.object(hardening, 'available'))
        self.engine = self.stack.enter_context(patch.object(w, 'run'))

    def test_delegated_creation_snapshots_defaults_and_budget(self):
        limits.initialize(self.directory)
        self.assertEqual(limits.load(self.directory), limits.DEFAULTS['project'])
        config = {'services': {'app': {'image': 'nginx'}}}
        limits.apply_defaults(config, self.directory)
        self.assertEqual(config['services']['app']['cpus'], 1)
        self.assertEqual(config['services']['app']['mem_limit'], 512 * 1048576)
        self.assertEqual(config['services']['app']['pids_limit'], 128)
        self.assertEqual(limits.check_directory(self.directory, config), {'cpus': 1, 'memory': 512, 'pids': 128})
        self.engine.assert_not_called()

    def test_administrator_creation_and_existing_guests_are_unchanged(self):
        with patch.object(w, 'trusted_management', return_value=True):
            limits.initialize(self.directory)
        self.assertFalse((self.directory / 'limits.json').exists())
        config = {'services': {'app': {'image': 'nginx'}}}
        before = copy.deepcopy(config)
        limits.apply_defaults(config, self.directory, creating=False)
        self.assertEqual(config, before)

    def test_node_changes_affect_future_guests_only(self):
        limits.initialize(self.directory)
        changed = copy.deepcopy(limits.DEFAULTS); changed['container']['memory'] = 256
        limits.POLICY.write_text(json.dumps(changed))
        config = {'services': {'app': {}}}
        limits.apply_defaults(config, self.directory)
        self.assertEqual(config['services']['app']['mem_limit'], 512 * 1048576)
        other = self.base / 'future'; other.mkdir()
        limits.initialize(other)
        limits.apply_defaults(config := {'services': {'app': {}}}, other)
        self.assertEqual(config['services']['app']['mem_limit'], 256 * 1048576)

    def test_explicit_caps_and_deploy_limits_are_retained(self):
        limits.initialize(self.directory)
        config = {'services': {'app': {'cpus': .25, 'mem_limit': '64m', 'pids_limit': 8},
                               'db': {'deploy': {'resources': {'limits': {'cpus': '.5', 'memory': '128m', 'pids': 16}}}}}}
        before = copy.deepcopy(config)
        limits.apply_defaults(config, self.directory)
        self.assertEqual(config, before)
        limits.check_directory(self.directory, config)

    def test_explicit_unlimited_values_are_not_replaced_silently(self):
        limits.initialize(self.directory)
        for field, value in (('cpus', 0), ('mem_limit', 0), ('pids_limit', -1)):
            config = {'services': {'app': {field: value}}}
            limits.apply_defaults(config, self.directory)
            self.assertEqual(config['services']['app'][field], value)
            with self.assertRaisesRegex(ValueError, 'finite'):
                limits.check_directory(self.directory, config)

    def test_service_and_total_replica_ceilings(self):
        config = {'services': {str(index): {} for index in range(9)}}
        with self.assertRaisesRegex(ValueError, 'service count'):
            limits.check(config, limits.DEFAULTS['project'])
        config = {'services': {'app': {'deploy': {'replicas': 17}}}}
        with self.assertRaisesRegex(ValueError, 'replica count'):
            limits.check(config, limits.DEFAULTS['project'])

    def test_node_policy_requires_finite_consistent_defaults(self):
        for changed in ({'container': {'cpus': 0, 'memory': 512, 'pids': 128}, 'project': limits.DEFAULTS['project']},
                        {'container': {'cpus': 5, 'memory': 512, 'pids': 128}, 'project': limits.DEFAULTS['project']}):
            limits.POLICY.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                limits.initialize(self.directory)
            self.assertFalse((self.directory / 'limits.json').exists())

    def test_budget_exception_requires_node_privilege_and_positive_limits_without_it(self):
        limits.initialize(self.directory)
        config = {'services': {'app': {}}}; limits.apply_defaults(config, self.directory)
        w.write_json(self.directory / 'compose.runtime.json', config)
        with patch.object(w, 'locked'), patch.object(w, 'detail', return_value={'digest': 'fixture'}), patch.object(w, 'assert_configuration_digest'), patch.object(w, 'container_records', return_value=[]):
            with patch.dict(os.environ, BETTERDOCKER_TASK_SYS_MODIFY='0'):
                with self.assertRaisesRegex(ValueError, 'Permission denied'):
                    w.project_limit_config(ID, 'pve', {'exempt': True}, 'fixture')
            with patch.dict(os.environ, BETTERDOCKER_TASK_SYS_MODIFY='1'):
                with self.assertRaisesRegex(ValueError, 'explicit administrator exemption'):
                    w.project_limit_config(ID, 'pve', {'cpus': 0}, 'fixture')
                result = w.project_limit_config(ID, 'pve', {'exempt': True}, 'fixture')
                self.assertTrue(result['exempt'])
            config = {'services': {'app': {}}}
            limits.apply_defaults(config, self.directory)
            self.assertEqual(config, {'services': {'app': {}}})
            limits.check_directory(self.directory, config)
        self.engine.assert_not_called()

    def test_actual_prepare_uses_same_defaults_for_rootful_and_rootless(self):
        limits.initialize(self.directory)
        data = {'mode': 'compose', 'compose': 'services: {app: {image: nginx}}', 'address': '10.0.0.20'}
        for rootless in (False, True):
            self.selected.rootless = rootless
            with patch.object(parsing, 'parse', return_value={'services': {'app': {'image': 'nginx'}}}):
                config = w.prepare(data, ID, self.directory)
            self.assertEqual(config['services']['app']['cpus'], 1)
            self.assertEqual(config['services']['app']['mem_limit'], 512 * 1048576)
            self.assertEqual(config['services']['app']['pids_limit'], 128)
        self.engine.assert_not_called()

    def test_preview_has_budget_without_writing_project_policy(self):
        config = {'services': {'app': {'cpus': 5}}}
        limits.apply_defaults(config, self.directory, creating=True)
        with self.assertRaisesRegex(ValueError, 'budget exceeded'):
            limits.check_directory(self.directory, config, creating=True)
        self.assertFalse((self.directory / 'limits.json').exists())

    def test_replica_inspection_does_not_count_each_instance_as_a_service(self):
        limits.check_containers([{'HostConfig': {'NanoCpus': 100000000, 'Memory': 1048576, 'PidsLimit': 1}}] * 9,
                                limits.DEFAULTS['project'])

    def test_cli_keeps_digest_and_serializes_administrator_exception(self):
        spec = importlib.util.spec_from_file_location('resource_cli', Path(__file__).resolve().parents[1] / 'scripts/dct.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        before = dict(limits.DEFAULTS['project'], digest='fixture')
        with patch.object(cli, 'guest', return_value='/guest'), patch.object(cli, 'api', side_effect=[before, dict(before, exempt=True)]) as api, patch('sys.stdout', new_callable=io.StringIO):
            cli.dispatch('limits', {'vmid': 100, 'services': 6, 'replicas': 12, 'exempt': 1})
        self.assertEqual(api.call_args.kwargs['services'], 6)
        self.assertEqual(api.call_args.kwargs['replicas'], 12)
        self.assertIs(api.call_args.kwargs['exempt'], True)
        self.assertEqual(api.call_args.kwargs['digest'], 'fixture')
