"""Release fixtures exercise actual supported PVE sources, including repair/removal."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_patcher', ROOT / 'scripts/patch.py')
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class NativeFixtureTests(unittest.TestCase):
    def test_supported_pve_sources(self):
        for release in ('9.1', '9.2'):
            with self.subTest(release=release):
                folder = ROOT / 'tests/fixtures/native'
                path = folder / ('pve-' + release + '.tar.gz')
                manifest = json.loads((folder / ('pve-' + release + '.json')).read_text())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest['archive_sha256'])
                with tarfile.open(path) as archive:
                    self.assertEqual(set(archive.getnames()), {name for name, _ in patcher.NATIVE_FILES})
                    for name, kind in patcher.NATIVE_FILES:
                        content = archive.extractfile(name).read()
                        self.assertEqual(hashlib.sha256(content).hexdigest(), manifest['files'][name])
                        original = content.decode()
                        applied = patcher.transform(original, kind)
                        self.assertEqual(patcher.transform(applied, kind), applied)
                        self.assertEqual(patcher.transform(applied, kind, remove=True), original)
