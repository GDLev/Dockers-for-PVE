import live_environment as env
#!/usr/bin/env python3
f"Installer acceptance on the named disposable PVE cluster, using real PTYs.\n\nRun on {env.node('primary')} with --source=<deployed checkout>. Never contacts PBS.\nLogs preserve the terminal transcript. No guest configuration is changed.\n"
import argparse
import atexit
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import time
import termios
import uuid

from live_firewall import remote
from live_rootless_package import containers

NODES = (env.node('primary'), env.node('secondary'), env.node('rootless'))
PENDING = '/var/lib/betterdocker/installer/pending-restart.json'


class Session:
    def __init__(self, command, log, term='xterm-256color'):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ.update(TERM=term, NO_COLOR='1', LANG='C.UTF-8')
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 100, 0, 0))
            os.execvp(command[0], command)
        self.output = b''
        self.log = Path(log)
        self.log.touch(mode=0o600)
        self.status = None
        atexit.register(self.close)

    def close(self):
        if self.status is None:
            try:
                self.send(b'\x03')
                os.kill(self.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    def read(self, timeout=.2):
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                block = os.read(self.fd, 65536)
            except OSError:
                block = b''
            self.output += block
            with self.log.open('ab') as handle:
                handle.write(block)
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)

    def wait(self, text, timeout=240, start=0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            if text.encode() in self.output[start:]:
                return len(self.output)
            if self.status is not None:
                raise AssertionError((text, self.status, self.output[-4000:].decode(errors='replace')))
        raise AssertionError(('Terminal timeout', text, self.output[-4000:].decode(errors='replace')))

    def send(self, keys):
        os.write(self.fd, keys)

    def finish(self, expected=0, timeout=240):
        deadline = time.monotonic() + timeout
        while self.status is None and time.monotonic() < deadline:
            self.read()
        assert self.status == expected, (self.status, self.output[-4000:].decode(errors='replace'))
        self.read(0)
        os.close(self.fd)


def panels():
    return {node: remote(node, ['systemctl', 'show', 'pvedaemon', 'pveproxy', '-p', 'MainPID', '-p', 'ActiveEnterTimestampMonotonic'], check=True).stdout for node in NODES}


def workloads():
    return {node: dict(runtime=remote(node, ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout,
                       containers=containers(node)) for node in NODES}


def lifecycle(source, logs, state_path):
    import live_rootless_installation as maintenance
    from live_pools_hooks import api
    maintenance.DROPIN = '/etc/systemd/system/{unit}.service.d/betterdocker-installer-test.conf'
    maintenance.CONDITION = '/run/betterdocker-installer-test-allow'
    if state_path.exists():
        raise ValueError('Lifecycle manifest exists; restore it first or choose a new --lifecycle-state path')
    state = dict(ha=api('get', '/cluster/ha/resources'), replication=api('get', '/cluster/replication'),
                 fixture='bd-installer-' + uuid.uuid4().hex[:10])
    state_path.write_text(json.dumps(state, indent=2)); state_path.chmod(0o600)
    name = state['fixture']
    try:
        maintenance.hold(state)
        remote(env.node('rootless'), ['docker', 'run', '--pull=never', '-d', '--name', name,
                           '--label', 'betterdocker.installer-test=' + name, '-v', name + ':/data',
                           'busybox:stable', 'sh', '-c', 'echo installer-retained >/data/marker; sleep 7200'], check=True)
        original = workloads()
        baseline = panels()
        ui = Session(['bash', str(source / 'start.sh'), 'uninstall', '--interactive', f"--nodes={env.node('rootless')}"], logs / 'uninstall.tty')
        ui.wait('Press Enter to start'); ui.send(b'\r')
        ui.wait('Press Enter to reload the PVE panel')
        assert panels() == baseline
        assert workloads() == original
        assert remote(env.node('rootless'), ['test', '-f', '/usr/share/perl5/PVE/API2/BetterDocker.pm']).returncode == 1
        ui.send(b'\r'); ui.finish()
        assert workloads() == original
        assert 'installer-retained' in remote(env.node('rootless'), ['docker', 'exec', name, 'cat', '/data/marker'], check=True).stdout
        print('PASS real uninstall removes PVE integration and retains rootless workload, volume and mode', flush=True)

        baseline = panels()
        ui = Session(['bash', str(source / 'start.sh'), 'install', '--interactive', f"--nodes={env.node('rootless')}"], logs / 'reinstall.tty')
        ui.wait('Press Enter to start'); ui.send(b'\r')
        ui.wait('Press Enter to reload the PVE panel', timeout=600)
        assert panels() == baseline
        assert workloads() == original
        ui.send(b'\r'); ui.finish()
        assert workloads() == original
        assert remote(env.node('rootless'), ['test', '-f', '/usr/share/perl5/PVE/API2/BetterDocker.pm']).returncode == 0
        assert 'installer-retained' in remote(env.node('rootless'), ['docker', 'exec', name, 'cat', '/data/marker'], check=True).stdout
        print('PASS real Install / Update reinstalls rootless integration without recreating workload', flush=True)
    finally:
        # Restore integration before releasing cluster HA maintenance, even if
        # the terminal check failed halfway through removal or installation.
        remote(env.node('rootless'), ['bash', str(source / 'start.sh'), 'repair'], check=True, timeout=300)
        owned = remote(env.node('rootless'), ['docker', 'inspect', name])
        if owned.returncode == 0:
            assert json.loads(owned.stdout)[0]['Config']['Labels']['betterdocker.installer-test'] == name
            remote(env.node('rootless'), ['docker', 'rm', '-f', name], check=True)
            remote(env.node('rootless'), ['docker', 'volume', 'rm', name], check=True)
        maintenance.restore(state)
        state['restored'] = True
        state_path.write_text(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--logs', default='/root/betterdocker-installer-acceptance')
    parser.add_argument('--lifecycle', action='store_true', help='Also exercise real uninstall/reinstall while holding native HA managers.')
    parser.add_argument('--lifecycle-state', default='/root/betterdocker-installer-lifecycle.json')
    parser.add_argument('--restore', action='store_true', help='Release HA maintenance from the saved lifecycle manifest after interruption.')
    args = parser.parse_args()
    assert subprocess.check_output(['hostname', '-s'], text=True).strip() == env.node('primary')
    source = Path(args.source)
    logs = Path(args.logs)
    logs.mkdir(mode=0o700, exist_ok=True)
    if args.restore:
        import live_rootless_installation as maintenance
        maintenance.DROPIN = '/etc/systemd/system/{unit}.service.d/betterdocker-installer-test.conf'
        maintenance.CONDITION = '/run/betterdocker-installer-test-allow'
        remote(env.node('rootless'), ['bash', str(source / 'start.sh'), 'repair'], check=True, timeout=300)
        maintenance.restore(json.loads(Path(args.lifecycle_state).read_text()))
        return
    before = workloads()
    resources = json.loads(subprocess.check_output(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'], text=True))
    for node in NODES:
        assert remote(node, ['test', '-f', PENDING]).returncode == 1, node

    # Cancel at the initial menu. No staging or host mutation is allowed.
    baseline = panels()
    ui = Session(['bash', str(source / 'start.sh')], logs / 'cancel-menu.tty')
    ui.wait('Esc cancel')
    ui.send(b'\x1b')
    ui.finish(130)
    assert panels() == baseline
    print('PASS menu cancellation leaves PVE services unchanged', flush=True)

    # Select Repair and all three nodes with actual arrows and Space.
    ui = Session(['bash', str(source / 'start.sh')], logs / 'repair-cluster.tty')
    ui.wait('Esc cancel')
    ui.send(b'\x1b[B\r')
    ui.wait('Space select')
    ui.send(b'\x1b[B \x1b[B \r')
    ui.wait('Press Enter to start')
    ui.send(b'\r')
    ui.wait('Press Enter to reload the PVE panel')
    assert panels() == baseline, 'Panel/API restarted before final Enter'
    assert workloads() == before
    for node in NODES:
        assert remote(node, ['test', '-f', PENDING]).returncode == 0
    ui.send(b'\r')
    ui.finish()
    assert all(panels()[node] != baseline[node] for node in NODES)
    assert workloads() == before
    for node in NODES:
        assert remote(node, ['test', '-f', PENDING]).returncode == 1
    print('PASS native cluster selection, mixed fixed modes, unchanged workloads and reload only after final Enter', flush=True)

    # TERM=dumb, then cancel final reload and recover from cached source.
    baseline = panels()
    ui = Session(['bash', str(source / 'start.sh'), 'repair', '--interactive', f"--nodes={env.node('rootless')}"], logs / 'plain-terminal.tty', term='dumb')
    ui.wait('Press Enter to start')
    ui.send(b'\n')
    ui.wait('Press Enter to reload the PVE panel')
    assert panels() == baseline
    ui.send(b'q\n')
    ui.finish(130)
    assert remote(env.node('rootless'), ['test', '-f', PENDING]).returncode == 0
    assert b'\x1b[' not in ui.output
    result = subprocess.run(['bash', '/var/lib/betterdocker/installer/source/start.sh', 'finish', f"--nodes={env.node('rootless')}", '--yes'], capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr + result.stdout
    assert panels()[env.node('rootless')] != baseline[env.node('rootless')]
    assert workloads() == before
    print('PASS plain terminal, deferred cancellation and cached finish recovery', flush=True)

    # Cluster-wide uninstall blockers must prevent changes on every node.
    baseline = panels()
    result = subprocess.run(['bash', str(source / 'start.sh'), 'uninstall', '--nodes=' + ','.join(NODES), '--yes'], capture_output=True, text=True, timeout=120)
    assert result.returncode != 0 and 'Remove DCT HA resources' in result.stdout, result.stdout + result.stderr
    assert panels() == baseline and workloads() == before
    assert all(remote(node, ['test', '-f', PENDING]).returncode == 1 for node in NODES)
    print('PASS HA uninstall blockers reject all selected nodes before mutation', flush=True)

    current = json.loads(subprocess.check_output(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'], text=True))
    fields = ('vmid', 'node', 'status', 'name', 'pool', 'tags', 'template')
    normalized = lambda rows: sorted(tuple(str(row.get(k, '')) for k in fields) for row in rows)
    assert normalized(current) == normalized(resources)
    if args.lifecycle:
        lifecycle(source, logs, Path(args.lifecycle_state))
        assert workloads() == before
    print('PASS original guest inventory and placement retained; terminal transcripts: ' + str(logs), flush=True)


if __name__ == '__main__':
    main()
