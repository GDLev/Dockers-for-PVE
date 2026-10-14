"""Live tests must explicitly select their targets through private configuration."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import live_environment as env


class LiveEnvironment(unittest.TestCase):
    def setUp(self):
        env.config.cache_clear()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(env.config.cache_clear)
        self.path = Path(self.folder.name) / 'test.json'
        self.path.write_text(json.dumps({
            'nodes': {'primary': 'pve1', 'secondary': 'pve2', 'rootless': 'pve3'},
            'addresses': '192.0.2.0/24', 'backup_storage': 'backup',
        }))
        self.path.chmod(0o600)
        self.environment = patch.dict(os.environ, {'BETTERDOCKER_TEST_CONFIG': str(self.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.original_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.original_umask)

    def test_requires_explicit_configuration(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'BETTERDOCKER_TEST_CONFIG'):
            env.config()

    def test_rejects_readable_configuration(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '0600'):
            env.config()

    def test_rejects_symbolic_link(self):
        link = self.path.with_name('link.json')
        link.symlink_to(self.path)
        with patch.dict(os.environ, {'BETTERDOCKER_TEST_CONFIG': str(link)}), self.assertRaises(OSError):
            env.config()

    def test_addresses_cannot_escape_subnet(self):
        self.assertEqual(env.address(150), '192.0.2.150')
        self.assertEqual(env.prefix(), 24)
        self.assertEqual(env.node('rootless'), 'pve3')
        with self.assertRaises(ValueError):
            env.address(256)
        with self.assertRaises(ValueError):
            env.address(-1)
