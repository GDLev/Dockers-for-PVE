"""Keep public documentation, frontend inputs and package identity consistent."""
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from dctlib.version import VERSION
import release


class Repository(unittest.TestCase):
    def test_frontend_manifest_covers_sources_once(self):
        directory = ROOT / 'frontend'
        names = json.loads((directory / 'sources.json').read_text())
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {p.name for p in directory.glob('*.js') if p.name != 'bootstrap.js'})

    def test_documentation_has_no_local_lab_references(self):
        private = re.compile(r'/Users/|10\.121\.15\.|\bpve' + r'test\d*\b|/tmp/bd-sec\d+-source')
        for path in ROOT.glob('*.md'):
            with self.subTest(path=path.name):
                self.assertIsNone(private.search(path.read_text()))

    def test_local_documentation_links_exist(self):
        for path in ROOT.glob('*.md'):
            for target in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', path.read_text()):
                if re.match(r'[a-z]+:', target) or target.startswith('#'):
                    continue
                filename = target.split('#', 1)[0]
                with self.subTest(path=path.name, target=target):
                    self.assertTrue((path.parent / filename).is_file())

    def test_version_badge_uses_current_release_identity(self):
        self.assertIn('version-' + VERSION + '-blue', (ROOT / 'README.md').read_text())

    def test_source_release_includes_current_public_documents(self):
        documents = {p.name for p in ROOT.glob('*.md')}
        self.assertTrue(documents.issubset(set(release.SOURCE_PATHS)))
