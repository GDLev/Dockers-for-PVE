import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from test_worker import worker, patcher, BASE


class GuestIDTests(unittest.TestCase):
    def test_shared_ids_migration_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metadata, nodes = root / 'metadata', root / 'nodes'
            for kind, vmid in [('lxc', 100), ('qemu-server', 101)]:
                path = nodes / 'other-node' / kind / f'{vmid}.conf'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
            with patch.object(worker, 'METADATA', metadata), patch.object(worker, 'PVE_NODES', nodes):
                metadata.mkdir()
                legacy = metadata / ('a' * 32 + '.json')
                legacy.write_text(json.dumps({'id': 'a' * 32, 'node': 'pve', 'name': 'web', 'address': '192.0.2.50'}))
                self.assertEqual(worker.reserve_id('a' * 32, 'pve'), 102)
                self.assertEqual(worker.reserve_id('a' * 32, 'pve'), 102)
                self.assertEqual(json.loads(legacy.read_text())['address'], '192.0.2.50')
                self.assertEqual(json.loads(legacy.read_text())['vmid'], 102)
                worker.write_json(legacy, {'id': 'a' * 32, 'node': 'pve', 'status': 'running'})
                self.assertEqual(json.loads(legacy.read_text())['vmid'], 102)
                self.assertEqual(worker.reserve_id('b' * 32, 'other-node'), 103)
                for used in (100, 101, 102, 103):
                    with self.assertRaisesRegex(ValueError, 'already in use'):
                        worker.reserve_id('c' * 32, 'pve', used)
                worker.release_id('b' * 32)
                self.assertEqual(worker.reserve_id('c' * 32, 'pve'), 103)

    def test_invalid_numeric_ids(self):
        for vmid in (True, 99, 1000000000, '101', 101.5):
            with self.subTest(vmid=vmid), self.assertRaisesRegex(ValueError, 'DCT ID'):
                worker.validate(dict(BASE, vmid=vmid))

    def test_native_guest_check_patch_is_compatible_with_upstream(self):
        source = 'sub check_vmid_unused {\n    my ($vmid, $noerr) = @_;\n    return 1;\n}\n'
        patched = patcher.transform(source, 'vmid')
        self.assertIn('/etc/pve/betterdocker/ids/$vmid.json', patched)
        self.assertEqual(patcher.transform(patched, 'vmid'), patched)
        self.assertEqual(patcher.transform(patched, 'vmid', remove=True), source)
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder)
            (registry / '101.json').write_text('{}')
            code = patched.replace('/etc/pve/betterdocker/ids/', str(registry) + '/')
            code += '''
die 'free ID blocked' unless check_vmid_unused(102);
die 'reserved ID reported free' if check_vmid_unused(101, 1);
eval { check_vmid_unused(101); };
die 'missing readable collision error' unless $@ =~ /DCT 101 already exists/;
'''
            subprocess.run(['perl', '-e', code], check=True)

    def test_backup_patch_is_reversible_and_rejects_unknown_upstream(self):
        source = 'sub get_included_guests {\n    my ($job) = @_;\n    return {};\n}\n'
        patched = patcher.transform(source, 'vzdump')
        self.assertIn("check_native_guest_ids('backup', $job->{vmid})", patched)
        self.assertEqual(patcher.transform(patched, 'vzdump'), patched)
        self.assertEqual(patcher.transform(patched, 'vzdump', remove=True), source)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported VZDump'):
            patcher.transform('sub get_included_guests { return {}; }', 'vzdump')
