"""Runtime selection, recovery and real terminal keystrokes without host changes."""
import os
import json
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import installer
from installer_ui import MESSAGES, Terminal


class TerminalInstallerTests(unittest.TestCase):
    def test_entrypoint_accepts_space_separated_and_equal_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / 'python3'
            python.write_text('#!' + sys.executable + '\nimport sys,json\nsys.path.insert(0,' + repr(str(ROOT / 'scripts')) + ')\nfrom installer import parser\nsys.argv=sys.argv[1:]\nprint(json.dumps(vars(parser().parse_args())))\n')
            python.chmod(0o755)
            for action, flags in (('repair', ['--nodes', 'one,two']), ('repair', ['--nodes=one,two']), ('migrate', ['--runtime', 'rootless', '--nodes', 'one,two'])):
                result = subprocess.run(['bash', str(ROOT / 'start.sh'), action, *flags],
                                        env=dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH']),
                                        capture_output=True, text=True, check=True)
                values = json.loads(result.stdout)
                self.assertEqual((values['action'], values['nodes']), (action, 'one,two'))

    def test_worker_progress_markers_use_english_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            cluster = unittest.mock.Mock(local='one')
            cluster.command.side_effect = lambda node, args: args
            ui = Terminal()
            runner = installer.Runner(cluster, ui)
            runner.directory = Path(folder)
            with patch.object(ui, 'output') as output:
                result = runner.process('one', [sys.executable, '-c', "print('[betterdocker-stage] integration')"], 'apply')
            self.assertEqual(result['code'], 0)
            output.assert_any_call('  one  Registering the PVE extension')

    def test_fixed_modes_and_fresh_selection_in_mixed_cluster(self):
        nodes = [dict(node='one', mode='rootful'), dict(node='two', mode='rootless'), dict(node='three', mode=None)]
        self.assertEqual(installer.modes_for(nodes, 'rootless', fresh_only=True),
                         dict(one='rootful', two='rootless', three='rootless'))
        with self.assertRaisesRegex(ValueError, 'fixed'):
            installer.modes_for(nodes, 'rootless')
        with self.assertRaisesRegex(ValueError, '--runtime'):
            installer.modes_for([nodes[-1]])

    def test_reload_failure_retains_marker_and_success_removes_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, 'CACHE', Path(directory)):
            installer.mark_restart('repair')
            marker = Path(directory) / 'pending-restart.json'
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            with patch.object(installer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'systemctl')):
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.finish_restart()
            self.assertTrue(marker.exists())
            with patch.object(installer.subprocess, 'run') as run, patch.object(installer.subprocess, 'check_output', return_value='one\n'):
                installer.finish_restart()
            self.assertEqual(run.call_args_list[0].args[0][:2], ['systemctl', 'restart'])
            self.assertFalse(marker.exists())

    def test_cached_source_can_finish_after_original_download_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'download'
            source.mkdir()
            (source / 'start.sh').write_text('first')
            with patch.object(installer, 'CACHE', Path(directory) / 'cache'):
                installer.cache_source(source)
                installer.cache_source(installer.CACHE / 'source')
                (source / 'start.sh').write_text('second')
                installer.cache_source(source)
                self.assertEqual((installer.CACHE / 'source/start.sh').read_text(), 'second')
                self.assertFalse((installer.CACHE / 'previous').exists())

    def test_messages_are_english_regardless_of_system_locale(self):
        with patch.dict(os.environ, LC_ALL='pl_PL.UTF-8', LANG='pl_PL.UTF-8'):
            ui = Terminal()
            self.assertEqual(ui.t('operation'), 'Choose an operation')
            for key in MESSAGES:
                self.assertTrue(ui.t(key, path='/log', node='one', nodes='one,two', command='finish'))

    def test_unreachable_node_is_not_silently_treated_as_reloaded(self):
        cluster = unittest.mock.Mock()
        cluster.run.return_value.returncode = 255
        runner = installer.Runner(cluster, Terminal())
        self.assertEqual(runner.pending(['two']), ['two'])
        self.assertEqual(runner.results[0]['code'], 1)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX terminal')
    def test_arrow_keys_space_and_enter_with_real_controlling_terminal(self):
        code = '''import sys;sys.path.insert(0,sys.argv[1]);from installer_ui import *
t=Terminal();assert t.open()
try:
 r=t.choose('Nodes',[Choice('one','One'),Choice('two','Two'),Choice('bad','Offline',enabled=False)],['one'],True)
 t.output('RESULT='+','.join(r))
 r=t.choose('Runtime',[Choice('rootful','rootful',t.t('rootful_description')),Choice('rootless','rootless',t.t('rootless_description'))],['rootless'])
 t.output('MODE='+r[0])
finally:t.close()
'''
        child, fd = pty.fork()
        if child == 0:
            os.environ['TERM'] = 'xterm-256color'
            os.environ['NO_COLOR'] = '1'
            os.execv(sys.executable, [sys.executable, '-c', code, str(ROOT / 'scripts')])
        output = b''
        try:
            deadline = time.monotonic() + 10
            sent = False
            switched = False
            accepted = False
            while time.monotonic() < deadline:
                if select.select([fd], [], [], .1)[0]:
                    try:
                        output += os.read(fd, 65536)
                    except OSError:
                        break
                if b'Esc cancel' in output and not sent:
                    os.write(fd, b'\x1b[B \x1b[B \r')
                    sent = True
                if b'Container AppArmor' in output and not switched:
                    os.write(fd, b'\x1b[A')
                    switched = True
                if b'Containers share a privileged' in output and switched and not accepted:
                    os.write(fd, b'\r')
                    accepted = True
                if b'MODE=rootful' in output:
                    break
            self.assertIn(b'RESULT=one,two', output)
            self.assertIn(b'MODE=rootful', output)
            self.assertIn(b'\r\n', output, 'raw forms must retain normal terminal line endings')
        finally:
            os.close(fd)
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid == 0:
                os.kill(child, 9)
                os.waitpid(child, 0)


if __name__ == '__main__':
    unittest.main()
