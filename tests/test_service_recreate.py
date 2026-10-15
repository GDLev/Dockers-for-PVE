"""Service swaps retain replicas and restore originals after interruption."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_worker import worker
from dctlib import service_recreate as swap

ID = 'ab' * 16
IMAGE = 'sha256:' + 'e' * 64


def source(index):
    return {'Id': str(index) * 64, 'Name': '/web-' + str(index), 'Image': IMAGE,
        'State': {'Status': 'exited'}, 'SizeRw': 0,
        'Config': {'User': '', 'Labels': {'betterdocker.id': ID, 'com.docker.compose.service': 'web', 'com.docker.compose.image': IMAGE}},
        'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}, 'SecurityOpt': ['seccomp=original'], 'Mounts': [{'Type': 'volume', 'Target': '/data', 'Source': ''}]},
        'Mounts': [{'Type': 'volume', 'Name': 'anonymous-' + str(index), 'Destination': '/data', 'RW': True}],
        'NetworkSettings': {'Networks': {'owned': {'Aliases': ['web'], 'EndpointID': 'endpoint'}}}}


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.directory = self.root / ID; self.directory.mkdir()
        self.config = {'services': {'web': {'restart': 'unless-stopped'}, 'db': {}}}
        worker.write_json(self.directory / 'compose.runtime.json', self.config)
        worker.write_json(self.directory / 'pending.json', {'web': {'read_only': True}})
        self.rows = {row['Id']: row for row in (source(1), source(2))}
        self.calls = []
        self.creates = 0
        self.fail_at = None
        self.interrupt = False
        patches = [patch.object(worker, 'LOCK', self.root / 'node.lock'), patch.object(worker, 'ROOT', self.root), patch.object(worker, 'storage_config', return_value={}),
            patch.object(worker, 'container_records', side_effect=lambda _: [{'id': r['Id'], 'service': 'web', 'state': 'exited'} for r in self.rows.values() if r['Id'][0] in '12']),
            patch.object(worker, 'capture', side_effect=lambda command: str(self.root) if command[:2] == ['docker', 'info'] else json.dumps([copy.deepcopy(self.rows[str(i)*64]) for i in (1, 2)])),
            patch.object(worker, 'run', side_effect=self.docker_run), patch.object(worker, 'docker_engine', side_effect=self.engine)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def docker_run(self, command, **kwargs):
        self.calls.append(command)
        kind = command[1]
        if kind == 'commit':
            return Mock(returncode=0, stdout=IMAGE)
        if command[:3] == ['docker', 'volume', 'inspect']:
            return Mock(returncode=0, stdout=json.dumps([{'Driver': 'local', 'Labels': {'com.docker.compose.project': 'bd-' + ID}}]))
        if kind == 'inspect':
            row = next((r for r in self.rows.values() if command[2] in (r['Id'], r['Name'].lstrip('/'))), None)
            return Mock(returncode=0 if row else 1, stdout=json.dumps([copy.deepcopy(row)]), stderr='' if row else 'error: no such object: '+command[2])
        if kind == 'rename':
            self.rows[command[2]]['Name'] = '/' + command[3]
        if kind == 'rm':
            del self.rows[command[2]]
        return Mock(returncode=0, stdout='')

    def engine(self, method, path, body):
        if '/update' in path:
            self.rows[path.split('/')[2]]['HostConfig']['RestartPolicy'] = body['RestartPolicy']
        elif path.endswith('/disconnect'):
            self.rows[body['Container']]['NetworkSettings']['Networks'] = {}
        elif path.endswith('/connect'):
            self.rows[body['Container']]['NetworkSettings']['Networks']['owned'] = body['EndpointConfig']
        elif path.startswith('/containers/create'):
            self.creates += 1
            if self.creates == self.fail_at:
                raise ValueError('Injected create failure')
            cid = str(self.creates + 2) * 64
            self.rows[cid] = {'Id': cid, 'Name': '/' + path.split('name=')[1], 'Config': body,
                'State': {'Status': 'created'}, 'HostConfig': body['HostConfig'], 'NetworkSettings': {'Networks': body['NetworkingConfig']['EndpointsConfig']}}
            if self.interrupt:
                raise KeyboardInterrupt('Worker killed before create ID was journalled')
            return {'Id': cid}

    def replace(self):
        candidate = copy.deepcopy(self.config); candidate['services']['web']['read_only'] = True
        swap.replace({'id': ID}, 'web', candidate, {'web': {'read_only': True}}, {})

    def test_success_retains_mounts_and_changes_every_replica(self):
        self.replace()
        self.assertEqual(len(self.rows), 2)
        self.assertTrue(all(r['HostConfig']['ReadonlyRootfs'] for r in self.rows.values()))
        self.assertEqual({r['HostConfig']['Mounts'][0]['Source'] for r in self.rows.values()}, {'anonymous-1', 'anonymous-2'})
        self.assertTrue(all(r['HostConfig']['SecurityOpt'] == ['seccomp=original'] for r in self.rows.values()))
        self.assertFalse((self.directory / swap.JOURNAL).exists())
        self.assertLess(max(i for i,c in enumerate(self.calls) if c[1]=='commit'), min(i for i,c in enumerate(self.calls) if c[1]=='rename'))

    def test_zero_replica_service_can_save_configuration_without_creating_instances(self):
        self.rows.clear()
        candidate = {'services': {'web': {'deploy': {'replicas': 0}, 'read_only': True}}}
        swap.replace({'id': ID}, 'web', candidate, {'web': {'read_only': True}}, {})
        self.assertEqual(self.creates, 0)
        self.assertEqual(json.loads((self.directory / 'compose.runtime.json').read_text()), candidate)
        self.assertFalse((self.directory / swap.JOURNAL).exists())

    def test_disk_commit_keeps_journal_authenticated(self):
        from dctlib import secure_io
        key = self.root / 'recovery.key'
        secure_io.atomic_text(key, 'test-only-key')
        path = self.directory / 'disk-transaction.json'
        before = {'phase': 'preparing', 'key': 'mp1'}
        after = dict(before, phase='committed')
        with patch.object(secure_io, 'KEY', key):
            worker.write_json(path, before)
            swap.execute({'id': ID}, '*', [], self.config, {}, {}, {},
                         updates={'disk-transaction.json': json.dumps(after)})
            self.assertEqual(secure_io.read_json(path), after)
            changed = json.loads(path.read_text())
            changed['key'] = 'mp2'
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'integrity'):
                secure_io.read_json(path)

    def test_failed_control_commit_restores_original_signed_disk_journal(self):
        from dctlib import secure_io
        key = self.root / 'recovery.key'
        secure_io.atomic_text(key, 'test-only-key')
        path = self.directory / 'disk-transaction.json'
        before = {'phase': 'preparing', 'key': 'mp1'}
        write = swap.durable_write
        def interrupted(target, value):
            if target.name == 'pending.json':
                raise OSError('Interrupted control commit')
            return write(target, value)
        with patch.object(secure_io, 'KEY', key):
            worker.write_json(path, before)
            original = path.read_bytes()
            with patch.object(swap, 'durable_write', side_effect=interrupted):
                with self.assertRaisesRegex(OSError, 'Interrupted control commit'):
                    swap.execute({'id': ID}, '*', [], self.config, {}, {}, {},
                                 updates={'disk-transaction.json': json.dumps(dict(before, phase='committed'))})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(secure_io.read_json(path), before)
            self.assertFalse((self.directory / swap.JOURNAL).exists())

    def test_second_replica_failure_restores_originals_and_pending_changes(self):
        self.fail_at = 2
        with self.assertRaisesRegex(ValueError, 'Injected'):
            self.replace()
        self.assert_originals()
        self.assertEqual(worker.load_pending(self.directory), {'web': {'read_only': True}})

    def test_killed_worker_recovers_unjournalled_replacement_by_name(self):
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.replace()
        self.assertTrue((self.directory / swap.JOURNAL).exists())
        swap.recover(ID)
        self.assert_originals()

    def test_missing_original_does_not_delete_replacement(self):
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt): self.replace()
        del self.rows['1'*64]
        with self.assertRaisesRegex(ValueError, 'Original.*missing'): swap.recover(ID)
        self.assertIn('3'*64, self.rows)
        self.assertTrue((self.directory / swap.JOURNAL).exists())

    def test_foreign_name_is_not_removed(self):
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt): self.replace()
        self.rows['3'*64]['Config']['Labels']['betterdocker.id'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'ownership'): swap.recover(ID)
        self.assertIn('3'*64, self.rows)

    def test_committed_cleanup_can_be_retried(self):
        with patch.object(swap, 'finish', side_effect=ValueError('Cleanup interrupted')):
            with self.assertRaisesRegex(ValueError, 'Cleanup'): self.replace()
        self.assertEqual(json.loads((self.directory / swap.JOURNAL).read_text())['phase'], 'committed')
        swap.recover(ID)
        self.assertEqual(set(self.rows), {'3'*64, '4'*64})
        self.assertTrue(json.loads((self.directory / 'compose.runtime.json').read_text())['services']['web']['read_only'])

    def test_clearing_user_uses_portable_image_default(self):
        row = source(1); row['Config']['User'] = '65534'
        body = swap.settings_body(row, {'user': '65534'}, {}, {IMAGE: '1000'})
        self.assertEqual(body['User'], '1000')

    def test_compose_deploy_resource_limits_are_restored(self):
        service = {'deploy': {'resources': {'limits': {'memory': 67108864, 'cpus': '0.5', 'pids': 100},
            'reservations': {'memory': 33554432}}}}
        host = swap.settings_body(source(1), {}, service, {})['HostConfig']
        self.assertEqual((host['Memory'], host['MemoryReservation'], host['NanoCpus'], host['PidsLimit']),
            (67108864, 33554432, 500000000, 100))

    def test_anonymous_volume_ownership_rejects_foreign_labels_and_drivers(self):
        from dctlib.policy import owned_local_volume
        volume = {'Driver': 'local', 'Labels': {'com.docker.volume.anonymous': ''}}
        self.assertTrue(owned_local_volume(volume, 'a'*64, ID))
        self.assertFalse(owned_local_volume(volume, 'named-foreign', ID))
        self.assertFalse(owned_local_volume(dict(volume, Driver='nfs'), 'a'*64, ID))
        self.assertFalse(owned_local_volume(dict(volume, Labels={'foreign': ''}), 'a'*64, ID))

    def test_cleanup_retains_images_used_by_any_container(self):
        repo = 'betterdocker-settings/' + ID
        rows = [{'Repository': repo, 'Tag': 'a'*32+'-0', 'ID': IMAGE},
                {'Repository': repo, 'Tag': 'b'*32+'-0', 'ID': 'sha256:'+'f'*64}]
        def run(command, **kwargs):
            return Mock(stdout='\n'.join(json.dumps(row) for row in rows) if command[2]=='ls' else '', returncode=0)
        with patch.object(worker, 'run', side_effect=run) as commands, patch.object(worker, 'capture', side_effect=['container', IMAGE]):
            swap.prune_images(ID)
        removals = [c.args[0] for c in commands.call_args_list if c.args[0][2]=='rm']
        self.assertEqual(removals, [['docker','image','rm','--force',repo+':'+'b'*32+'-0']])

    def test_empty_default_user_does_not_inherit_committed_override(self):
        row = source(1); row['Config']['User'] = '65534'
        body = swap.settings_body(row, {'user': '65534'}, {}, {IMAGE: ''})
        self.assertEqual(body['User'], '0')

    def test_unavailable_daemon_is_not_treated_as_a_missing_container(self):
        with patch.object(worker, 'run', return_value=Mock(returncode=1, stderr='Cannot connect: no such file or directory')):
            with self.assertRaisesRegex(ValueError, 'Cannot inspect'):
                swap.inspect('web')

    def test_cleanup_interruption_after_deleting_one_original_keeps_new_replicas(self):
        def interrupted(command, **kwargs):
            if command == ['docker', 'rm', '2'*64]:
                raise ValueError('Cleanup interrupted after the first original')
            return self.docker_run(command, **kwargs)
        with patch.object(worker, 'run', side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, 'Cleanup interrupted'):
                self.replace()
        self.assertEqual(set(self.rows), {'2'*64, '3'*64, '4'*64})
        swap.recover(ID)
        self.assertEqual(set(self.rows), {'3'*64, '4'*64})
        self.assertFalse((self.directory / swap.JOURNAL).exists())

    def test_legacy_explicit_user_resolves_verified_platform_image_default(self):
        row = source(1); row['Config']['User'] = '65534'
        def run(command, **kwargs):
            if command[-1] == IMAGE:
                return Mock(returncode=1, stdout='', stderr='No such image')
            image = {'Id': IMAGE, 'Config': {'User': '1000'}}
            if '--platform' not in command:
                image.update(Id='sha256:'+'f'*64, Os='linux', Architecture='amd64',
                    Descriptor={'mediaType':'application/vnd.oci.image.index.v1+json'})
            return Mock(returncode=0, stdout=json.dumps([image]))
        with patch.object(worker, 'run', side_effect=run):
            self.assertEqual(swap.default_user(row, {'user':'65534','image':'busybox:1.37'}, {}, {}), '1000')

    def assert_originals(self):
        self.assertEqual(set(self.rows), {'1'*64, '2'*64})
        self.assertEqual({r['Name'] for r in self.rows.values()}, {'/web-1', '/web-2'})
        self.assertTrue(all('owned' in r['NetworkSettings']['Networks'] for r in self.rows.values()))
        self.assertTrue(all(r['HostConfig']['RestartPolicy']['Name']=='unless-stopped' for r in self.rows.values()))
        self.assertEqual(json.loads((self.directory / 'compose.runtime.json').read_text()), self.config)
        self.assertFalse((self.directory / swap.JOURNAL).exists())
