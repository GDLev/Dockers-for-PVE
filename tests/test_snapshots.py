"""Snapshot ownership, integrity, per-instance restoration and LXC operation semantics."""
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker

ID = 'a' * 32


class SnapshotTests(unittest.TestCase):
    def test_cleanup_commands_can_return_nonzero_without_masking_result(self):
        with patch.object(worker.subprocess, 'run') as run:
            worker.run(['docker', 'image', 'rm', 'snapshot'], check=False, capture_output=True)
            self.assertFalse(run.call_args.kwargs['check'])

    def test_snapshot_restores_notes_and_environment_from_actual_configuration_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'projects'
            project = root / ID
            project.mkdir(parents=True)
            (project / 'notes.txt').write_text('Original notes')
            (project / '.env').write_text('SECRET=original')
            (project / 'request.json').write_text('{}')
            (project / 'compose.source.yaml').write_text('services: {}')
            worker.write_json(project / 'compose.runtime.json', {'services': {'app': {'image': 'busybox'}}})
            info = dict(id=ID, node='pve', vmid=101, address='10.0.0.1', name='test', status='stopped')
            container = dict(Id='old', Name='/app', Config={'Image': 'busybox', 'Labels': {'com.docker.compose.service': 'app'}}, HostConfig={}, Mounts=[], NetworkSettings={})
            def capture(args):
                if args[0] == 'findmnt':
                    return json.dumps({'filesystems': []})
                if args[:3] == ['docker', 'image', 'inspect']:
                    return 'sha256:snapshot'
                return ''
            def run(args, **kwargs):
                if args[:2] == ['docker', 'commit']:
                    return subprocess.CompletedProcess(args, 0, stdout='Flag --pause has been deprecated\nsha256:snapshot\n')
                if args[:3] == ['docker', 'image', 'save']:
                    Path(args[args.index('-o') + 1]).write_bytes(b'archive')
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'METADATA', Path(temporary) / 'metadata'), patch.object(worker, 'detail', return_value=info), patch.object(worker, 'snapshot_containers', return_value=[container]), patch.object(worker, 'capture', side_effect=capture), patch.object(worker, 'run', side_effect=run), patch.object(worker, 'docker_engine', return_value={'Id': 'new'}), patch.object(worker, 'refresh'):
                worker.snapshot_capture(ID, 'pve', 'before')
                saved = json.loads((worker.snapshot_root(ID) / 'before' / 'containers.json').read_text())
                self.assertEqual(saved[0]['SnapshotImage'], 'sha256:snapshot')
                (project / 'notes.txt').write_text('Changed notes')
                (project / '.env').write_text('SECRET=changed')
                worker.snapshot_apply(ID, 'pve', 'before')
                self.assertEqual((project / 'notes.txt').read_text(), 'Original notes')
                self.assertEqual((project / '.env').read_text(), 'SECRET=original')

    def test_names_cannot_escape_snapshot_directory(self):
        for value in ('../x', 'x/y', 'current', 'vzdump', '-x', 'a' * 41, 'x\n', 'x;id'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.snapshot_name(value)
        self.assertEqual(worker.snapshot_name('before-update_1'), 'before-update_1')

    def test_snapshot_integrity_detects_changed_files_links_and_missing_directories(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'):
            folder = worker.snapshot_root(ID) / 'before'
            folder.mkdir(parents=True)
            (folder / 'empty').mkdir()
            (folder / 'data').write_text('original')
            (folder / 'link').symlink_to('data')
            (folder / 'manifest.json').write_text(json.dumps({'version': 1, 'id': ID, 'name': 'before'}))
            worker.write_json(folder / 'checksums.json', worker.snapshot_digest(folder))
            worker.snapshot_check(ID, 'before')
            (folder / 'data').write_text('modified')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                worker.snapshot_check(ID, 'before')
            (folder / 'data').write_text('original')
            (folder / 'empty').rmdir()
            with self.assertRaisesRegex(ValueError, 'integrity'):
                worker.snapshot_check(ID, 'before')

    def test_running_or_paused_containers_are_rejected(self):
        for state in ('running', 'paused', 'restarting', 'removing', 'dead'):
            with patch.object(worker, 'container_records', return_value=[{'state': state}]), self.assertRaisesRegex(ValueError, 'stable'):
                worker.snapshot_containers(ID)

    def test_external_bind_mount_rejected_before_copy(self):
        row = {'Id': 'own', 'Mounts': [{'Type': 'bind', 'Source': '/etc', 'RW': True}]}
        with self.assertRaisesRegex(ValueError, 'external'):
            worker.snapshot_mounts(ID, [row])

    def test_shared_volume_is_rejected(self):
        row = {'Id': 'own', 'Mounts': [{'Type': 'volume', 'Source': '/tmp/volume', 'Name': 'data', 'RW': True}]}
        volume = {'Driver': 'local', 'Labels': {'com.docker.compose.project': 'bd-' + ID}, 'Mountpoint': '/tmp/volume'}
        with patch.object(worker, 'capture', side_effect=[json.dumps([volume]), 'own\nother\n']), self.assertRaisesRegex(ValueError, 'shared'):
            worker.snapshot_mounts(ID, [row])

    def test_restore_can_recover_missing_owned_directory_without_accepting_external_paths(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary)):
            source = Path(temporary) / ID / 'missing-data'
            row = {'Id': 'own', 'Mounts': [{'Type': 'bind', 'Source': str(source), 'RW': True}]}
            with self.assertRaisesRegex(ValueError, 'directory'):
                worker.snapshot_mounts(ID, [row])
            self.assertEqual(worker.snapshot_mounts(ID, [row], allow_missing=True)[0]['source'], str(source))
            row['Mounts'][0]['Source'] = '/missing-external-data'
            with self.assertRaisesRegex(ValueError, 'external'):
                worker.snapshot_mounts(ID, [row], allow_missing=True)

    def test_overlapping_project_mounts_are_copied_once(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary)):
            root = Path(temporary) / ID / 'data'
            (root / 'nested').mkdir(parents=True)
            row = {'Id': 'own', 'Mounts': [{'Type': 'bind', 'Source': str(path), 'RW': True} for path in (root, root / 'nested')]}
            self.assertEqual(len(worker.snapshot_mounts(ID, [row])), 1)

    def test_list_never_exposes_configuration_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), patch.object(worker, 'detail'):
            folder = worker.snapshot_root(ID) / 'before'
            folder.mkdir(parents=True)
            worker.write_json(folder / 'manifest.json', dict(version=1, id=ID, name='before', created=1,
                description='upgrade', size=123, recovery=False, mounts=[{'source': 'SECRET'}]))
            self.assertNotIn('SECRET', json.dumps(worker.snapshot_list(ID, 'pve')))

    def test_restore_stops_running_guest_and_never_creates_another_snapshot(self):
        records = [{'id': 'one', 'state': 'running'}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary)), patch.object(worker, 'locked'), patch.object(worker, 'detail'), patch.object(worker, 'container_records', return_value=records), patch.object(worker, 'snapshot_capture') as capture, patch.object(worker, 'snapshot_apply') as apply, patch.object(worker, 'run') as run:
            worker.project_snapshot(ID, 'pve', 'restore', 'before')
            capture.assert_not_called()
            self.assertEqual(apply.call_count, 2)
            self.assertTrue(apply.call_args_list[0].kwargs['check_only'])
            run.assert_called_once_with(['docker', 'stop', '--time', '0', 'one'], timeout=1800, capture_output=True)

    def test_invalid_rollback_does_not_stop_running_guest(self):
        with patch.object(worker, 'locked'), patch.object(worker, 'detail'), patch.object(worker, 'snapshot_capture') as capture, patch.object(worker, 'snapshot_apply', side_effect=ValueError('integrity')), patch.object(worker, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'integrity'):
                worker.project_snapshot(ID, 'pve', 'restore', 'before')
            capture.assert_not_called()
            run.assert_not_called()

    def test_live_snapshot_always_resumes_paused_services_after_failure(self):
        records = [{'id': 'one', 'state': 'running'}, {'id': 'two', 'state': 'exited'}]
        with patch.object(worker, 'container_records', return_value=records), patch.object(worker, 'run') as run, patch.object(worker.os, 'sync'):
            with self.assertRaisesRegex(ValueError, 'copy failed'):
                with worker.snapshot_quiesce(ID):
                    raise ValueError('copy failed')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands, [['docker', 'pause', 'one'], ['docker', 'unpause', 'one']])

    def test_restored_start_preserves_each_replica_identity(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary)), patch.object(worker, 'run') as run:
            folder = Path(temporary) / ID
            folder.mkdir()
            worker.write_json(folder / 'snapshot-restored.json', {'containers': ['one', 'two'], 'address': '10.0.0.1'})
            worker.write_json(folder / 'compose.runtime.json', {'services': {'web': {}}})
            records = [{'id': 'one', 'service': 'web'}, {'id': 'two', 'service': 'web'}]
            with patch.object(worker, 'container_records', return_value=records):
                self.assertTrue(worker.restored_start(ID))
            run.assert_called_once_with(['docker', 'start', 'one', 'two'], timeout=1800)
            with patch.object(worker, 'container_records', return_value=records[:1]), self.assertRaisesRegex(ValueError, 'instances changed'):
                worker.restored_start(ID)

    def test_tree_parent_position_and_delete_reparenting(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), patch.object(worker, 'detail', return_value={'status': 'stopped'}), patch.object(worker, 'locked'):
            for index, name in enumerate(('first', 'second', 'branch')):
                path = worker.snapshot_root(ID) / name
                path.mkdir(parents=True)
                worker.write_json(path / 'manifest.json', dict(version=1, id=ID, name=name, created=index + 1, description=name, size=0, recovery=False))
            worker.snapshot_tree_write(ID, {'parents': {'first': '', 'second': 'first', 'branch': 'first'}, 'descriptions': {}, 'current': 'second'})
            rows = {row['name']: row for row in worker.snapshot_list(ID, 'pve')}
            self.assertEqual(rows['current']['parent'], 'second')
            self.assertEqual(rows['branch']['parent'], 'first')
            worker.project_snapshot(ID, 'pve', 'delete', 'second')
            rows = {row['name']: row for row in worker.snapshot_list(ID, 'pve')}
            self.assertEqual(rows['current']['parent'], 'first')
            worker.project_snapshot(ID, 'pve', 'delete', 'first')
            rows = {row['name']: row for row in worker.snapshot_list(ID, 'pve')}
            self.assertEqual(rows['branch']['parent'], '')
            self.assertEqual(rows['current']['parent'], '')

    def test_description_edit_does_not_change_snapshot_integrity_or_expose_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker, 'ROOT', Path(temporary) / 'projects'), patch.object(worker, 'detail', return_value={'status': 'stopped'}), patch.object(worker, 'locked'):
            folder = worker.snapshot_root(ID) / 'first'
            configs = folder / 'configuration'
            configs.mkdir(parents=True)
            worker.write_json(folder / 'manifest.json', dict(version=1, id=ID, name='first', created=1, description='before', size=0, recovery=False))
            worker.write_json(configs / 'metadata.json', {'name': 'web', 'bridge': 'vmbr0', 'address': '10.0.0.1'})
            worker.write_json(configs / 'compose.runtime.json', {'services': {'app': {'image': 'busybox', 'environment': {'PASSWORD': 'secret'}, 'command': 'secret'}}})
            worker.write_json(folder / 'checksums.json', worker.snapshot_digest(folder))
            old_digest = worker.snapshot_list(ID, 'pve')[-1]['digest']
            worker.snapshot_edit(ID, 'pve', 'first', 'changed description')
            worker.snapshot_check(ID, 'first')
            response = worker.snapshot_config(ID, 'pve', 'first')
            self.assertEqual(response['description'], 'changed description')
            self.assertNotIn('secret', json.dumps(response))
            self.assertNotEqual(worker.snapshot_list(ID, 'pve')[-1]['digest'], old_digest)

    def test_live_snapshot_can_resume_before_archive_serialization(self):
        with patch.object(worker, 'container_records', return_value=[{'id': 'one', 'state': 'running'}]), patch.object(worker, 'run') as run, patch.object(worker.os, 'sync'):
            with worker.snapshot_quiesce(ID) as resume:
                resume()
                self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_count, 2)
