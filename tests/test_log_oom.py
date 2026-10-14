"""Deployment defaults and delegated limits protect host log space and OOM recovery."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import yaml
from dctlib import hardening
from unittest.mock import patch
from test_worker import worker
from dctlib.policy import apply_log_defaults

ID = 'a' * 32


class LogOomTests(unittest.TestCase):
    def test_defaults_are_bounded_idempotent_and_preserve_explicit_driver_options(self):
        config = {'services': {
            'default': {'image': 'busybox'},
            'local': {'logging': {'driver': 'local', 'options': {'max-size': '5m'}}},
            'external': {'logging': {'driver': 'syslog', 'options': {'syslog-address': 'udp://logs:514'}}},
        }}
        external = copy.deepcopy(config['services']['external'])
        apply_log_defaults(config)
        self.assertEqual(config['services']['default']['logging'],
                         {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}})
        self.assertEqual(config['services']['local']['logging']['options'], {'max-size': '5m', 'max-file': '3'})
        self.assertEqual(config['services']['external'], external)
        first = copy.deepcopy(config)
        apply_log_defaults(config)
        self.assertEqual(config, first)

    def test_delegated_limits_reject_disabled_rotation_and_excessive_values(self):
        for options in ({'max-size': '-1'}, {'max-size': '0'}, {'max-size': '101m'},
                        {'max-size': '1g'}, {'max-size': 'invalid'}, {'max-size': True},
                        {'max-file': '0'}, {'max-file': '11'}, {'max-file': '1.5'}, {'max-file': True}):
            config = {'services': {'app': {'image': 'busybox', 'logging': {'driver': 'json-file', 'options': options}}}}
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'Log rotation'):
                apply_log_defaults(config)
                worker.validate_delegated_config(config, ID)
        for driver in ('json-file', 'local'):
            config = {'services': {'app': {'logging': {'driver': driver, 'options': {'max-size': '10M', 'max-file': '3'}}}}}
            worker.validate_delegated_config(config, ID)

    def test_oom_protection_checks_config_and_saved_containers(self):
        for changes in ({'oom_kill_disable': True}, {'oom_score_adj': -1000}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'OOM'):
                worker.validate_delegated_config({'services': {'app': {'image': 'busybox', **changes}}}, ID)
        for changes in ({'OomKillDisable': True}, {'OomScoreAdj': -1}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'OOM'):
                worker.validate_delegated_containers([{'HostConfig': changes}], ID)
        worker.validate_delegated_config({'services': {'app': {'oom_kill_disable': False, 'oom_score_adj': 100}}}, ID)

    def test_prepare_persists_defaults_for_both_roles_and_rejects_delegated_oom(self):
        from test_worker import BASE
        for trusted in ('0', '1'):
            with tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                config = {'services': {'app': {'image': 'busybox'}}}
                with patch('dctlib.parsing.parse', side_effect=lambda source, *args: copy.deepcopy(config) if args else yaml.safe_load(source)), patch.object(hardening, 'available'), patch('service_security.availability', return_value={'supported': True, 'profiles': ['betterdocker-default']}), patch.object(worker, 'capture', return_value=json.dumps(config)), \
                     patch.dict(os.environ, BETTERDOCKER_TRUSTED=trusted):
                    worker.prepare(BASE, ID, directory)
                saved = json.loads((directory / 'compose.runtime.json').read_text())
                self.assertEqual(saved['services']['app']['logging']['options'], {'max-size': '10m', 'max-file': '3'})
                before = (directory / 'compose.runtime.json').read_text()
                config['services']['app']['oom_kill_disable'] = True
                with patch('dctlib.parsing.parse', side_effect=lambda source, *args: copy.deepcopy(config) if args else yaml.safe_load(source)), patch.object(hardening, 'available'), patch('service_security.availability', return_value={'supported': True, 'profiles': ['betterdocker-default']}), patch.object(worker, 'capture', return_value=json.dumps(config)), \
                     patch.dict(os.environ, BETTERDOCKER_TRUSTED='0'), self.assertRaisesRegex(ValueError, 'OOM'):
                    worker.prepare(BASE, ID, directory)
                self.assertEqual((directory / 'compose.runtime.json').read_text(), before)
                with patch('dctlib.parsing.parse', side_effect=lambda source, *args: copy.deepcopy(config) if args else yaml.safe_load(source)), patch.object(hardening, 'available'), patch('service_security.availability', return_value={'supported': True, 'profiles': ['betterdocker-default']}), patch.object(worker, 'capture', return_value=json.dumps(config)), \
                     patch.dict(os.environ, BETTERDOCKER_TRUSTED='1'):
                    worker.prepare(BASE, ID, directory)
                self.assertTrue(json.loads((directory / 'compose.runtime.json').read_text())['services']['app']['oom_kill_disable'])
