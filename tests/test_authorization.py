"""API scope, queued ownership and delegated input regression checks."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_worker import worker as w, ROOT, BASE
from dctlib import authorization, data, security_errors

ID = 'a' * 32
OTHER = 'b' * 32


class ApiScopes(unittest.TestCase):
    def source(self):
        return (ROOT / 'backend/PVE/API2/BetterDocker.pm').read_text()

    def test_every_registered_route_has_an_explicit_scope(self):
        source = self.source()
        table = source.split('my %method_scopes = (', 1)[1].split('\n);', 1)[0]
        covered = set(re.findall(r"(\w+) => '[^']+'", table))
        names = set()
        for path in (ROOT / 'backend/PVE/API2/BetterDocker').glob('*.pm'):
            names.update(re.findall(r"name => '(\w+)', path =>", path.read_text()))
        self.assertEqual(names, covered)

    def test_actual_scope_function_denies_unknown_routes_and_separates_actions(self):
        source = self.source()
        policy = source[source.index('my %method_scopes = ('):source.index('sub request_context {')]
        script = 'use JSON::PP; ' + policy + r'''
my %result;
for my $name (qw(detail logs management termproxy vncwebsocket image_apply migrate set_disk recover)) {
 $result{$name}=method_scope($name, {});
}
$result{delete}=method_scope('action',{action=>'delete'});
$result{rollback}=method_scope('snapshot',{action=>'restore'});
$result{diagnostics}=method_scope('list',{diagnostics=>1});
eval { method_scope('accidental_new_route',{}) }; $result{unknown}=$@ ? 1 : 0;
print encode_json(\%result);
'''
        output = json.loads(subprocess.check_output(['perl', '-e', script], text=True))
        self.assertEqual(output, dict(detail='configure', logs='console', management='audit', termproxy='console',
            vncwebsocket='console', image_apply='images', migrate='migrate', set_disk='disk', recover='configure',
            delete='remove', rollback='rollback', diagnostics='trusted', unknown=1))


class Ownership(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for attr, value in [('ROOT', base / 'projects'), ('METADATA', base / 'metadata'), ('LOCK', base / 'node.lock')]:
            context = patch.object(w, attr, value); context.start(); self.addCleanup(context.stop)
        (w.ROOT / ID).mkdir(parents=True)
        (w.METADATA / 'ids').mkdir(parents=True)
        proc = base / 'proc' / str(os.getpid()); proc.mkdir(parents=True)
        (proc / 'stat').write_text('1 (python) ' + ' '.join(['0'] * 20))
        context = patch.object(w, 'PROC', base / 'proc'); context.start(); self.addCleanup(context.stop)
        self.info = dict(id=ID, vmid=101, node='pve', runtime_mode='rootful')
        self.context = dict(self.info)
        (w.METADATA / (ID + '.json')).write_text(json.dumps(self.info))
        self.owner = w.METADATA / 'ids/101.json'
        self.owner.write_text(json.dumps(self.info))
        for context in (patch.object(w, 'current_node', return_value='pve'),
                        patch.object(w.node_runtime, 'load', return_value=SimpleNamespace(mode='rootful'))):
            context.start(); self.addCleanup(context.stop)

    def check(self, info=None, context=None):
        with patch.dict(os.environ, {'BETTERDOCKER_REQUEST_CONTEXT': json.dumps(context or self.context)}):
            authorization.check_context(ID, info if info is not None else self.info)

    def test_authorized_identity_is_accepted(self):
        self.check()

    def test_changed_owner_node_guest_id_or_runtime_is_rejected(self):
        for change in ({'node': 'another'}, {'vmid': 102}, {'runtime_mode': 'rootless'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check(dict(self.info, **change))
        self.owner.write_text(json.dumps(dict(self.info, id=OTHER)))
        with self.assertRaisesRegex(ValueError, 'guest ID ownership'):
            self.check()

    def test_foreign_identifier_requires_explicit_clone_destination(self):
        with self.assertRaisesRegex(ValueError, 'another DCT'):
            self.check(context=dict(self.context, id=OTHER))
        self.check(info={}, context=dict(self.context, id=OTHER, destination_id=ID, destination_vmid=101))

    def test_deleted_guest_runtime_cannot_silently_switch_daemon(self):
        authorization.remember_owner(ID)
        with patch.object(w.node_runtime, 'load', return_value=SimpleNamespace(mode='rootless')):
            with self.assertRaisesRegex(ValueError, 'another runtime'):
                authorization.check_context(ID, {})

    def test_ownership_is_checked_before_recovery(self):
        self.owner.write_text(json.dumps(dict(self.info, node='moved')))
        with patch.dict(os.environ, {'BETTERDOCKER_REQUEST_CONTEXT': json.dumps(self.context)}), \
             patch('dctlib.service_recreate.recover') as recover:
            with self.assertRaisesRegex(ValueError, 'ownership'):
                with w.operation(ID, 'update'):
                    self.fail('A stale request entered the mutation')
            recover.assert_not_called()

    def test_recording_runtime_does_not_create_a_restore_destination(self):
        with self.assertRaisesRegex(ValueError, "project directory"):
            authorization.remember_owner(OTHER)
        self.assertFalse((w.ROOT / OTHER).exists())

    def test_legacy_disk_cleanup_rechecks_runtime_before_free(self):
        (w.METADATA / (ID + '.json')).unlink()
        authorization.remember_owner(ID)
        with patch.object(w.node_runtime, 'load', return_value=SimpleNamespace(mode='rootless')), patch.object(w, 'pve_storage') as free:
            with self.assertRaisesRegex(ValueError, 'another runtime'):
                w.purge_retained_storage(ID)
            free.assert_not_called()

    def test_foreign_retained_anonymous_volume_is_rejected(self):
        name = 'c' * 64
        other = w.ROOT / OTHER; other.mkdir()
        (other / 'owned-volumes.json').write_text(json.dumps({'volumes': [name]}))
        with self.assertRaisesRegex(ValueError, 'another DCT'):
            data.check_volume_claim(name, ID)

    def test_retained_cleanup_validates_all_resources_before_deleting_snapshots(self):
        (w.METADATA / (ID + '.json')).unlink()
        snapshot = w.snapshot_root(ID) / 'kept'; snapshot.mkdir(parents=True)
        (snapshot / 'manifest.json').write_text('{}')
        manifest = {'id': ID, 'name': 'kept'}
        with patch.object(w, 'container_records', return_value=[]), patch.object(w, 'snapshot_check', return_value=(snapshot, manifest)), \
             patch.object(data, 'check_data_dependencies'), patch.object(data, 'owned_data_plan', side_effect=ValueError('foreign volume')), \
             patch.object(data.shutil, 'rmtree') as remove:
            with self.assertRaisesRegex(ValueError, 'foreign volume'):
                data.retained_data(ID, destroy=True)
            remove.assert_not_called()


class SourcePolicy(unittest.TestCase):
    def test_dangerous_source_is_rejected_before_compose_runs(self):
        options = ['privileged: true', 'pid: host', 'network_mode: host', 'devices: [/dev/sda]',
            'cap_add: [SYS_ADMIN]', 'use_api_socket: true', 'post_start: [{command: id}]',
            'volumes: ["/etc/shadow:/data"]', 'volumes: [{type: bind, source: /var/run/docker.sock, target: /sock}]',
            'env_file: [{path: /etc/shadow, required: false}]', 'volumes: ["../../etc/shadow:/data"]',
            'extends: {file: /etc/private.yaml, service: app}', '<<: {privileged: true}']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for index, option in enumerate(options):
                directory = root / ID
                source = 'services:\n  app:\n    image: busybox\n    ' + option + '\n'
                with self.subTest(option=option), patch.object(w, 'ROOT', root), patch.object(w, 'trusted_management', return_value=False), \
                     patch.object(w, 'capture') as compose:
                    with self.assertRaises(ValueError):
                        w.prepare(dict(BASE, compose=source), ID, directory)
                    compose.assert_not_called()

    def test_errors_do_not_echo_command_arguments_or_environment_credentials(self):
        before = set(security_errors._values)
        self.addCleanup(lambda: setattr(security_errors, '_values', before))
        security_errors.remember_environment('DATABASE_PASSWORD=private-sentinel\nNORMAL=another-value')
        self.assertNotIn('private-sentinel', security_errors.public_error(ValueError('failed private-sentinel')))
        self.assertNotIn('private-sentinel', security_errors.public_error(subprocess.CalledProcessError(1, ['tool', 'private-sentinel'])))
        self.assertNotIn('credential', security_errors.redact('https://user:credential@example.test'))

    def test_docker_task_stderr_redacts_imported_credentials(self):
        before = set(security_errors._values)
        self.addCleanup(lambda: setattr(security_errors, '_values', before))
        security_errors.remember_environment('PASSWORD=stage1-private-sentinel')
        result = subprocess.CompletedProcess(['docker'], 0, stderr='warning: stage1-private-sentinel\n')
        output = io.StringIO()
        with patch.object(w.node_runtime, 'run', return_value=result) as docker, contextlib.redirect_stderr(output):
            w.run(['docker', 'compose', 'up'])
        self.assertEqual(docker.call_args.kwargs['stderr'], subprocess.PIPE)
        self.assertIn('[redacted]', output.getvalue())
        self.assertNotIn('stage1-private-sentinel', output.getvalue())

    def test_root_only_helpers_report_denial_without_tracebacks(self):
        for name in ('provision.py', 'guest_state.py', 'transfer.py'):
            with self.subTest(helper=name):
                script = ('import os,runpy,sys; os.geteuid=lambda:1000; '
                          'sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; '
                          'runpy.run_path(sys.argv[0],run_name="__main__")')
                result = subprocess.run([sys.executable, '-c', script, str(ROOT / 'scripts'),
                                         str(ROOT / 'scripts' / name), 'invalid-operation'],
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('host root privileges', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
