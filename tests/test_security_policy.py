"""Workload-policy and hostile control-file boundaries."""
import copy
import io as bytes_io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_worker import worker as w
from dctlib import hardening as h, secure_io as io, parsing, process, archives

ID = 'a' * 32


class HardenedPolicy(unittest.TestCase):
    def setUp(self):
        self.selected = SimpleNamespace(rootless=False)
        guard = patch.object(h, 'available'); guard.start(); self.addCleanup(guard.stop)

    def test_default_caps_are_bounded_and_protections_are_explicit(self):
        config = {'services': {'app': {'image': 'nginx'}}}
        h.apply(config, self.selected)
        service = config['services']['app']
        self.assertEqual(service['cap_drop'], ['ALL'])
        self.assertEqual(set(service['cap_add']), h.CAPS)
        self.assertNotIn('NET_RAW', service['cap_add'])
        self.assertEqual(service['security_opt'], ['no-new-privileges:true', 'apparmor:' + h.PROFILE])
        self.assertEqual(h.apply(copy.deepcopy(config), self.selected), config)

    def test_explicit_drop_all_remains_stronger_than_default(self):
        config = {'services': {'app': {'cap_drop': ['ALL']}}}
        h.apply(config, self.selected)
        self.assertEqual(config['services']['app']['cap_add'], [])
        h.apply(config, self.selected)
        self.assertEqual(config['services']['app']['cap_add'], [])

    def test_rootless_has_no_apparmor_and_retains_nnp(self):
        config = {'services': {'app': {}}}
        h.apply(config, SimpleNamespace(rootless=True))
        self.assertEqual(config['services']['app']['security_opt'], ['no-new-privileges:true'])

    def test_unsafe_source_cannot_silently_downgrade_policy(self):
        for change in ({'privileged': True}, {'cap_add': ['SYS_ADMIN']}, {'pid': 'host'},
                       {'security_opt': ['seccomp:unconfined']}, {'security_opt': ['no-new-privileges:false']}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                h.apply({'services': {'app': change}}, self.selected)

    def test_preserved_body_keeps_empty_capability_set(self):
        body = {'HostConfig': {'CapDrop': ['ALL'], 'CapAdd': [], 'NetworkMode': 'none'}}
        h.host(body, self.selected, 'hardened')
        self.assertEqual(body['HostConfig']['CapAdd'], [])
        self.assertIn('no-new-privileges', body['HostConfig']['SecurityOpt'])

    def test_docker_api_capability_prefixes_are_normalized(self):
        body = {'HostConfig': {'CapDrop': ['ALL'], 'CapAdd': ['CAP_CHOWN'], 'NetworkMode': 'none'}}
        h.host(body, self.selected, 'hardened')
        self.assertEqual(body['HostConfig']['CapAdd'], ['CHOWN'])
        with patch.object(w.node_runtime, 'load', return_value=self.selected):
            w.validate_delegated_containers([{'HostConfig': {'CapDrop': ['ALL'], 'CapAdd': ['CAP_CHOWN']}}], ID)


class ControlFiles(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.key = self.base / 'recovery.key'
        io.atomic_text(self.key, 'test-key')
        guard = patch.object(io, 'KEY', self.key); guard.start(); self.addCleanup(guard.stop)

    def test_signed_journal_roundtrips_and_modified_content_is_rejected(self):
        path = self.base / 'settings-transaction.json'
        value = {'id': ID, 'phase': 'preparing'}
        io.atomic_text(path, json.dumps(io.seal(path, value)))
        self.assertEqual(io.read_json(path), value)
        corrupted = json.loads(path.read_text()); corrupted['id'] = 'b' * 32
        path.write_text(json.dumps(corrupted))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            io.read_json(path)

    def test_unsigned_journal_and_relocated_signature_are_rejected(self):
        a = self.base / 'snapshot-transaction.json'; b = self.base / 'disk-transaction.json'
        io.atomic_text(a, '{}')
        with self.assertRaisesRegex(ValueError, 'authenticated'): io.read_json(a)
        io.atomic_text(a, json.dumps(io.seal(a, {'id': ID})))
        b.write_bytes(a.read_bytes()); b.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'integrity'): io.read_json(b)

    def test_symlink_and_writable_ancestors_are_rejected(self):
        target = self.base / 'target.json'; target.write_text('{}')
        link = self.base / 'link.json'; link.symlink_to(target)
        with self.assertRaises(OSError): io.read_json(link)
        folder = self.base / 'bad'; folder.mkdir(); folder.chmod(0o777)
        (folder / 'config.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'writable'): io.read_json(folder / 'config.json')

    def test_private_credentials_and_size_limits_are_enforced(self):
        path = self.base / '.env'; path.write_text('SECRET=x'); path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'Credentials'): io.read_text(path)
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'oversized'): io.read_text(path, maximum=1)

    def test_environment_read_group_is_only_the_selected_runtime(self):
        path = self.base / '.env'; path.write_text('SECRET=x'); path.chmod(0o640)
        actual = os.fstat
        def owned(fd):
            result = actual(fd)
            return SimpleNamespace(st_uid=0, st_gid=1234, st_mode=result.st_mode, st_size=result.st_size)
        with patch('runtime.load', return_value=SimpleNamespace(rootless=True, gid=1234)), patch.object(io.os, 'fstat', owned):
            self.assertEqual(io.read_text(path), 'SECRET=x')
        with patch('runtime.load', return_value=SimpleNamespace(rootless=True, gid=1235)), patch.object(io.os, 'fstat', owned):
            with self.assertRaisesRegex(ValueError, 'Credentials'): io.read_text(path)

    def test_atomic_write_replaces_a_link_without_touching_its_target(self):
        target = self.base / 'target'; target.write_text('retained')
        link = self.base / 'config'; link.symlink_to(target)
        io.atomic_text(link, 'new')
        self.assertEqual(target.read_text(), 'retained')
        self.assertEqual(link.read_text(), 'new')

    def test_pinned_descriptor_reads_original_after_path_replacement(self):
        path = self.base / 'original'; path.write_text('retained')
        with io.anchored(path) as fd:
            path.rename(self.base / 'saved'); path.symlink_to('/etc/passwd')
            self.assertEqual(os.read(fd, 100), b'retained')

    def test_nesting_and_object_limits(self):
        value = []; current = value
        for _ in range(70): current.append([]); current = current[0]
        with self.assertRaisesRegex(ValueError, 'nesting'): io.structure(value)
        with self.assertRaisesRegex(ValueError, 'object'): io.structure(list(range(30)), maximum=10)


