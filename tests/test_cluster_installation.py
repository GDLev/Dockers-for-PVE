"""Installation membership prevents removal and guest-ID allocation races."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dctlib import integration
import installer


class ClusterInstallationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for fixture in (patch.object(integration, 'METADATA', self.root),
                        patch.object(integration, 'nodes', return_value=['one', 'two'])):
            fixture.start()
            self.addCleanup(fixture.stop)

    def test_complete_membership_required_without_online_probe(self):
        integration.registration('ready', 'one')
        with self.assertRaisesRegex(ValueError, 'Missing: two'):
            integration.require_cluster()
        integration.registration('ready', 'two')
        integration.require_cluster()
        # A newly joined member must register before any new DCT ID allocation.
        with patch.object(integration, 'nodes', return_value=['one', 'two', 'three']):
            with self.assertRaisesRegex(ValueError, 'Missing: three'):
                integration.require_cluster()

    def test_guests_and_retained_reservations_block_removal_on_other_node(self):
        integration.registration('ready', 'one')
        for path, value in ((self.root / ('a' * 32 + '.json'), {'node': 'two'}),
                            (self.root / 'ids/100.json', {'id': 'a' * 32, 'node': 'two'})):
            with self.subTest(path=path):
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'retained disks'):
                    integration.registration('remove', 'one')
                self.assertTrue(integration.registered('one'))
                path.unlink()

    def test_removal_invalidates_new_admission_and_repair_restores_it(self):
        for node in ('one', 'two'):
            integration.registration('ready', node)
        integration.registration('remove', 'one')
        integration.registration('remove', 'one')
        with self.assertRaisesRegex(ValueError, 'Missing: one'):
            integration.require_cluster()
        integration.registration('ready', 'one')
        integration.require_cluster()

    def test_pending_reload_blocks_ids_until_finish_and_failed_finish_stays_pending(self):
        for node in ('one', 'two'):
            integration.registration('ready', node)
        integration.registration('pending', 'two')
        with self.assertRaisesRegex(ValueError, 'Missing: two'):
            integration.require_cluster()
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, 'CACHE', Path(directory)):
            installer.mark_restart('install')
            with patch.object(installer.subprocess, 'run', side_effect=OSError('restart failed')):
                with self.assertRaises(OSError):
                    installer.finish_restart()
            self.assertFalse(integration.registered('two'))
            self.assertTrue((Path(directory) / 'pending-restart.json').exists())
        integration.registration('ready', 'two')
        integration.require_cluster()

    def test_finish_registers_only_after_native_api_and_patch_checks(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, 'CACHE', Path(directory)):
            installer.mark_restart('repair')
            actual_exists = Path.exists
            def exists(path):
                return str(path) == '/usr/share/perl5/PVE/API2/BetterDocker.pm' or actual_exists(path)
            events = []
            with patch.object(Path, 'exists', exists), patch.object(installer.subprocess, 'check_output', return_value='one\n'), patch.object(installer.subprocess, 'run', side_effect=lambda args, **kw: events.append(args)), patch.object(installer, 'register_node', side_effect=lambda action: events.append(action)):
                installer.finish_restart()
            self.assertEqual(events[-1], 'ready')
            self.assertTrue(any(args[-1:] == ['status'] for args in events[:-1]))
            self.assertFalse((Path(directory) / 'pending-restart.json').exists())

    def test_corrupt_or_unsafe_record_fails_closed(self):
        integration.registration('ready', 'one')
        record = self.root / 'nodes/one.json'
        record.write_text('{')
        with self.assertRaises(ValueError):
            integration.require_cluster()
        record.unlink()
        target = self.root / 'other.json'
        target.write_text(json.dumps(dict(node='one', state='ready')))
        record.symlink_to(target)
        with self.assertRaises(OSError):
            integration.require_cluster()

    def test_unselected_nodes_must_already_be_installed(self):
        inventory = [dict(node='one', installed=False, error=''), dict(node='two', installed=False, error='')]
        with self.assertRaisesRegex(ValueError, 'two'):
            installer.installation_targets(inventory, ['one'], 'install')
        installer.installation_targets(inventory, ['one', 'two'], 'install')
        integration.registration('ready', 'two')
        with self.assertRaisesRegex(ValueError, 'two'):
            installer.installation_targets(inventory, ['one'], 'repair')
        inventory[1].update(installed=True)
        installer.installation_targets(inventory, ['one'], 'repair')
        inventory[1].update(installed=False, error='offline')
        installer.installation_targets(inventory, ['one'], 'repair')

    def test_removal_tombstone_precedes_integration_changes(self):
        source = (Path(__file__).resolve().parents[1] / 'start.sh').read_text()
        remove = source.index('--registration remove')
        self.assertLess(remove, source.index('systemctl disable --now betterdocker-integration.timer'))
        self.assertLess(remove, source.index('scripts/patch.py" remove'))
        with patch.object(installer.subprocess, 'run') as run:
            installer.register_node('remove')
        program = run.call_args.args[0][2]
        self.assertIn("cfs_lock_file('datacenter.cfg'", program)
        self.assertIn('--registration-worker', program)


if __name__ == '__main__':
    unittest.main()
