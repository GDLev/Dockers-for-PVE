"""Filesystem policy contracts with ordinary fixtures and a mocked Engine."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker as w
from dctlib import filesystem_policy as f, hardening as h

ID = 'a' * 32


class FilesystemPolicy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for target, attr, value in ((f, 'POLICY', self.root / 'policy.json'), (w, 'ROOT', self.root / 'projects')):
            guard = patch.object(target, attr, value); guard.start(); self.addCleanup(guard.stop)
        self.rootless = SimpleNamespace(rootless=True)
        self.storage = {'volid': 'local:disk', 'size_mib': 512, 'additional': {'mp1': {'volid': 'local:extra', 'size_mib': 256}}}
        for guard in (patch.object(w, 'storage_config', return_value=self.storage), patch.object(h, 'available')):
            guard.start(); self.addCleanup(guard.stop)
        self.service = {'labels': {'betterdocker.id': ID}, 'image': 'fixture', 'volumes': []}

    def enable(self):
        f.POLICY.write_text(json.dumps({'require_read_only_rootless': True}))

    def test_opt_in_preserves_existing_modes(self):
        self.assertFalse(f.required(self.rootless, 'hardened'))
        self.enable()
        self.assertTrue(f.required(self.rootless, 'hardened'))
        self.assertFalse(f.required(self.rootless, 'standard'))
        self.assertFalse(f.required(SimpleNamespace(rootless=False), 'hardened'))

    def test_policy_rejects_unknown_keys_and_non_boolean(self):
        for value in ({'require_read_only_rootless': 1}, {'unknown': True}, []):
            f.POLICY.write_text(json.dumps(value))
            with self.assertRaises(ValueError): f.required(self.rootless, 'hardened')

    def test_hardened_sets_readonly_and_rejects_explicit_downgrade(self):
        self.enable()
        value = {'services': {'app': copy.deepcopy(self.service)}}
        h.apply(value, self.rootless)
        self.assertTrue(value['services']['app']['read_only'])
        self.assertEqual(value['services']['app']['pull_policy'], 'never')
        value['services']['app']['read_only'] = False
        with self.assertRaises(ValueError): h.apply(value, self.rootless)

    def test_data_and_additional_disks_have_separate_positive_capacities(self):
        self.enable()
        for suffix in ('storage-data/data', 'mount-mp1'):
            service = copy.deepcopy(self.service)
            service['volumes'] = [{'type': 'bind', 'source': str(w.ROOT / ID / suffix), 'target': '/data'}]
            h.apply({'services': {'app': service}}, self.rootless)
        for change in ({'volid': '', 'size_mib': 512}, {'volid': 'local:disk', 'size_mib': 0}):
            with self.assertRaises(ValueError):
                f.config({'services': {'app': service}}, self.rootless, 'hardened', storage=change)

    def test_writable_unmanaged_mounts_rejected_readonly_allowed(self):
        self.enable()
        for item in ({'type': 'volume', 'source': 'app-data'}, {'type': 'bind', 'source': str(w.ROOT / ID / 'data')}, {'type': 'tmpfs'}):
            value = {'services': {'app': dict(self.service, volumes=[dict(item, target='/data')])}}
            with self.assertRaises(ValueError): f.config(value, self.rootless, 'hardened')
            value['services']['app']['volumes'][0]['read_only'] = True
            f.config(value, self.rootless, 'hardened')

    def test_tmpfs_requires_positive_size(self):
        self.enable()
        for item in ('/tmp:size=16m',):
            f.config({'services': {'app': dict(self.service, tmpfs=[item])}}, self.rootless, 'hardened')
        for item in ('/tmp', '/tmp:size=0', '/tmp:size=-1'):
            with self.assertRaises(ValueError):
                f.config({'services': {'app': dict(self.service, tmpfs=[item])}}, self.rootless, 'hardened')

    def test_pending_setting_cannot_disable_requirement(self):
        self.enable()
        with patch.object(h, 'mode', return_value='hardened'), patch.object(w.node_runtime, 'load', return_value=self.rootless):
            with self.assertRaises(ValueError): h.settings(self.root, {'read_only': False})
            h.settings(self.root, {'read_only': True})

    def test_preserved_rootfs_and_mount_enforcement(self):
        self.enable()
        body = {'Labels': self.service['labels'], 'HostConfig': {'Binds': [str(w.ROOT / ID / 'storage-data/data') + ':/data:rw']}, 'Volumes': {'/data': {}}}
        h.host(body, self.rootless, 'hardened', storage=self.storage)
        self.assertTrue(body['HostConfig']['ReadonlyRootfs'])
        body['HostConfig']['Binds'] = ['unlimited:/data:rw']
        with self.assertRaises(ValueError): h.host(body, self.rootless, 'hardened')

    def test_live_check_detects_writable_root_and_implicit_volumes(self):
        self.enable()
        body = {'Config': {'Labels': self.service['labels']}, 'HostConfig': {'ReadonlyRootfs': False}}
        with self.assertRaises(ValueError): f.host(body, self.rootless, 'hardened', live=True)
        body['HostConfig']['ReadonlyRootfs'] = True
        body['Mounts'] = [{'Type': 'volume', 'Source': '/ordinary/volume', 'RW': True}]
        with self.assertRaises(ValueError): f.host(body, self.rootless, 'hardened', live=True)

    def test_images_cannot_add_unbounded_anonymous_volumes(self):
        self.enable()
        image = SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Config': {'Volumes': {'/data': {}}}}]))
        with patch.object(w, 'run', return_value=image):
            with self.assertRaises(ValueError): f.compose_images({'services': {'app': self.service}}, self.rootless, 'hardened')
            service = dict(self.service, volumes=[{'target': '/data', 'type': 'tmpfs', 'tmpfs': {'size': 1024}}])
            f.compose_images({'services': {'app': service}}, self.rootless, 'hardened')
        with patch.object(w, 'capture', return_value=image.stdout):
            with self.assertRaises(ValueError): f.engine_image({'Image': 'fixture', 'HostConfig': {}}, self.rootless, 'hardened')

    def test_clone_preflight_uses_saved_disk_metadata_without_destination(self):
        self.enable()
        service = dict(self.service, volumes=[{'type': 'bind', 'source': str(w.ROOT / ID / 'storage-data/data'), 'target': '/data'}])
        with patch.object(w, 'storage_config', side_effect=AssertionError('destination not created')):
            result = h.preserved({'services': {'app': service}}, self.rootless, 'hardened', storage=self.storage)
            self.assertTrue(result['services']['app']['read_only'])

    def test_missing_mount_fails_instead_of_writing_to_host_root_disk(self):
        self.enable()
        service = dict(self.service, volumes=[{'type': 'bind', 'source': str(w.ROOT / ID / 'storage-data/data'), 'target': '/data'}])
        with patch('os.path.ismount', return_value=False), self.assertRaises(ValueError):
            f.config({'services': {'app': service}}, self.rootless, 'hardened', live=True)

    def test_backing_identity_is_checked_without_mounting(self):
        with patch('os.path.ismount', return_value=True), patch.object(w, 'pve_storage', return_value={'path': '/ordinary/disk'}), patch.object(w, 'capture', side_effect=['/dev/loop0', '/ordinary/disk']):
            f.backing(str(w.ROOT / ID / 'storage-data/data'), ID, self.storage)
        with patch('os.path.ismount', return_value=True), patch.object(w, 'pve_storage', return_value={'path': '/ordinary/disk'}), patch.object(w, 'capture', side_effect=['/dev/loop0', '/ordinary/other']), self.assertRaises(ValueError):
            f.backing(str(w.ROOT / ID / 'storage-data/data'), ID, self.storage)

    def test_api_reports_rootless_quota_rejection_before_guest_edits(self):
        with patch.object(w, 'rootfs_quota_support', return_value={'supported': False, 'reason': 'Writable-layer quotas are not supported by the rootless runtime'}), patch.object(w, 'locked', side_effect=AssertionError('guest touched')):
            with self.assertRaisesRegex(ValueError, 'rootless runtime'):
                w.service_settings(ID, 'node', 'app', {'disk': 512})

    def test_readonly_bind_must_not_also_request_readwrite(self):
        self.enable()
        body = {'Labels': self.service['labels'], 'HostConfig': {'Binds': ['unlimited:/data:ro,rw']}}
        with self.assertRaises(ValueError): f.host(body, self.rootless, 'hardened')

    def test_engine_accepts_explicit_sized_tmpfs_for_image_volume(self):
        self.enable()
        image = json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Config': {'Volumes': {'/data': {}}}}])
        body = {'Image': 'fixture', 'HostConfig': {'Tmpfs': {'/data': 'size=16m'}}}
        with patch.object(w, 'capture', return_value=image):
            f.engine_image(body, self.rootless, 'hardened')
            self.assertEqual(body['Image'], 'sha256:' + 'b' * 64)

    def test_compose_execution_pins_the_inspected_image_id(self):
        self.enable()
        image = SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Config': {'Volumes': {}}}]))
        with patch.object(w, 'run', return_value=image):
            pins = f.compose_images({'services': {'app': self.service}}, self.rootless, 'hardened')
        self.assertEqual(pins, {'app': 'sha256:' + 'b' * 64})
