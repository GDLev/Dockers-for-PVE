"""Reports preserve gaps and only authorize cleanup of recorded fixture identities."""
import json
from pathlib import Path
import tempfile
import unittest
from acceptance_report import Report, hashes


class AcceptanceReport(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.report = Report(self.root / 'report.json', 'fixture-commit', {'ordinary': 'a' * 64})

    def test_pending_ownership_is_saved_before_creation(self):
        item = self.report.plan_guest('node', 123, 'owned-fixture')
        saved = json.loads(self.report.path.read_text())
        self.assertEqual(saved['fixtures'][0], item)
        self.assertIsNone(item['id'])
        self.assertEqual(self.report.path.stat().st_mode & 0o777, 0o600)

    def test_cleanup_identity_cannot_match_by_prefix_only(self):
        item = self.report.plan_guest('node', 123, 'owned-fixture')
        expected = dict(item, id='a' * 32)
        for field, value in (('node', 'other'), ('vmid', 124), ('name', 'owned-fixture-copy')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report.confirm_guest(item, dict(expected, **{field: value}))
        self.assertIsNone(item['id'])

    def test_reused_guest_id_does_not_authorize_deletion(self):
        item = self.report.plan_guest('node', 123, 'owned-fixture')
        self.report.confirm_guest(item, dict(item, id='a' * 32))
        with self.assertRaises(ValueError):
            self.report.confirm_guest(item, dict(item, id='b' * 32))
        self.assertEqual(item['id'], 'a' * 32)

    def test_duplicate_allocation_is_rejected(self):
        self.report.plan_guest('node', 123, 'owned-fixture')
        with self.assertRaises(ValueError): self.report.plan_guest('node', 123, 'other')
        with self.assertRaises(ValueError): self.report.plan_guest('other', 124, 'owned-fixture')

    def test_incomplete_matrix_is_not_a_pass(self):
        self.report.check('fixture', 'pass', 'ordinary fixture')
        self.report.check('real traffic', 'not_tested', 'environment missing')
        self.report.finish()
        self.assertEqual(json.loads(self.report.path.read_text())['status'], 'partial')

    def test_failure_cannot_be_hidden_by_later_pass(self):
        self.report.check('first attempt', 'fail', 'ordinary failure')
        self.report.check('followup', 'pass', 'ordinary followup')
        self.report.finish()
        self.assertEqual(self.report.data['status'], 'failed')

    def test_source_hashes_ignore_only_generated_caches(self):
        source = self.root / 'source'; source.mkdir()
        (source / 'ordinary.py').write_text('ordinary fixture')
        (source / '__pycache__').mkdir(); (source / '__pycache__/ordinary.pyc').write_bytes(b'cache')
        before = hashes(source); self.assertEqual(set(before), {'ordinary.py'})
        (source / 'ordinary.py').write_text('changed fixture')
        self.assertNotEqual(before, hashes(source))

    def test_empty_run_cannot_pass(self):
        self.report.finish()
        self.assertEqual(self.report.data['status'], 'partial')

    def test_unremoved_fixture_cannot_pass(self):
        self.report.plan_guest('node', 123, 'owned-fixture')
        self.report.check('traffic', 'pass', 'ordinary fixture')
        self.report.finish()
        self.assertEqual(self.report.data['status'], 'failed')
