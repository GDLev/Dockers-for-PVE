"""Existing-engine dependency checks never invoke package or service changes."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ComposeCompatibility(unittest.TestCase):
    def check_version(self, version, mode, engine="29.9.1", containerd="2.4.2"):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            docker = directory / 'docker'
            docker.write_text('#!/bin/sh\nif [ "$1 $2 $3" = "compose version --short" ]; then printf "%s\\n" "$BD_TEST_VERSION"; fi\n')
            docker.chmod(0o755)
            for name, output in (('dockerd', 'Docker version ' + engine + ', build fixture'), ('containerd', 'containerd containerd.io v' + containerd + ' fixture')):
                helper = directory / name
                helper.write_text("#!/bin/sh\nprintf '%s\\n' '" + output + "'\n")
                helper.chmod(0o755)
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

    def test_unsupported_runtime_is_refused_without_host_changes(self):
        for mode in ('rootful', 'rootless'):
            for engine, containerd in [('29.9.0', '2.4.2'), ('29.9.1', '2.4.1'), ('30.0.0', '2.4.2'), ('29.9.1', '2.5.0')]:
                with self.subTest(mode=mode, engine=engine, containerd=containerd):
                    result = self.check_version('5.6.0', mode, engine, containerd)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('maintenance window', result.stderr)

    def test_package_configuration_never_invokes_apt_for_unavailable_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for name in ('docker', 'dockerd-rootless.sh'):
                helper = directory / name
                helper.write_text('#!/bin/sh\nexit 1\n')
                helper.chmod(0o755)
            helper = directory / 'apt-get'
            helper.write_text('#!/bin/sh\necho UNEXPECTED_HOST_CHANGE >&2\nexit 99\n')
            helper.chmod(0o755)
            for mode in ('rootful', 'rootless'):
                with self.subTest(mode=mode):
                    result = subprocess.run(['bash', str(ROOT / 'scripts/docker-install.sh'), mode],
                        env=dict(os.environ, PATH=folder + os.pathsep + os.environ['PATH'], BETTERDOCKER_PACKAGE_CONFIGURE='1'),
                        text=True, capture_output=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('inside dpkg', result.stderr)
                    self.assertNotIn('UNEXPECTED_HOST_CHANGE', result.stderr)


class RootlessPackages(unittest.TestCase):
    def install(self, *, existing=False, daemon_only=False, preexisting_mask=False, fail=''):
        source = (ROOT / 'scripts/docker-install.sh').read_text()
        function = source.split('install_rootless_packages() (', 1)[1].split('\n)\n', 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / 'state.json'
            state.write_text(json.dumps(dict(masked=['docker.service'] if preexisting_mask else [], enabled=True, calls=[])))
            program = '''import json,os,sys
from pathlib import Path
p=Path(os.environ['BD_TEST_STATE']);s=json.loads(p.read_text());a=sys.argv[1:];name=Path(sys.argv[0]).name;result=0
s['calls'].append([name,*a]);failure=os.environ['BD_TEST_FAIL']
if name=='apt-get':
 if os.environ['BD_TEST_EXISTING']!='1':
  assert set(s['masked'])=={'docker.service','docker.socket'}, 'Package installation can start rootful Docker'
 s['enabled']=True
 if failure=='apt':result=7
elif a[0]=='is-active':result=3
elif a[0]=='is-enabled':
 print('masked' if a[1] in s['masked'] else 'not-found');result=1
elif a[0]=='mask':
 if failure=='mask' and a[1]=='docker.socket':result=1
 else:s['masked'].append(a[1])
elif a[0]=='disable':
 if failure=='disable':result=1
 else:s['enabled']=False
elif a[0]=='unmask':s['masked']=[u for u in s['masked'] if u not in a[1:]]
else:raise AssertionError(a)
p.write_text(json.dumps(s));sys.exit(result)
'''
            for name in ('apt-get', 'systemctl'):
                helper = root / name
                helper.write_text('#!' + sys.executable + '\n' + program)
                helper.chmod(0o755)
            if existing or daemon_only:
                helper = root / ('docker' if existing else 'dockerd')
                helper.write_text('#!/bin/sh\nexit 0\n'); helper.chmod(0o755)
            result = subprocess.run(['/bin/bash', '-e', '-c', 'install_rootless_packages() (' + function + '\n)\ninstall_rootless_packages'],
                env=dict(os.environ, PATH=folder, BD_TEST_STATE=str(state), BD_TEST_FAIL=fail,
                         BD_TEST_EXISTING='1' if existing else '0'), capture_output=True, text=True)
            return result, json.loads(state.read_text())

    def test_fresh_packages_are_masked_before_apt_then_disabled(self):
        result, state = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state['enabled'])
        self.assertEqual(state['masked'], [])
        masks = [call for call in state['calls'] if call[:2] == ['systemctl', 'mask']]
        self.assertEqual(masks, [['systemctl', 'mask', 'docker.service'], ['systemctl', 'mask', 'docker.socket']])

    def test_existing_daemon_and_units_are_not_stopped_or_disabled(self):
        result, state = self.install(existing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(state['enabled'])
        self.assertEqual(len(state['calls']), 1)
        self.assertEqual(state['calls'][0][0], 'apt-get')

    def test_partial_binary_installation_is_refused_without_changes(self):
        result, state = self.install(daemon_only=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Existing Docker daemon has no client', result.stderr)
        self.assertEqual(state['calls'], [])

    def test_preexisting_masks_remain(self):
        result, state = self.install(preexisting_mask=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['masked'], ['docker.service'])
        self.assertFalse(state['enabled'])

    def test_failed_apt_disables_partial_installation_and_keeps_its_error(self):
        result, state = self.install(fail='apt')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse(state['enabled'])
        self.assertEqual(state['masked'], [])

    def test_failed_disable_preserves_masks_and_refuses_success(self):
        result, state = self.install(fail='disable')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(state['masked']), {'docker.service', 'docker.socket'})
        self.assertFalse(any(call[:2] == ['systemctl', 'unmask'] for call in state['calls']))

    def test_failed_mask_rolls_back_only_own_masks_without_installing(self):
        result, state = self.install(fail='mask')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['masked'], [])
        self.assertTrue(state['enabled'])
        self.assertFalse(any(call[0] == 'apt-get' or call[:2] == ['systemctl', 'disable'] for call in state['calls']))
