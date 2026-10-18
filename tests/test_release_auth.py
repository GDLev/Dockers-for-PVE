"""Release authentication with disposable keys/files and mocked package operations."""
import json
import contextlib
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release
import installer


class ReleaseAuthentication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='bd-test-signing-')
        cls.private = Path(cls.keys.name) / 'private.pem'
        cls.public = Path(cls.keys.name) / 'public.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(cls.private)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(cls.private), '-pubout', '-out', str(cls.public)], check=True, capture_output=True)
        cls.pin = release.fingerprint(cls.public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in release.SOURCE_PATHS:
            path = self.root / name
            if '.' in name:
                path.write_text('ordinary fixture\n')
            else:
                path.mkdir()
                (path / 'fixture').write_text('ordinary fixture\n')
        (self.root / 'scripts/dctlib').mkdir()
        (self.root / 'scripts/dctlib/version.py').write_text("VERSION = '0.20.0'\n")
        (self.root / 'scripts/release.py').write_bytes((ROOT / 'scripts/release.py').read_bytes())
        (self.root / 'install.sh').write_bytes((ROOT / 'install.sh').read_bytes())
        self.version = '0.20.0'
        self.metadata = dict(format=1, version=self.version, artifacts={
            'install.sh': release.digest(self.root / 'install.sh'),
            'betterdockers-0.20.0.tar.gz': 'a' * 64,
            'proxmox-betterdockers_0.20.0_all.deb': 'b' * 64,
        }, source=release.tree(self.root))
        self.sign()

    def sign(self, raw=None):
        (self.root / release.MANIFEST).write_bytes(raw if raw is not None else json.dumps(self.metadata).encode())
        subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(self.private), '-out', str(self.root / release.SIGNATURE), str(self.root / release.MANIFEST)], check=True)

    def read(self, **kwargs):
        return release.read_manifest(self.root / release.MANIFEST, self.root / release.SIGNATURE, self.public, self.pin, **kwargs)

    def test_valid_signed_release_and_source(self):
        self.assertEqual(self.read(), self.metadata)
        release.verify_tree(self.root, self.read())

    def test_changed_installer_source_archive_and_deb_rejected(self):
        for name in self.metadata['artifacts']:
            with self.subTest(name=name):
                file = self.root / name
                file.write_bytes(b'changed ordinary fixture')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    release.verify_artifact(self.read(), file, name)

    def test_changed_metadata_rejected(self):
        (self.root / release.MANIFEST).write_text(json.dumps(dict(self.metadata, version='0.21.0')))
        with self.assertRaises(subprocess.CalledProcessError):
            self.read()

    def test_changed_signature_rejected(self):
        (self.root / release.SIGNATURE).write_bytes(b'ordinary invalid signature fixture')
        with self.assertRaises(subprocess.CalledProcessError):
            self.read()

    def test_wrong_trusted_key_or_fingerprint_rejected(self):
        with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
            release.read_manifest(self.root / release.MANIFEST, self.root / release.SIGNATURE, self.public, '0' * 64)

    def test_wrong_version_rejected(self):
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            self.read(expected='0.21.0')

    def test_changed_missing_and_extra_cache_files_rejected(self):
        target = self.root / 'scripts/fixture'
        for operation in ('change', 'remove', 'extra'):
            with self.subTest(operation=operation):
                target.write_text('ordinary fixture\n')
                extra = self.root / 'scripts/extra'
                extra.unlink(missing_ok=True)
                if operation == 'change': target.write_text('changed')
                elif operation == 'remove': target.unlink()
                else: extra.write_text('extra')
                with self.assertRaisesRegex(ValueError, 'differs'):
                    release.verify_tree(self.root, self.read())

    def test_symbolic_link_source_rejected(self):
        target = self.root / 'scripts/fixture'
        target.unlink(); target.symlink_to(self.root / 'start.sh')
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            release.verify_tree(self.root, self.read())

    def test_duplicate_metadata_fields_rejected_even_when_signed(self):
        raw = json.dumps(self.metadata).replace('"format": 1', '"format": 1, "format": 1')
        self.sign(raw.encode())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.read()

    def test_invalid_signed_path_rejected(self):
        self.metadata['source']['../outside'] = '0' * 64
        self.sign()
        with self.assertRaisesRegex(ValueError, 'filename'):
            self.read()

    def test_missing_artifact_rejected(self):
        self.metadata['artifacts'].pop('install.sh'); self.sign()
        with self.assertRaisesRegex(ValueError, 'installer, source archive and DEB'):
            self.read()

    def test_explicit_downgrade_only(self):
        state = self.root / 'installed.json'
        state.write_text('{"version":"0.21.0"}')
        with patch.object(release, 'protected'):
            with self.assertRaisesRegex(ValueError, 'Downgrade refused'):
                release.check_version(self.read(), state=state)
            release.check_version(self.read(), True, state=state)
            release.check_version(dict(self.metadata, version='0.22.0'), state=state)

    def test_unattended_repair_and_custom_url_have_no_unsigned_fallback(self):
        source = (ROOT / 'install.sh').read_text()
        self.assertIn('openssl dgst -sha256 -verify', source)
        self.assertIn('BETTERDOCKER_RELEASE_URL', source)
        self.assertIn('BETTERDOCKER_SOURCE_URL', source)
        self.assertNotIn('SOURCE_SHA256SUMS', source)
        args = installer.parser().parse_args(['repair', '--yes'])
        self.assertFalse(args.development_source)
        self.assertFalse(args.allow_downgrade)

    def test_streamed_bootstrap_executes_only_the_authenticated_installer(self):
        bootstrap = (ROOT / 'install.sh').read_text()
        section = bootstrap[bootstrap.index('    local metadata_url='):bootstrap.index('    echo "Downloading ')]
        binaries = self.root / 'bin'
        binaries.mkdir()
        curl = binaries / 'curl'
        curl.write_text('#!' + sys.executable + '\n' +
                        'import os, pathlib, shutil, sys\n' +
                        'args=sys.argv[1:]; name=next(a for a in args if a.startswith("https://")).rsplit("/",1)[1]\n' +
                        'source={"RELEASE.json":".release.json","RELEASE.sig":".release.sig","install.sh":"install.sh"}[name]\n' +
                        'shutil.copyfile(pathlib.Path(os.environ["BD_TEST_ASSETS"])/source,args[args.index("--output")+1])\n')
        curl.chmod(0o755)
        executed = self.root / 'executed'
        installer_file = self.root / 'install.sh'
        installer_file.write_text('printf "%s\\n" "$@" > "$BD_TEST_EXECUTED"\n')
        self.metadata['artifacts']['install.sh'] = release.digest(installer_file)
        working = self.root / 'download'
        working.mkdir()
        shell = ('set -euo pipefail\nmain() {\n' +
                 'local project_dir=' + shlex.quote(str(working)) + '\n' +
                 'local release_version=0.20.0 release_url=https://example.invalid/release\n' +
                 'local trusted_key=' + shlex.quote(str(self.public)) + '\n' + section +
                 '\necho "Unverified stream continued" >&2; return 99\n}\nmain "$@"\n')
        environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                           BD_TEST_ASSETS=str(self.root), BD_TEST_EXECUTED=str(executed))
        environment.pop('BETTERDOCKER_RELEASE_URL', None)
        for case in ('valid', 'changed installer', 'bad signature'):
            with self.subTest(case=case):
                installer_file.write_text('printf "%s\\n" "$@" > "$BD_TEST_EXECUTED"\n')
                self.sign()
                executed.unlink(missing_ok=True)
                if case == 'changed installer':
                    installer_file.write_text(installer_file.read_text() + 'echo changed\n')
                elif case == 'bad signature':
                    (self.root / release.SIGNATURE).write_bytes(b'not a signature')
                result = subprocess.run(['bash', '-s', '--', 'repair', '--yes'], input=shell,
                                        env=environment, text=True, capture_output=True, timeout=20)
                if case == 'valid':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(executed.read_text().splitlines(), ['repair', '--yes'])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(executed.exists(), 'Unauthenticated installer must never execute')

    def test_signed_node_preparation_checks_before_transfer(self):
        runner = installer.Runner(unittest.mock.Mock(), unittest.mock.Mock())
        with patch.object(release, 'source', side_effect=ValueError('bad signature')):
            with self.assertRaisesRegex(ValueError, 'bad signature'):
                runner.prepare(['node'])
        runner.cluster.run.assert_not_called()

    def test_all_nodes_check_before_worker(self):
        runner = installer.Runner(unittest.mock.Mock(), unittest.mock.Mock())
        runner.cluster.run.return_value.stdout = '/tmp/betterdocker-install.ABCDEFGH\n'
        with patch.object(installer, 'SOURCE', self.root), patch.object(release, 'source', return_value=self.metadata), patch.object(installer, 'archive', return_value=b'fixture'):
            runner.prepare(['node'])
        self.assertEqual(runner.cluster.run.call_args_list[-1].args[1][0:2], ['python3', '/tmp/betterdocker-install.ABCDEFGH/scripts/release.py'])
        calls = runner.cluster.run.call_args_list
        authentication = next(i for i, call in enumerate(calls) if 'transport.release.json' in ' '.join(call.args[1]) and call.args[1][0] == 'python3')
        extraction = next(i for i, call in enumerate(calls) if call.args[1][:2] == ['python3', '-c'] and 'import ctypes' in call.args[1][2])
        self.assertLess(authentication, extraction)
        self.assertFalse(any(call.args[1][0] == 'tar' for call in calls))

    def test_worker_propagates_explicit_downgrade_and_development(self):
        runner = installer.Runner(unittest.mock.Mock(), unittest.mock.Mock())
        runner.prepared['node'] = '/fixture'
        runner.allow_downgrade = True
        self.assertIn('BETTERDOCKER_ALLOW_DOWNGRADE=1', runner.worker('node', 'repair'))
        self.assertNotIn('BETTERDOCKER_DEVELOPMENT_SOURCE=1', runner.worker('node', 'repair'))
        runner.development = True
        self.assertIn('BETTERDOCKER_DEVELOPMENT_SOURCE=1', runner.worker('node', 'repair'))

    def test_invalid_deb_never_calls_package_manager(self):
        package = self.root / 'proxmox-betterdockers_0.20.0_all.deb'
        package.write_text('ordinary changed package fixture')
        args = ['release.py', 'deb', str(package), '--manifest', str(self.root / release.MANIFEST), '--signature', str(self.root / release.SIGNATURE)]
        with patch.object(sys, 'argv', args), patch.object(release, 'trust', return_value=(self.public, self.pin)), patch.object(release, 'STATE', self.root / 'state'), patch.object(release.subprocess, 'run', wraps=subprocess.run) as run:
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                release.main()
            self.assertFalse(any(call.args[0][0] == 'dpkg' for call in run.call_args_list))

    def test_valid_deb_authorization_is_private_and_removed(self):
        package = self.root / 'proxmox-betterdockers_0.20.0_all.deb'
        package.write_text('ordinary signed package fixture')
        self.metadata['artifacts'][package.name] = release.digest(package)
        self.sign()
        args = ['release.py', 'deb', str(package), '--manifest', str(self.root / release.MANIFEST), '--signature', str(self.root / release.SIGNATURE), '--allow-downgrade']
        real_run = subprocess.run
        called = []
        def run(command, **kwargs):
            if command[0] != 'dpkg':
                return real_run(command, **kwargs)
            copied = Path(command[-1])
            self.assertEqual(copied.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(copied.read_bytes(), package.read_bytes())
            self.assertEqual((copied.parent / release.MANIFEST).read_bytes(), (self.root / release.MANIFEST).read_bytes())
            self.assertEqual(kwargs['env']['BETTERDOCKER_ALLOW_DOWNGRADE'], '1')
            called.append(command)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(sys, 'argv', args), patch.object(release, 'trust', return_value=(self.public, self.pin)), patch.object(release, 'STATE', self.root / 'installer/state'), patch.object(release, 'protected'), patch.object(release, 'package_lock', return_value=contextlib.nullcontext()), patch.object(os, 'geteuid', return_value=0), patch.object(subprocess, 'run', side_effect=run):
            release.main()
        self.assertEqual(len(called), 1)
        self.assertFalse((self.root / 'installer/deb-authorization').exists())

    def test_deb_authorization_serializes_concurrent_installers(self):
        with patch.object(release, 'STATE', self.root / 'installer/state'), patch.object(release, 'protected'):
            with release.package_lock():
                with self.assertRaises(BlockingIOError):
                    with release.package_lock():
                        self.fail('Concurrent installer admitted')
            with release.package_lock():
                pass

    def test_unsafe_deb_authorization_lock_is_rejected(self):
        directory = self.root / 'installer'; directory.mkdir()
        lock = directory / 'release-deb.lock'; lock.write_text('ordinary fixture'); lock.chmod(0o666)
        with patch.object(release, 'STATE', directory / 'state'), patch.object(release, 'protected'):
            with self.assertRaisesRegex(ValueError, 'Unprotected'):
                with release.package_lock():
                    self.fail('Unsafe lock admitted')

    def test_source_admission_uses_actual_signature(self):
        with patch.object(release, 'trust', return_value=(self.public, self.pin)), patch.object(release, 'protected'), patch.object(release, 'STATE', self.root / 'installed.json'):
            self.assertEqual(release.source(self.root)['version'], self.version)

    def test_missing_signed_source_is_not_treated_as_development(self):
        (self.root / release.SIGNATURE).unlink()
        with patch.object(release, 'trust', return_value=(self.public, self.pin)), patch.object(release, 'protected'), patch.object(release, 'STATE', self.root / 'installed.json'):
            with self.assertRaises(OSError):
                release.source(self.root)

    def test_source_version_must_match_signed_metadata(self):
        (self.root / 'scripts/dctlib/version.py').write_text("VERSION = '0.19.0'\n")
        self.metadata['source'] = release.tree(self.root)
        self.sign()
        with patch.object(release, 'trust', return_value=(self.public, self.pin)), patch.object(release, 'protected'), patch.object(release, 'STATE', self.root / 'installed.json'):
            with self.assertRaisesRegex(ValueError, 'Source version differs'):
                release.source(self.root)

    def test_bootstrap_and_worker_verify_before_installation(self):
        bootstrap = (ROOT / 'install.sh').read_text()
        self.assertLess(bootstrap.index('openssl dgst'), bootstrap.index('--output "$project_dir/project.tar.gz"'))
        self.assertLess(bootstrap.index('Installer checksum mismatch'), bootstrap.index('python3 "$project_dir/scripts/installer.py"'))
        worker = (ROOT / 'start.sh').read_text()
        self.assertLess(worker.index('scripts/release.py" source'), worker.index('exec 9>'))
        self.assertIn('--development-source', worker)
