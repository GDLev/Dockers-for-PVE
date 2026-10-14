"""Security error handling with mocked services; no Docker or node changes."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import hardening, storage


class SecurityContracts(unittest.TestCase):
    def test_missing_enforcing_profile_is_rejected(self):
        with patch.object(hardening.Path, 'exists', return_value=False):
            with self.assertRaisesRegex(ValueError, 'enforcing AppArmor'):
                hardening.available(SimpleNamespace(rootless=False))

    def test_complain_profile_is_rejected(self):
        with patch.object(hardening.Path, 'exists', return_value=True), patch.object(hardening.Path, 'read_text', return_value=hardening.PROFILE + ' (complain)\n'):
            with self.assertRaisesRegex(ValueError, 'enforcing AppArmor'):
                hardening.available(SimpleNamespace(rootless=False))

    def test_unavailable_daemon_prevents_storage_recovery(self):
        result = subprocess.CompletedProcess([], 3, '', 'inactive')
        with patch.object(storage.Path, 'exists', return_value=True), patch.object(storage, 'read_json', return_value={'operation': 'grow', 'before': {}}), patch.object(storage.node_runtime, 'service', return_value=result), patch.object(w, 'require_stopped') as stopped, patch.object(w, 'pve_storage') as disk, patch.object(w, 'write_json') as write:
            with self.assertRaisesRegex(ValueError, 'recovery was not performed'):
                storage.recover_storage_transaction('a' * 32)
            stopped.assert_not_called()
            disk.assert_not_called()
            write.assert_not_called()

    def test_unavailable_daemon_does_not_confirm_cleanup(self):
        result = subprocess.CompletedProcess([], 1, '', 'Docker is unavailable')
        with patch.object(w, 'trusted_management', return_value=True), patch.object(w.node_runtime, 'run', return_value=result) as engine:
            with self.assertRaisesRegex(ValueError, 'recovery state was retained'):
                w.run(['docker', 'volume', 'rm', 'fixture'], check=False, capture_output=True)
            engine.assert_called_once()
