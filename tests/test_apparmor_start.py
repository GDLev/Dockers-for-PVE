"""Profile preflight contracts; Docker, recovery and host services are mocked."""
from contextlib import ExitStack
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import hardening, lifecycle, service_start
import guest_state

ID = 'a' * 32


class AppArmorStart(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.base = Path(temporary)
        self.stack.enter_context(patch.object(w, 'ROOT', self.base / 'projects'))
        self.stack.enter_context(patch.object(w, 'METADATA', self.base / 'metadata'))
        self.stack.enter_context(patch.object(w, 'locked'))
        self.stack.enter_context(patch.object(w, 'detail', return_value={'id': ID, 'node': 'pve'}))
        for name in ('recover_network_transaction', 'recover_storage_transaction', 'recover_snapshot_transaction'):
            self.stack.enter_context(patch.object(w, name))
        self.engine = self.stack.enter_context(patch.object(w, 'run'))
        self.stack.enter_context(patch.object(hardening, 'mode', return_value='hardened'))
        self.stack.enter_context(patch.object(w.node_runtime, 'load', return_value=SimpleNamespace(rootless=False)))
        self.stack.enter_context(patch('dctlib.recovery.require_docker'))
        actual_exists = Path.exists
        self.stack.enter_context(patch.object(Path, 'exists', lambda path: False if str(path) == '/sys/kernel/security/apparmor/profiles' else actual_exists(path)))

    def rejected(self, function, *args):
        with self.assertRaisesRegex(ValueError, 'enforcing AppArmor'):
            function(*args)
        self.engine.assert_not_called()

    def test_project_power_operations_check_before_stopping(self):
        with patch.object(hardening.Path, 'exists', return_value=False), patch.object(w, 'guest_hook') as hook:
            for operation in ('start', 'restart', 'reboot'):
                self.rejected(w.action, ID, 'pve', operation)
            hook.assert_not_called()

    def test_service_power_operations_check_before_network_or_shutdown(self):
        with patch('dctlib.projects.read_json', return_value={'services': {'app': {}}}), patch.object(hardening.Path, 'exists', return_value=False), patch.object(w, 'prepare_network_start') as network, patch.object(w, 'graceful_shutdown') as stop:
            for operation in ('start', 'restart', 'reboot'):
                self.rejected(w.service_action, ID, 'pve', 'app', operation)
            network.assert_not_called()
            stop.assert_not_called()

    def test_preserved_start_checks_before_reading_or_starting_instances(self):
        with patch.object(hardening.Path, 'exists', return_value=False), patch.object(service_start, 'read_json') as read:
            self.rejected(service_start.start, ID)
            read.assert_not_called()

    def test_creation_checks_before_writing_policy_or_preparing_compose(self):
        with patch.object(w, 'current_node', return_value='pve'), patch.object(w, 'reserved_ids', return_value={100: {'id': ID}}), patch.object(hardening.Path, 'iterdir', return_value=iter([])), patch.object(w, 'network_preflight'), patch.object(hardening.Path, 'exists', return_value=False), patch.object(w, 'write_json') as write, patch.object(w, 'prepare') as prepare:
            self.rejected(w.create, {'name': 'fixture'}, 'pve', ID)
            write.assert_not_called()
            prepare.assert_not_called()

    def test_daemon_resume_retains_journal_and_restart_policies(self):
        journal = self.base / 'lifecycle.json'
        journal.write_text('retained')
        boot = self.base / 'proc/sys/kernel/random/boot_id'
        boot.parent.mkdir(parents=True)
        boot.write_text('same')
        value = {'boot_id': 'same', 'containers': [{'project': ID, 'id': 'container', 'state': 'running', 'policy': {'Name': 'always'}}]}
        with patch.object(w, 'PROC', self.base / 'proc'), patch.object(lifecycle, 'read_json', return_value=value), patch.object(w, 'summaries', return_value=[{'id': ID, 'node': 'pve'}]), patch.object(w, 'container_records', return_value=[{'id': 'container'}]):
            self.rejected(w.daemon_resume, 'pve')
        self.assertEqual(journal.read_text(), 'retained')

    def test_backup_resume_retains_journal_without_unpausing(self):
        journal = self.base / 'capture.json'
        journal.write_text('retained')
        with patch.object(guest_state, 'journal_path', return_value=journal), patch.object(guest_state, 'read', return_value={'paused': ['container']}), patch.object(w, 'container_records', return_value=[{'id': 'container', 'state': 'paused'}]):
            self.rejected(guest_state.cleanup_capture, ID)
        self.assertEqual(journal.read_text(), 'retained')

    def test_standard_and_rootless_do_not_require_managed_profile(self):
        with patch.object(hardening, 'mode', return_value='standard'), patch.object(hardening, 'available') as profile:
            hardening.check_profile(ID)
            profile.assert_not_called()
        with patch.object(w.node_runtime, 'load', return_value=SimpleNamespace(rootless=True)), patch.object(hardening.Path, 'exists') as exists:
            hardening.check_profile(ID)
            exists.assert_not_called()

    def test_enforcing_profile_is_accepted(self):
        with patch.object(hardening.Path, 'exists', return_value=True), patch.object(hardening.Path, 'read_text', return_value=hardening.PROFILE + ' (enforce)\n'):
            hardening.check_profile(ID)
