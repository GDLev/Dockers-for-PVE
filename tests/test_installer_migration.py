"""Admission, confirmation and recovery for installer runtime conversion."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from installer_migration import migrate, MIGRATOR
from installer_ui import Cancelled


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.nodes = [dict(node=name, mode='rootful', installed=True, pending=False) for name in ('one', 'two')]
        self.ui = Mock()
        self.ui.t.side_effect = lambda key: key
        self.runner = SimpleNamespace(ui=self.ui, interrupted=False, results=[])
        self.events = []
        self.blocked = False
        self.fail = False
        self.token = 'a' * 32
        def process(node, args, phase):
            self.events.append((node, phase))
            self.assertEqual(args, [MIGRATOR, '--runtime=rootless'] + (['--apply'] if phase == 'apply' else []))
            report = dict(source='rootful', target='rootless', guests=[], blockers=['stop guests'] if self.blocked and node == 'two' else []) if phase == 'preflight' else dict(journal=self.token, phase='backup')
            path = self.root / (node + phase)
            path.write_text(json.dumps(report))
            return dict(node=node, phase=phase, log=str(path), code=int(self.fail and phase == 'apply'))
        self.runner.process = Mock(side_effect=process)

    def test_all_nodes_checked_before_confirmation_and_apply(self):
        self.ui.confirm.side_effect = lambda message: self.events.append(('confirm', message))
        self.assertEqual(migrate(self.runner, self.nodes, 'rootless'), 0)
        self.assertEqual([event[1] for event in self.events[:2]], ['preflight', 'preflight'])
        self.assertEqual(self.events[2][0], 'confirm')
        self.assertEqual(self.events[3:], [('one', 'apply'), ('two', 'apply')])
        output = '\n'.join(call.args[0] for call in self.ui.output.call_args_list)
        self.assertIn('--rollback ' + self.token + ' --apply', output)
        self.assertIn('discards changes', output)

    def test_last_nodes_blocker_prevents_every_apply(self):
        self.blocked = True
        with self.assertRaisesRegex(ValueError, 'No runtime changes'):
            migrate(self.runner, self.nodes, 'rootless')
        self.assertEqual(self.events, [('one', 'preflight'), ('two', 'preflight')])
        self.ui.confirm.assert_not_called()

    def test_cancel_confirmation_does_not_convert(self):
        self.ui.confirm.side_effect = Cancelled()
        with self.assertRaises(Cancelled):
            migrate(self.runner, self.nodes, 'rootless')
        self.assertFalse(self.runner.results)
        self.assertEqual(len(self.events), 2)

    def test_failure_stops_remaining_nodes_and_prints_resume(self):
        self.fail = True
        self.assertEqual(migrate(self.runner, self.nodes, 'rootless', interactive=False), 1)
        self.assertEqual(self.events[-1], ('one', 'apply'))
        self.assertEqual(len(self.runner.results), 1)
        self.ui.confirm.assert_not_called()
        output = '\n'.join(call.args[0] for call in self.ui.output.call_args_list)
        self.assertIn('--runtime=rootless --resume ' + self.token + ' --apply', output)

    def test_invalid_selection_never_runs_migrator(self):
        for field, value in [('mode', 'rootless'), ('installed', False), ('pending', True)]:
            with self.subTest(field=field):
                nodes = [dict(node) for node in self.nodes]
                nodes[1][field] = value
                with self.assertRaises(ValueError):
                    migrate(self.runner, nodes, 'rootless')
        self.runner.process.assert_not_called()

    def test_missing_or_current_unattended_target_never_runs_migrator(self):
        for target in (None, 'rootful'):
            with self.assertRaises(ValueError):
                migrate(self.runner, self.nodes, target, interactive=False)
        self.runner.process.assert_not_called()
