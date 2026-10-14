"""Replica budgets, saved state and administrator policy cannot silently become unlimited."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker, BASE
from dctlib import limits

ID = 'a' * 32
BUDGET = {'cpus': 1, 'memory': 128, 'pids': 32}
CONFIG = {'services': {'web': {'cpus': .25, 'mem_limit': 32 * 1048576, 'pids_limit': 8, 'deploy': {'replicas': 2}},
                       'db': {'cpus': .5, 'mem_limit': 64 * 1048576, 'pids_limit': 16}}}


class ProjectLimitTests(unittest.TestCase):
    def test_all_replicas_are_counted_and_zero_replicas_use_no_budget(self):
        self.assertEqual(limits.check(CONFIG, BUDGET), {'cpus': 1, 'memory': 128, 'pids': 32})
        config = copy.deepcopy(CONFIG)
        config['services']['web']['deploy']['replicas'] = 3
        for key in BUDGET:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'budget exceeded'):
                limits.check(config, {key: BUDGET[key]})
        config['services']['web'] = {'deploy': {'replicas': 0}}
        self.assertEqual(limits.check(config, BUDGET)['memory'], 64)

    def test_no_unlimited_service_or_replica_can_bypass_active_ceiling(self):
        for key, field in [('cpus', 'cpus'), ('memory', 'mem_limit'), ('pids', 'pids_limit')]:
            for raw in (0, -1):
                config = copy.deepcopy(CONFIG)
                config['services']['db'][field] = raw
                with self.subTest(key=key, raw=raw), self.assertRaisesRegex(ValueError, 'finite'):
                    limits.check(config, {key: BUDGET[key]})
        for invalid in (True, -1, 1.5):
            config = copy.deepcopy(CONFIG)
            config['services']['web']['deploy']['replicas'] = invalid
            with self.assertRaises(ValueError): limits.check(config, BUDGET)

    def test_canonical_deploy_limits_and_cpu_quota_are_counted(self):
        config = {'services': {'web': {'deploy': {'replicas': 2, 'resources': {'limits': {'cpus': '0.5', 'memory': '64m', 'pids': 16}}}}}}
        self.assertEqual(limits.check(config, BUDGET), {'cpus': 1, 'memory': 128, 'pids': 32})
        config = {'services': {'web': {'cpu_quota': 50000, 'cpu_period': 100000}}}
        self.assertEqual(limits.check(config, {'cpus': 1})['cpus'], .5)

    def test_invalid_policy_and_nonfinite_values_rejected(self):
        for value in ({'cpus': float('nan')}, {'memory': True}, {'pids': 1.5}, {'memory': -1}, {'unknown': 10}):
            with self.subTest(value=value), self.assertRaises(ValueError): limits.validate(value)

    def test_inspected_container_caps_are_checked_independently_of_saved_compose(self):
        container = {'HostConfig': {'NanoCpus': 500000000, 'Memory': 64 * 1048576, 'PidsLimit': 16}}
        limits.check_containers([container, container], BUDGET)
        with self.assertRaisesRegex(ValueError, 'budget exceeded'): limits.check_containers([container] * 3, BUDGET)
        with self.assertRaisesRegex(ValueError, 'finite'): limits.check_containers([{'HostConfig': {}}], BUDGET)

    def test_admin_budget_changes_are_stopped_digest_checked_and_not_delegated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'projects'; directory = root / ID; directory.mkdir(parents=True)
            (directory / 'compose.runtime.json').write_text(json.dumps(CONFIG))
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'detail', return_value={'digest': 'digest'}), \
                 patch.object(worker, 'assert_configuration_digest') as digest, patch.object(worker, 'container_records', return_value=[]):
                with patch.dict(os.environ, BETTERDOCKER_TASK_SYS_MODIFY='0'), self.assertRaisesRegex(ValueError, 'Permission denied'):
                    worker.project_limit_config(ID, 'pve', BUDGET)
                self.assertFalse((directory / 'limits.json').exists())
                with patch.dict(os.environ, BETTERDOCKER_TASK_SYS_MODIFY='1'):
                    worker.project_limit_config(ID, 'pve', BUDGET, 'old-digest')
                digest.assert_called_with(ID, 'pve', 'old-digest')
                self.assertEqual(limits.load(directory), BUDGET)
                before = (directory / 'limits.json').read_bytes()
                with patch.dict(os.environ, BETTERDOCKER_TASK_SYS_MODIFY='1'), self.assertRaisesRegex(ValueError, 'budget exceeded'):
                    worker.project_limit_config(ID, 'pve', dict(BUDGET, cpus=.5))
                self.assertEqual((directory / 'limits.json').read_bytes(), before)

    def test_queued_decrease_cannot_fund_an_immediate_increase(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'projects'; directory = root / ID; directory.mkdir(parents=True)
            (directory / 'compose.runtime.json').write_text(json.dumps(CONFIG))
            (directory / 'limits.json').write_text(json.dumps(BUDGET))
            (directory / 'pending.json').write_text(json.dumps({'db': {'memory': 32}}))
            rows = [{'key': key, 'value': value} for key, value in worker.service_values(CONFIG['services']['web']).items()]
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'detail', return_value={}), \
                 patch.object(worker, 'assert_configuration_digest'), patch.object(worker, 'pending_settings', return_value=rows), \
                 patch.object(worker, 'run') as execute, self.assertRaisesRegex(ValueError, 'budget exceeded'):
                worker.service_settings(ID, 'pve', 'web', {'memory': 48})
            execute.assert_not_called()
            self.assertEqual(json.loads((directory / 'pending.json').read_text()), {'db': {'memory': 32}})

    def test_stop_is_allowed_even_when_a_project_exceeds_its_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'limits.json').write_text(json.dumps(BUDGET))
            (directory / 'compose.runtime.json').write_text(json.dumps({'services': {'up': {'cpus': 2}}}))
            with patch.object(worker.node_runtime, 'run') as execute:
                worker.run(['docker', 'compose', '-f', str(directory / 'compose.runtime.json'), 'stop', 'up'])
            execute.assert_called_once()

    def test_composition_cannot_run_after_out_of_budget_source_change(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'limits.json').write_text(json.dumps(BUDGET))
            (directory / 'compose.runtime.json').write_text(json.dumps({'services': {'web': {'cpus': 2}}}))
            with patch.object(worker.node_runtime, 'run') as execute, self.assertRaises(ValueError):
                worker.run(['docker', 'compose', '-f', str(directory / 'compose.runtime.json'), 'up', '-d'])
            execute.assert_not_called()
