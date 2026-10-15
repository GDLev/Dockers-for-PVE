"""Installed entry points reject unsafe code while Python replaces its bytecode."""
import ast
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ('provision.py', 'parse_config.py', 'guest_state.py', 'host.py',
                'runtime.py', 'archive_scan.py', 'transfer.py', 'dct.py')


@unittest.skipUnless(os.geteuid() == 0, 'Installed code ownership requires Linux root')
class BootstrapOwnership(unittest.TestCase):
    def check(self, root, vanished=None, error=None):
        rows = list(os.walk(root, followlinks=False))
        if vanished:
            vanished.unlink()
        for name in ENTRY_POINTS:
            with self.subTest(entry_point=name):
                tree = ast.parse((ROOT / 'scripts' / name).read_text())
                guard = next(node for node in tree.body if isinstance(node, ast.If)
                             and '__file__' in ast.unparse(node.test))
                code = compile(ast.Module(body=[guard], type_ignores=[]), name, 'exec')
                expected = self.assertRaises(error) if error else contextlib.nullcontext()
                with expected, patch.object(os, 'walk', return_value=iter(rows)), contextlib.redirect_stderr(io.StringIO()):
                    exec(code, {'_boot_os': os, '_boot_sys': sys,
                                '__file__': '/usr/local/lib/betterdocker/' + name})

    def test_disappearing_atomic_bytecode_file_is_not_executable_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '__pycache__'; cache.mkdir()
            temporary = cache / 'module.cpython-313.pyc.123456'
            temporary.write_bytes(b'temporary bytecode')
            self.check(root, vanished=temporary)

    def test_disappearing_source_still_aborts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'module.py'; source.touch()
            self.check(root, vanished=source, error=FileNotFoundError)

    def test_disappearing_canonical_bytecode_still_aborts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '__pycache__'; cache.mkdir()
            bytecode = cache / 'module.cpython-313.pyc'; bytecode.touch()
            self.check(root, vanished=bytecode, error=FileNotFoundError)

    def test_temporary_name_outside_cache_still_aborts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            temporary = root / 'module.pyc.123456'; temporary.touch()
            self.check(root, vanished=temporary, error=FileNotFoundError)

    def test_writable_bytecode_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '__pycache__'; cache.mkdir()
            bytecode = cache / 'module.cpython-313.pyc'; bytecode.touch(); bytecode.chmod(0o666)
            self.check(root, error=SystemExit)

    def test_temporary_bytecode_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '__pycache__'; cache.mkdir()
            (cache / 'module.cpython-313.pyc.123456').symlink_to(root / 'absent')
            self.check(root, error=SystemExit)

    def test_foreign_owned_bytecode_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '__pycache__'; cache.mkdir()
            bytecode = cache / 'module.cpython-313.pyc'; bytecode.touch()
            os.chown(bytecode, 12345, 12345)
            self.check(root, error=SystemExit)

    def test_writable_cache_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '__pycache__'; cache.mkdir(); cache.chmod(0o777)
            self.check(root, error=SystemExit)
