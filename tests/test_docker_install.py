"""Existing-engine dependency checks never invoke package or service changes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ComposeCompatibility(unittest.TestCase):
    def check_version(self, version, mode):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            docker = directory / 'docker'
            docker.write_text('#!/bin/sh\nif [ "$1 $2 $3" = "compose version --short" ]; then printf "%s\\n" "$BD_TEST_VERSION"; fi\n')
            docker.chmod(0o755)
            for name in ('dockerd-rootless.sh', 'apt-get', 'systemctl'):
                helper = directory / name
                helper.write_text('#!/bin/sh\necho UNEXPECTED_HOST_CHANGE >&2\nexit 99\n')
                helper.chmod(0o755)
            result = subprocess.run(['bash', str(ROOT / 'scripts/docker-install.sh'), mode],
                env=dict(os.environ, PATH=folder + os.pathsep + os.environ['PATH'], BD_TEST_VERSION=version),
                text=True, capture_output=True)
            self.assertNotIn('UNEXPECTED_HOST_CHANGE', result.stderr)
            return result

    def test_old_or_unknown_parser_is_rejected_before_host_changes(self):
        for mode in ('rootful', 'rootless'):
            for version in ('2.39.4', '2.40.3', '5.5.0', 'development', '5.6.0-rc.1'):
                with self.subTest(mode=mode, version=version):
                    result = self.check_version(version, mode)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('5.6.0 or newer is required', result.stderr)

    def test_supported_existing_parser_is_kept(self):
        for mode in ('rootful', 'rootless'):
            for version in ('v5.6.0', '5.6.0', '5.7.1', '6.0.0'):
                with self.subTest(mode=mode, version=version):
                    self.assertEqual(self.check_version(version, mode).returncode, 0)
