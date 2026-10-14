"""Small ordinary archives and mocked reader failures; no workload restore."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import archives
from dctlib.secure_io import MAX_JSON
import guest_state


class ArchiveReaders(unittest.TestCase):
    def scan(self, compressed=False):
        with tempfile.TemporaryFile() as source:
            with tarfile.open(fileobj=source, mode='w:gz' if compressed else 'w') as output:
                directory = tarfile.TarInfo('data'); directory.type = tarfile.DIRTYPE
                output.addfile(directory)
                entry = tarfile.TarInfo('data/message.txt'); entry.size = 5
                output.addfile(entry, io.BytesIO(b'hello'))
            source.seek(0)
            return archives.members(source.fileno(), 1024)

    def test_ordinary_tar_and_gzip_are_scanned_offline(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                entries = self.scan(compressed)
                self.assertEqual([entry.name for entry in entries], ['data', 'data/message.txt'])
                self.assertEqual(entries[1].size, 5)

    def test_reader_failure_returns_no_partial_members(self):
        with patch.object(archives, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'unavailable')):
            with self.assertRaisesRegex(ValueError, 'Invalid, oversized or unsupported'):
                archives.members(123, 1024)

    def test_reply_is_validated_and_child_output_is_bounded(self):
        for value in ({}, [{'name': 'data/file', 'link': '', 'size': '5', 'kind': 'file'}], [{'name': 'data/file', 'link': '', 'size': 5, 'kind': 'unknown'}]):
            with self.subTest(value=value), patch.object(archives, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(value), '')) as run:
                with self.assertRaises(ValueError):
                    archives.members(123, 1024)
                self.assertEqual(run.call_args.kwargs['output_limit'], archives.MAX_HEADERS)
                self.assertEqual(run.call_args.kwargs['timeout'], 1800)

    def test_declared_size_cannot_exceed_destination_capacity(self):
        reply = [{'name': 'data/file', 'link': '', 'size': 5, 'kind': 'file'}]
        with patch.object(archives, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), '')):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                archives.members(123, 4)

    def test_normal_link_metadata_is_preserved(self):
        entry = tarfile.TarInfo('data/latest'); entry.type = tarfile.SYMTYPE; entry.linkname = 'message.txt'
        self.assertEqual(archives.record(entry)['link'], 'message.txt')

    def test_link_metadata_length_is_bounded(self):
        entry = tarfile.TarInfo('data/latest'); entry.type = tarfile.SYMTYPE; entry.linkname = 'x' * (archives.MAX_PATH + 1)
        with self.assertRaisesRegex(ValueError, 'link length'):
            archives.record(entry)

    def test_configuration_size_is_checked_before_extraction(self):
        entry = tarfile.TarInfo('configuration/compose.runtime.json'); entry.size = MAX_JSON + 1
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'backup.tar'; archive.write_bytes(b'fixture')
            destination = Path(temporary) / 'restore'
            with patch.object(archives, 'members', return_value=[entry]), patch('dctlib.capacity.require_space', return_value=MAX_JSON * 2), patch.object(w, 'run') as extract:
                with self.assertRaisesRegex(ValueError, 'configuration exceeds'):
                    guest_state.unpack_state(archive, destination)
                extract.assert_not_called()
            self.assertFalse(destination.exists())

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0, 'Bootstrap privilege drop requires a Linux root fixture')
    def test_bootstrap_extracts_small_release_without_running_it(self):
        source = Path('/tmp/bd-sec23-source/install.sh')
        if not source.exists():
            source = Path(__file__).resolve().parents[1] / 'install.sh'
        reader = source.read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory(prefix='bd-release-reader-') as temporary:
            archive = Path(temporary) / 'source.tar.gz'
            destination = Path(temporary) / 'output'; destination.mkdir(mode=0o700)
            content = b'# Ordinary installer fixture. Never executed.\n'
            with tarfile.open(archive, 'w:gz') as output:
                root = tarfile.TarInfo('release'); root.type = tarfile.DIRTYPE
                output.addfile(root)
                entry = tarfile.TarInfo('release/start.sh'); entry.size = len(content); entry.mode = 0o755
                output.addfile(entry, io.BytesIO(content))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            result = subprocess.run([sys.executable, '-', str(archive), digest, str(destination)], input=reader, text=True, capture_output=True, timeout=70)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = destination / 'start.sh'
            self.assertEqual(installed.read_bytes(), content)
            self.assertEqual(installed.stat().st_uid, 0)
            self.assertEqual(installed.stat().st_mode & 0o7777, 0o755)
