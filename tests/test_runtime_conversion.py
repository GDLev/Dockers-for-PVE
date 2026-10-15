"""Rootful security setup must precede any runtime switch or daemon startup."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_conversion', SOURCE / 'scripts/migrate-runtime.py')
conversion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conversion)


class RuntimeConversionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name) / 'profile'
        self.profile.write_text((SOURCE / 'assets/betterdocker.apparmor').read_text())
        setting = patch.object(conversion, 'PROFILE', self.profile)
        setting.start(); self.addCleanup(setting.stop)
        selected = patch.object(conversion.runtime, 'load', return_value=SimpleNamespace(mode='rootless', rootless=True))
        selected.start(); self.addCleanup(selected.stop)

    def test_missing_profile_blocks_preview_and_apply_before_runtime_changes(self):
        self.profile.unlink()
        with patch.object(conversion.worker, 'summaries', return_value=[]), \
                patch.object(conversion.worker, 'capture', return_value=''), \
                patch.object(conversion.runtime_install, 'preflight'), \
                patch.object(conversion.runtime, 'service') as service, \
                patch.object(conversion, 'command') as command, \
                patch.object(conversion.security_install, 'initialize') as security:
            report = conversion.inventory('rootful')
            self.assertTrue(any('repair installation' in item for item in report['blockers']))
            with self.assertRaisesRegex(ValueError, 'repair installation'):
                conversion.select_target('rootful')
            journals = self.profile.parent / 'migrations'
            with patch.object(conversion, 'ROOT', journals), self.assertRaisesRegex(ValueError, 'repair installation'):
                conversion.convert('rootful')
            self.assertFalse(journals.exists())
        service.assert_not_called(); command.assert_not_called(); security.assert_not_called()

    def test_writable_profile_is_rejected_before_security_setup(self):
        self.profile.chmod(0o666)
        with patch.object(conversion.security_install, 'initialize') as security, \
                patch.object(conversion.runtime, 'service') as service:
            with self.assertRaisesRegex(ValueError, 'repair installation'):
                conversion.select_target('rootful')
        security.assert_not_called(); service.assert_not_called()

    def test_profile_load_failure_does_not_stop_source_runtime(self):
        error = subprocess.CalledProcessError(1, ['apparmor_parser'])
        with patch.object(conversion.security_install, 'initialize', side_effect=error), \
                patch.object(conversion.runtime, 'service') as service, \
                patch.object(conversion, 'command') as command:
            with self.assertRaises(subprocess.CalledProcessError):
                conversion.select_target('rootful')
        service.assert_not_called(); command.assert_not_called()

    def test_security_is_ready_before_stopping_rootless_source(self):
        events = []
        def security(mode, profile):
            self.assertEqual((mode, profile), ('rootful', self.profile))
            events.append('security')
        def stop(action):
            self.assertEqual(action, 'stop')
            events.append('source-stop')
            raise ValueError('fixture stops before changing runtime')
        with patch.object(conversion.security_install, 'initialize', side_effect=security), \
                patch.object(conversion.runtime, 'service', side_effect=stop), \
                patch.object(conversion.runtime_install, 'initialize') as initialize:
            with self.assertRaisesRegex(ValueError, 'fixture stops'):
                conversion.select_target('rootful')
        self.assertEqual(events, ['security', 'source-stop'])
        initialize.assert_not_called()
