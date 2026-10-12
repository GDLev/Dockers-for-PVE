"""Security boundaries and update preview invariants."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime
import service_security as security
from dctlib import images, projects as w


class ServiceSecurityTests(unittest.TestCase):
    def test_stopped_health_is_not_reported_as_current(self):
        item = {'Config': {'Healthcheck': {'Test': ['CMD', 'true']}}, 'State': {'Status': 'exited', 'Health': {'Status': 'healthy'}}}
        self.assertEqual(w.health_state(item), 'inactive')
        item['State']['Status'] = 'running'
        self.assertEqual(w.health_state(item), 'healthy')
        item['State'].pop('Health')
        self.assertEqual(w.health_state(item), 'starting')
        item['Config']['Healthcheck']['Test'] = []
        self.assertEqual(w.health_state(item), 'none')

    def test_preserve_unrelated_options_and_clear_image_defaults(self):
        service = {'user': '1000', 'security_opt': ['seccomp:/profile', 'no-new-privileges:true', 'apparmor:docker-default']}
        security.apply(service, security.validate({'user': '', 'no_new_privileges': False, 'apparmor': ''}))
        self.assertNotIn('user', service)
        self.assertEqual(service['security_opt'], ['seccomp:/profile'])

    def test_privilege_relaxations_are_not_delegated(self):
        for options in (['no-new-privileges:false'], ['apparmor:unconfined'], ['seccomp:unconfined'], ['apparmor:other']):
            with self.subTest(options=options), self.assertRaises(ValueError):
                security.delegated(options)
        security.delegated(['no-new-privileges:true', 'apparmor=docker-default'])

    def test_security_types_and_capabilities(self):
        for settings in ({'read_only': 'false'}, {'no_new_privileges': 1}, {'cap_drop': ['TYPO']}, {'user': 'a\nb'}, {'apparmor': 'profile:bad'}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                w.validate_settings(settings)
        self.assertEqual(w.validate_settings({'cap_drop': ['ALL', 'ALL']})['cap_drop'], ['ALL'])

    def test_rootless_rejects_explicit_apparmor(self):
        with self.assertRaisesRegex(ValueError, 'rootful'):
            security.check({'services': {'app': {'security_opt': ['apparmor:docker-default']}}}, runtime.Runtime(mode='rootless'))

    def test_unavailable_destination_profile(self):
        with patch.object(security, 'availability', return_value={'supported': True, 'profiles': ['docker-default']}):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                security.check({'services': {'app': {'security_opt': ['apparmor:custom']}}}, runtime.Runtime())

    def test_running_security_changes_are_not_hotplugged(self):
        with self.assertRaises(ValueError):
            w.live_settings_arguments({'image': 'busybox'}, {'image': 'busybox', 'read_only': True})

    def test_partial_resource_override_preserves_security(self):
        config = {'services': {'app': {'user': '1000', 'read_only': True, 'security_opt': ['no-new-privileges:true']}}}
        w.apply_settings(config, {'app': {'memory': 32}})
        self.assertEqual(security.values(config['services']['app']), {'user': '1000', 'read_only': True, 'cap_drop': [], 'no_new_privileges': True, 'apparmor': ''})

    def test_image_override_cannot_be_mutable(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'images.json').write_text(json.dumps({'app': 'busybox:latest'}))
            with self.assertRaises(ValueError):
                images.overrides({'services': {'app': {}}}, directory)

    def test_image_override_is_in_configuration_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            old = w.configuration_digest(directory, {'name': 'test'})
            (directory / 'images.json').write_text(json.dumps({'app': {'id': 'sha256:' + 'a' * 64, 'reference': 'busybox:1.37'}}))
            self.assertNotEqual(old, w.configuration_digest(directory, {'name': 'test'}))
            self.assertEqual(images.saved_images(directory), ['sha256:' + 'a' * 64])

    def test_stale_preview_never_recreates_containers(self):
        with tempfile.TemporaryDirectory() as folder:
            identifier = 'a' * 32
            directory = Path(folder) / identifier; directory.mkdir()
            (directory / 'image-update.json').write_text(json.dumps({'phase': 'preview', 'token': 'old', 'digest': 'old'}))
            with patch.object(w, 'ROOT', Path(folder)), patch.object(w, 'detail', return_value={'digest': 'new'}), patch.object(w, 'assert_configuration_digest'), patch.object(w, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'stale'):
                    images.apply(identifier, 'node', 'old', 'recovery', 'new')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
