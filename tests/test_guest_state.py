"""Portable state must preserve owned data and reject archives that escape it."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker

spec = importlib.util.spec_from_file_location('guest_state', Path(__file__).resolve().parents[1] / 'scripts/guest_state.py')
state = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'provision': worker}):
    spec.loader.exec_module(state)

ID = 'a' * 32
NEW_ID = 'b' * 32


def fixture(folder):
    folder.mkdir()
    (folder / 'configuration').mkdir()
    (folder / 'data').mkdir()
    (folder / 'images.tar').write_bytes(b'image layers')
    state.write(folder / 'configuration/request.json', {'name': 'app'})
    state.write(folder / 'configuration/metadata.json', {'id': ID})
    state.write(folder / 'configuration/compose.runtime.json', {'name': 'bd-' + ID, 'services': {
        'app': {'image': 'nginx', 'labels': {'betterdocker.id': ID}, 'ports': [{'host_ip': '192.0.2.50', 'target': 80}]}},
        'volumes': {'data': {'name': 'bd-' + ID + '_data'}},
        'networks': {'default': {'name': 'bd-' + ID + '_default', 'ipam': {'config': [{'subnet': '172.18.0.0/16'}]}}}})
    state.write(folder / 'containers.json', [{'Id': '1' * 64, 'Config': {'Labels': {
        'betterdocker.id': ID, 'com.docker.compose.service': 'app'}},
        'Mounts': [{'Type': 'volume', 'Name': 'bd-' + ID + '_data'}],
        'SnapshotImage': 'sha256:' + '2' * 64, 'SnapshotTag': 'betterdocker-export/' + ID + ':' + '3' * 32}])
    state.write(folder / 'networks.json', [{'Name': 'bd-' + ID + '_default'}])
    state.write(folder / 'manifest.json', {'version': 1, 'kind': 'dct-state', 'id': ID, 'vmid': 100, 'mounts': []})
    seal(folder)


def seal(folder):
    state.write(folder / 'checksums.json', worker.snapshot_digest(folder))


class PortableStateTests(unittest.TestCase):
    def test_custom_docker_subnet_cannot_be_silently_dropped(self):
        runtime = {'networks': {'app': {'ipam': {'config': [{'subnet': '172.30.0.0/16'}]}}},
                   'services': {'app': {'image': 'busybox'}}}
        with self.assertRaisesRegex(ValueError, 'custom Docker IPAM'):
            state.check_portable_networks(runtime)

    def test_static_internal_addresses_are_rejected_but_service_aliases_are_supported(self):
        endpoint = {'aliases': ['database']}
        runtime = {'services': {'app': {'networks': {'default': endpoint}}}}
        state.check_portable_networks(runtime)
        for key, value in [('ipv4_address', '172.30.0.10'), ('ipv6_address', 'fd00::10'),
                           ('link_local_ips', ['169.254.1.1'])]:
            with self.subTest(key=key):
                endpoint[key] = value
                with self.assertRaisesRegex(ValueError, 'fixed internal Docker addresses'):
                    state.check_portable_networks(runtime)
                endpoint.pop(key)

    def test_changed_image_layer_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'state'
            fixture(folder)
            (folder / 'images.tar').write_bytes(b'changed image layers')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                state.check_state(folder)

    def test_configuration_links_are_not_read_even_with_matching_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'state'
            fixture(folder)
            secret = Path(temporary) / 'secret'
            secret.write_text('{}')
            (folder / 'configuration/request.json').unlink()
            (folder / 'configuration/request.json').symlink_to(secret)
            seal(folder)
            with self.assertRaisesRegex(ValueError, 'configuration file'):
                state.check_state(folder)

    def test_clone_changes_id_mounts_networks_and_published_ip_without_rewriting_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'state'
            fixture(folder)
            runtime_path = folder / 'configuration/compose.runtime.json'
            runtime = state.read(runtime_path)
            runtime['services']['app']['environment'] = {'SECRET': ID}
            state.write(runtime_path, runtime)
            seal(folder)
            manifest, runtime, volumes, networks, remap = state.adapted_state(folder, NEW_ID, '192.0.2.51')
            self.assertEqual(runtime['services']['app']['environment']['SECRET'], ID)
            self.assertEqual(runtime['services']['app']['labels']['betterdocker.id'], NEW_ID)
            self.assertEqual(runtime['services']['app']['ports'][0]['host_ip'], '192.0.2.51')
            self.assertEqual(runtime['volumes']['data']['name'], volumes['bd-' + ID + '_data'])
            self.assertEqual(runtime['networks']['default']['name'], networks['bd-' + ID + '_default'])
            self.assertNotIn('ipam', runtime['networks']['default'])
            self.assertEqual(remap(str(worker.ROOT / ID / 'data')), str(worker.ROOT / NEW_ID / 'data'))
            with self.assertRaisesRegex(ValueError, 'external mount'):
                remap('/etc')

    def test_another_project_cannot_supply_the_container_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'state'
            fixture(folder)
            containers = state.read(folder / 'containers.json')
            containers[0]['Config']['Labels']['betterdocker.id'] = NEW_ID
            state.write(folder / 'containers.json', containers)
            seal(folder)
            with self.assertRaisesRegex(ValueError, 'ownership'):
                state.check_state(folder)

    def test_traversal_and_writes_beneath_links_never_reach_tar_extraction(self):
        for names in ('traversal', 'symlink-parent', 'hardlink-configuration', 'duplicate'):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / 'backup.tar'
                with tarfile.open(archive, 'w') as handle:
                    if names == 'traversal':
                        member = tarfile.TarInfo('../outside'); member.size = 1
                        handle.addfile(member, io.BytesIO(b'x'))
                    elif names == 'symlink-parent':
                        member = tarfile.TarInfo('data/0'); member.type = tarfile.SYMTYPE; member.linkname = '/tmp'
                        handle.addfile(member)
                        member = tarfile.TarInfo('data/0/outside'); member.size = 1
                        handle.addfile(member, io.BytesIO(b'x'))
                    elif names == 'hardlink-configuration':
                        member = tarfile.TarInfo('configuration/request.json'); member.type = tarfile.LNKTYPE; member.linkname = '../../secret'
                        handle.addfile(member)
                    else:
                        for _ in range(2):
                            member = tarfile.TarInfo('manifest.json'); member.size = 1
                            handle.addfile(member, io.BytesIO(b'x'))
                folder = Path(temporary) / 'restore'
                with patch.object(worker, 'run') as run, self.assertRaises(ValueError):
                    state.unpack_state(archive, folder)
                run.assert_not_called()
                self.assertFalse(folder.exists())

    def test_migration_history_cannot_write_through_configuration_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'transfer.tar'
            with tarfile.open(archive, 'w') as handle:
                member = tarfile.TarInfo('history/before/configuration/request.json')
                member.type = tarfile.SYMTYPE; member.linkname = '/etc/shadow'
                handle.addfile(member)
            with patch.object(worker, 'run') as run, self.assertRaisesRegex(ValueError, 'configuration cannot contain links'):
                state.unpack_state(archive, Path(temporary) / 'target', history=True)
            run.assert_not_called()

    def test_backup_rejects_external_read_only_bind_mounts_too(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / 'outside'; outside.mkdir()
            with patch.object(worker, 'ROOT', Path(temporary) / 'projects'):
                containers = [{'Id': 'container', 'Mounts': [{'Type': 'bind', 'Source': str(outside), 'RW': False}]}]
                with self.assertRaisesRegex(ValueError, 'project-owned'):
                    state.owned_mounts(ID, containers)

    def test_docker_anonymous_marker_is_accepted_only_for_exclusive_local_volumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'volume'; source.mkdir()
            name = '1' * 64
            container = {'Id': 'mine', 'Mounts': [{'Type': 'volume', 'Source': str(source), 'Name': name}]}
            volume = {'Driver': 'local', 'Options': None, 'Labels': {'com.docker.volume.anonymous': ''}, 'Mountpoint': str(source)}
            def capture(command):
                if command[1:3] == ['volume', 'inspect']:
                    return json.dumps([volume])
                if command[1] == 'ps':
                    return users
                return '{"filesystems": []}'
            with patch.object(worker, 'capture', side_effect=capture), patch.object(worker, 'storage_config', return_value=None):
                users = 'mine'
                self.assertEqual(state.owned_mounts(ID, [container])[0]['volume'], name)
                users = 'mine foreign'
                with self.assertRaisesRegex(ValueError, 'shared'):
                    state.owned_mounts(ID, [container])

    def test_templates_allow_capture_but_block_deployment_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), patch.object(worker, 'METADATA', Path(temporary) / 'metadata'), patch.object(worker, 'PROC', Path(temporary) / 'proc'):
            process = worker.PROC / str(os.getpid())
            process.mkdir(parents=True)
            (process / 'stat').write_text(str(os.getpid()) + ' (python) ' + ' '.join(['0'] * 19 + ['123']))
            state.write(worker.METADATA / (ID + '.json'), {'id': ID, 'template': True})
            for name in ('update', 'settings', 'network', 'snapshot-restore'):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'immutable'):
                    with worker.operation(ID, name):
                        self.fail('Template mutation acquired an operation lock')
            with worker.operation(ID, 'backup'):
                self.assertTrue(worker.operation_state(ID)['active'])

    def test_cancellation_recovery_resumes_only_its_own_containers(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'):
            folder = worker.ROOT / ID; folder.mkdir(parents=True)
            state.write(state.journal_path(ID), {'paused': ['mine', 'foreign'], 'restart': ['stopped', 'foreign'],
                                                'tags': ['betterdocker-export/' + ID + ':' + '3' * 32]})
            with patch.object(worker, 'container_records', return_value=[{'id': 'mine', 'state': 'paused'},
                                                                         {'id': 'stopped', 'state': 'exited'}]), patch.object(worker, 'run') as run, patch('dctlib.recovery.require_docker'), patch.object(worker, 'detail', return_value={'id': ID}), patch.object(worker, 'current_node', return_value='pve'), patch.object(worker, 'prepare_network_start'), patch.object(worker, 'prepare_firewall'), patch.object(worker, 'finish_network_start'), patch('dctlib.hardening.check_profile'):
                state.cleanup_capture(ID)
            self.assertEqual(run.call_args_list[0].args[0], ['docker', 'unpause', 'mine'])
            self.assertEqual(run.call_args_list[1].args[0], ['docker', 'start', 'stopped'])
            self.assertFalse(state.journal_path(ID).exists())
            self.assertFalse(any('foreign' in item.args[0] for item in run.call_args_list))

    def test_active_export_is_not_recovered_by_background_refresh(self):
        with patch.object(worker, 'operation_state', return_value={'active': True}), patch.object(state, 'cleanup_capture') as cleanup:
            state.recover(ID)
            cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