class ProcessBoundary(unittest.TestCase):
    def test_output_flood_is_stopped(self):
        with self.assertRaisesRegex(ValueError, 'output limit'):
            process.run([sys.executable, '-c', 'print("x"*100000)'], capture_output=True, text=True, output_limit=1000)

    def test_timeout_kills_descendants(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            process.run([sys.executable, '-c', 'import time;time.sleep(30)'], capture_output=True, timeout=.1)

    def test_path_override_does_not_select_attacker_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'true'; fake.write_text('attack'); fake.chmod(0o755)
            with patch.dict(os.environ, {'PATH': temporary}):
                self.assertNotEqual(process.executable('true'), str(fake))

    def test_arguments_are_literal_and_inherited_environment_is_removed(self):
        with patch.dict(os.environ, {'SECRET': 'must-not-pass'}):
            result = process.run([sys.executable, '-c', 'import os,sys;print(sys.argv[1]);print(os.getenv("SECRET"))',
                                  '$(touch unwanted); --flag'], capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(), ['$(touch unwanted); --flag', 'None'])

    def test_timeout_also_kills_children_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'survived'
            script = 'import time,pathlib;time.sleep(1);pathlib.Path(' + repr(str(marker)) + ').touch()'
            parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(script) + '])'
            with self.assertRaises(subprocess.TimeoutExpired):
                process.run([sys.executable, '-c', parent], capture_output=True, timeout=.2)
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_completed_parser_cannot_leave_background_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'survived'
            script = 'import os,time,pathlib;os.close(1);os.close(2);time.sleep(1);pathlib.Path(' + repr(str(marker)) + ').touch()'
            parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(script) + '])'
            self.assertEqual(process.run([sys.executable, '-c', parent], capture_output=True, timeout=3).returncode, 0)
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.geteuid() == 0, 'Root fixtures exercise the installed offline account in live acceptance')
    def test_yaml_alias_bomb_and_deep_nesting_are_bounded(self):
        for value in ('root: &root [*root]', 'root: ' + '[' * 90 + '0' + ']' * 90):
            with self.assertRaises(ValueError): parsing.parse(value)
        self.assertEqual(parsing.parse('services: {app: {image: nginx}}')['services']['app']['image'], 'nginx')

    @unittest.skipUnless(sys.platform == 'linux', 'Installed Linux Compose acceptance')
    def test_offline_compose_has_enough_virtual_space_without_daemon_access(self):
        result = parsing.parse('services: {app: {image: nginx, volumes: ["./data:/data"]}}',
                               directory='/var/lib/betterdocker/projects/example', canonical=True, identifier=ID)
        self.assertEqual(result['services']['app']['volumes'][0]['source'], '/var/lib/betterdocker/projects/example/data')


class ArchiveBoundary(unittest.TestCase):
    def scan(self, entries, capacity=1024 * 1024):
        with tempfile.TemporaryFile() as source:
            with tarfile.open(fileobj=source, mode='w:gz') as archive:
                for entry, content in entries:
                    archive.addfile(entry, bytes_io.BytesIO(content) if entry.isfile() else None)
            source.seek(0)
            return archives.members(source.fileno(), capacity)

    def test_safe_regular_archive_is_read_offline(self):
        entry = tarfile.TarInfo('data/file'); entry.size = 2
        self.assertEqual(self.scan([(entry, b'ok')])[0].name, 'data/file')

    def test_traversal_absolute_special_duplicate_and_expansion_are_rejected(self):
        for name in ('../outside', '/etc/shadow', 'data/../../outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.scan([(tarfile.TarInfo(name), b'')])
        special = tarfile.TarInfo('device'); special.type = tarfile.CHRTYPE
        with self.assertRaises(ValueError): self.scan([(special, b'')])
        entry = tarfile.TarInfo('same')
        with self.assertRaises(ValueError): self.scan([(entry, b''), (entry, b'')])
        entry = tarfile.TarInfo('expanded'); entry.size = 65536
        with self.assertRaises(ValueError): self.scan([(entry, b'\0' * entry.size)], capacity=1024)

    def test_docker_image_links_cannot_escape_or_follow_host_files(self):
        for name in ('/etc/shadow', '../outside'):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'images.tar'
                entry = tarfile.TarInfo('manifest.json'); entry.type = tarfile.SYMTYPE; entry.linkname = name
                with tarfile.open(path, 'w') as archive: archive.addfile(entry)
                with self.assertRaises(ValueError): archives.validate_images(path)
