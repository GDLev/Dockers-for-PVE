#!/usr/bin/env python3
"""Interactive PVE installer and cluster coordinator. No third-party UI runtime."""
import argparse
from collections import deque
import contextlib
import hashlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from installer_ui import Cancelled, Choice, MESSAGES, Terminal

SOURCE = Path(__file__).resolve().parent.parent
CACHE = Path('/var/lib/betterdocker/installer')
FILES = ('assets', 'frontend', 'backend', 'scripts', 'systemd', 'start.sh', 'install.sh', 'README.md', 'API_DOCS.md', 'CONTRIBUTING.md', 'SECURITY.md', 'RELEASE_NOTES.md', 'LICENSE', '.release.json', '.release.sig')
SERVICES = ('pvedaemon', 'pveproxy', 'pvescheduler', 'pve-ha-crm', 'pve-ha-lrm')
PROBE = '''import json,os,pathlib,subprocess
p=pathlib.Path('/etc/betterdocker/runtime.json')
mode=None
if p.exists():
 s=p.lstat()
 if p.is_symlink() or s.st_uid!=0 or s.st_mode&0o022: raise ValueError('Runtime configuration must be root-owned and protected')
 mode=json.loads(p.read_text()).get('mode')
 if mode not in ('rootful','rootless'): raise ValueError('Invalid saved runtime')
installed=pathlib.Path('/usr/share/perl5/PVE/API2/BetterDocker.pm').is_file()
if mode is None and (installed or any(pathlib.Path('/var/lib/betterdocker/projects').glob('*/request.json'))): mode='rootful'
version=subprocess.check_output(['pveversion'],text=True).strip()
print(json.dumps(dict(mode=mode,installed=installed,version=version,pending=pathlib.Path('/var/lib/betterdocker/installer/pending-restart.json').exists())))
'''


def failure_tail(path):
    with path.open(errors='replace') as log:
        lines = list(deque(log, maxlen=20))
    messages = []
    for line in lines:
        if not line.strip() or line[:1].isspace() or line.startswith(('Traceback', '[betterdocker-stage]')):
            continue
        message = re.sub(r'^(?:[\w.]+Error|[\w.]+Exception):\s*', '', line.strip())
        messages.append(message)
    if messages:
        return messages[-6:]
    return [lines[-1].strip()] if lines else []


def archive(source=None):
    source = SOURCE if source is None else Path(source)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as package:
        for name in FILES:
            if (source / name).exists():
                package.add(source / name, arcname='betterdockers/' + name, filter=lambda item: None if '__pycache__' in Path(item.name).parts or item.name.endswith('.pyc') else item)
    return output.getvalue()


def cache_source(source):
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = CACHE / 'source'
    if source.resolve() == destination.resolve():
        return
    folder = Path(tempfile.mkdtemp(prefix='.source-', dir=CACHE))
    previous = CACHE / 'previous'
    try:
        for name in FILES:
            if (source / name).is_dir():
                shutil.copytree(source / name, folder / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            elif (source / name).exists():
                shutil.copy2(source / name, folder / name)
        if previous.exists():
            shutil.rmtree(previous)
        if destination.exists():
            destination.rename(previous)
        try:
            folder.rename(destination)
        except BaseException:
            if previous.exists() and not destination.exists():
                previous.rename(destination)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    finally:
        if folder.exists():
            shutil.rmtree(folder)


def mark_restart(action):
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CACHE / 'pending-restart.json'
    temporary = CACHE / ('.pending-' + uuid.uuid4().hex)
    try:
        with temporary.open('x') as handle:
            os.chmod(temporary, 0o600)
            json.dump(dict(action=action, time=datetime.now(timezone.utc).isoformat()), handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def finish_restart():
    path = CACHE / 'pending-restart.json'
    if not path.exists():
        print('No panel restart is pending.')
        return
    subprocess.run(['systemctl', 'restart', *SERVICES], check=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'pvedaemon', 'pveproxy', 'pvescheduler'], check=True)
    node = subprocess.check_output(['hostname', '-s'], text=True).strip()
    if Path('/usr/share/perl5/PVE/API2/BetterDocker.pm').exists():
        subprocess.run(['pvesh', 'usage', '/nodes/' + node + '/docker', '--verbose'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['pvesh', 'get', '/cluster/resources', '--output-format', 'json'], check=True, stdout=subprocess.DEVNULL)
    path.unlink()
    print('PVE panel and API services reloaded.')


class Cluster:
    def __init__(self):
        self.local = subprocess.check_output(['hostname', '-s'], text=True).strip()
        self.connections = {}

    def command(self, node, args):
        if node == self.local:
            return list(args)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', node):
            raise ValueError('Invalid PVE node name')
        if node not in self.connections:
            program = 'use PVE::Cluster; use PVE::SSHInfo; use JSON; PVE::Cluster::cfs_update(); print encode_json(PVE::SSHInfo::ssh_info_to_command(PVE::SSHInfo::get_ssh_info($ARGV[0])));'
            command = json.loads(subprocess.check_output(['perl', '-e', program, node], text=True))
            self.connections[node] = command[:-1] + ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', command[-1]]
        return self.connections[node] + [shlex.join(args)]

    def run(self, node, args, **kwargs):
        return subprocess.run(self.command(node, args), capture_output=True, timeout=kwargs.pop('timeout', 30), **kwargs)

    def inventory(self):
        rows = json.loads(subprocess.check_output(['pvesh', 'get', '/nodes', '--output-format', 'json'], text=True))
        result = []
        for row in sorted(rows, key=lambda i: (i['node'] != self.local, i['node'])):
            item = dict(node=row['node'], error='', mode=None, installed=False, pending=False, version='')
            if row.get('status') != 'online':
                item['error'] = 'offline'
            else:
                try:
                    probe = self.run(row['node'], ['python3', '-c', PROBE], text=True, check=True)
                    item.update(json.loads(probe.stdout))
                except subprocess.CalledProcessError as error:
                    details = (error.stderr or '').strip().splitlines()
                    item['error'] = details[-1] if details else 'SSH/runtime probe failed'
                except subprocess.TimeoutExpired:
                    item['error'] = 'SSH/runtime probe timed out'
                except (ValueError, OSError) as error:
                    item['error'] = str(error)
            result.append(item)
        return result


class Runner:
    def __init__(self, cluster, terminal):
        self.cluster, self.ui = cluster, terminal
        self.interrupted = False
        self.run_id = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.directory = CACHE / 'logs' / self.run_id
        self.prepared = {}
        self.results = []

    def prepare(self, nodes):
        if not getattr(self, 'development', False):
            import release
            metadata = release.source(SOURCE, getattr(self, 'allow_downgrade', False))
        bootstrap = (SOURCE / 'install.sh').read_bytes()
        verifier = (SOURCE / 'scripts/release.py').read_bytes()
        if not getattr(self, 'development', False):
            for name, content in (('install.sh', bootstrap), ('scripts/release.py', verifier)):
                if hashlib.sha256(content).hexdigest() != metadata['source'][name]:
                    raise ValueError('Installer source changed during preparation')
        reader = bootstrap.decode().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        authenticate = verifier.decode().split("\nif __name__ == '__main__':", 1)[0] + '''
key, pin = trust()
metadata = read_manifest(Path(sys.argv[1]), Path(sys.argv[2]), key, pin)
check_version(metadata, sys.argv[3] == '1', manifest_digest=digest(sys.argv[1]))
'''
        authenticate = 'import sys\n' + authenticate
        data = archive()
        checksum = hashlib.sha256(data).hexdigest()
        for node in nodes:
            if self.interrupted:
                raise Cancelled()
            result = self.cluster.run(node, ['mktemp', '-d', '/tmp/betterdocker-install.XXXXXXXX'], text=True, check=True)
            folder = result.stdout.strip()
            if not re.fullmatch(r'/tmp/betterdocker-install\.[A-Za-z0-9]{8}', folder):
                raise ValueError('Unexpected installation staging path')
            self.prepared[node] = folder
            if not getattr(self, 'development', False):
                for name in ('.release.json', '.release.sig'):
                    self.cluster.run(node, ['bash', '-c', 'cat > ' + shlex.quote(folder + '/transport' + name)], input=(SOURCE / name).read_bytes(), check=True)
                self.cluster.run(node, ['python3', '-c', authenticate, folder + '/transport.release.json', folder + '/transport.release.sig', '1' if getattr(self, 'allow_downgrade', False) else '0'], check=True)
            self.cluster.run(node, ['bash', '-c', 'cat > ' + shlex.quote(folder + '/source.tar.gz')], input=data, check=True)
            self.cluster.run(node, ['python3', '-c', reader, folder + '/source.tar.gz', checksum, folder], check=True)
            if not getattr(self, 'development', False):
                self.cluster.run(node, ['python3', folder + '/scripts/release.py', 'source', folder] + (['--allow-downgrade'] if getattr(self, 'allow_downgrade', False) else []), check=True)

    def worker(self, node, action, mode=None, operation=None):
        args = ['env']
        if getattr(self, 'development', False):
            args += ['BETTERDOCKER_DEVELOPMENT_SOURCE=1']
        if getattr(self, 'allow_downgrade', False):
            args += ['BETTERDOCKER_ALLOW_DOWNGRADE=1']
        args += ['bash', self.prepared[node] + '/start.sh', action]
        if mode:
            args += ['--runtime=' + mode]
        if operation:
            args += ['--operation=' + operation]
        return args

    def preflight(self, nodes, action, modes):
        errors = []
        for node in nodes:
            if self.interrupted:
                raise Cancelled()
            result = self.process(node, self.worker(node, 'preflight', modes[node], action), 'preflight')
            if result['code']:
                errors.append(node + ': preflight failed. ' + self.ui.t('log', path=result['log']))
        if errors:
            raise ValueError('\n'.join(errors))

    def cancel(self, *_):
        self.interrupted = True

    def process(self, node, args, phase):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (node + '-' + phase + '.log')
        started = time.monotonic()
        current = phase
        cancellation_shown = False
        command = self.cluster.command(node, args)
        environment = dict(os.environ, BETTERDOCKER_INSTALL_PROGRESS='1')
        # env must cross SSH too; only this fixed value is propagated.
        if node != self.cluster.local:
            command = self.cluster.command(node, ['env', 'BETTERDOCKER_INSTALL_PROGRESS=1', *args])
        with path.open('wb') as log:
            os.chmod(path, 0o600)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            buffer = b''
            try:
                while selector.get_map():
                    for key, _ in selector.select(.15):
                        block = os.read(key.fd, 8192)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        log.write(block)
                        buffer += block
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if line.startswith(b'[betterdocker-stage] '):
                                code = line.decode(errors='replace').split(' ', 1)[1].strip()
                                if 'stage_' + code in MESSAGES:
                                    current = code
                                    if not self.ui.cursor:
                                        self.ui.output('  ' + node + '  ' + self.ui.t('stage_' + current))
                        buffer = buffer[-32768:]
                    if self.interrupted and not cancellation_shown:
                        self.ui.end_progress()
                        self.ui.output(self.ui.t('finishing_node', node=node))
                        cancellation_shown = True
                    self.ui.progress(node, current, time.monotonic() - started)
                code = process.wait()
            finally:
                selector.close()
                process.stdout.close()
                self.ui.end_progress()
        if code:
            for line in failure_tail(path):
                self.ui.output('    ' + line)
        return dict(node=node, code=code, log=str(path), phase=phase)

    def execute(self, nodes, action, modes):
        for node in nodes:
            if self.interrupted:
                break
            result = self.process(node, self.worker(node, action, modes[node]) + ['--defer-restart'], 'apply')
            self.results.append(result)
            self.ui.output('  ' + node + '  ' + self.ui.t('failed' if result['code'] else 'done'))

    def pending(self, nodes):
        pending = []
        for node in nodes:
            try:
                result = self.cluster.run(node, ['test', '-f', str(CACHE / 'pending-restart.json')])
                if result.returncode not in (0, 1):
                    raise ValueError('SSH connection failed')
                if result.returncode == 0:
                    pending.append(node)
            except (subprocess.SubprocessError, OSError, ValueError):
                self.ui.output(self.ui.t('unknown_restart', node=node))
                self.results.append(dict(node=node, code=1, log='', phase='probe'))
                pending.append(node)
        return pending

    def finish(self, nodes):
        results = []
        for node in nodes:
            if self.interrupted:
                break
            result = self.process(node, self.worker(node, 'finish-restart'), 'restart')
            results.append(result)
            self.ui.output('  ' + node + '  ' + self.ui.t('failed' if result['code'] else 'reloaded'))
            if result['code']:
                self.ui.output('    ' + self.ui.t('log', path=result['log']))
        return results

    def cleanup(self):
        for node, folder in self.prepared.items():
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                self.cluster.run(node, ['rm', '-rf', '--', folder])


def supported(node, action):
    return not node['error'] and (action in ('uninstall', 'finish') or bool(re.match(r'pve-manager/9\.(1|2)\.', node['version'])))


def modes_for(nodes, requested=None, fresh_only=False):
    values = {}
    for node in nodes:
        if requested and node['mode'] and requested != node['mode'] and not fresh_only:
            raise ValueError(node['node'] + ': runtime mode is fixed; use migrate-runtime.py for conversion')
        mode = node['mode'] or requested
        if mode is None:
            raise ValueError(node['node'] + ': fresh unattended installation requires --runtime=rootful or --runtime=rootless')
        values[node['node']] = mode
    return values


def parser():
    result = argparse.ArgumentParser(description='Install, repair or remove BetterDockers on selected PVE nodes.')
    result.add_argument('action', nargs='?', choices=('install', 'repair', 'uninstall', 'finish', 'check'))
    result.add_argument('--development-source', action='store_true', help='Explicitly install an unsigned local checkout. Never use for downloaded releases.')
    result.add_argument('--allow-downgrade', action='store_true', help='Deliberately install an older signed release.')
    result.add_argument('--nodes', help='Comma-separated cluster node names. Default: local node.')
    result.add_argument('--runtime', choices=('rootful', 'rootless'))
    result.add_argument('--interactive', action='store_true', help='Open terminal forms even with an explicit action.')
    result.add_argument('--yes', action='store_true', help='Run without terminal forms or confirmation prompts.')
    result.add_argument('--no-restart', action='store_true', help='Leave panel/API restart pending for a later finish command.')
    return result


def cancel_prompt(*_):
    raise Cancelled()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--cache-source':
        if os.geteuid() != 0:
            raise ValueError('Run as root')
        cache_source(Path(sys.argv[2])); return 0
    if len(sys.argv) == 3 and sys.argv[1] == '--mark-restart':
        if os.geteuid() != 0:
            raise ValueError('Run as root')
        mark_restart(sys.argv[2]); return 0
    if len(sys.argv) == 2 and sys.argv[1] == '--finish-restart':
        if os.geteuid() != 0:
            raise ValueError('Run as root')
        finish_restart(); return 0
    args = parser().parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run as root on a Proxmox VE node')
    if args.development_source:
        if (SOURCE / '.release.json').exists():
            raise ValueError('Signed sources cannot bypass verification')
        print('WARNING: explicitly using unsigned development source.', file=sys.stderr)
    else:
        import release
        release.source(SOURCE, args.allow_downgrade)
    interactive = not args.yes and (args.action is None or args.interactive)
    ui = Terminal()
    runner = None
    handlers = {}
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            handlers[name] = signal.signal(name, cancel_prompt)
        if interactive and not ui.open():
            raise ValueError(ui.t('no_terminal'))
        if not interactive and sys.stdout.isatty():
            ui.color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
        ui.heading()
        cluster = Cluster()
        action = args.action
        if interactive and action is None:
            action = ui.choose(ui.t('operation'), [Choice(a, ui.t(a), ui.t(a + '_description')) for a in ('install', 'repair', 'uninstall', 'finish')], ['install'])[0]
        if action is None:
            raise ValueError(ui.t('no_terminal'))
        inventory = cluster.inventory()
        requested = list(dict.fromkeys(args.nodes.split(','))) if args.nodes else None
        if interactive and requested is None:
            choices = []
            for node in inventory:
                enabled = supported(node, action)
                label = node['node'] + (' (' + ui.t('local') + ')' if node['node'] == cluster.local else '')
                label += '  ' + (node['mode'] or ui.t('fresh')) + ('  ' + ui.t('pending') if node['pending'] else '')
                if not enabled:
                    label += '  ' + ui.t('unavailable')
                description = node['error'] or node['version']
                if not enabled and not node['error']:
                    description = ui.t('unsupported') + ' ' + description
                choices.append(Choice(node['node'], label, description, enabled))
            if not any(c.enabled for c in choices):
                raise ValueError(ui.t('no_nodes'))
            requested = ui.choose(ui.t('targets'), choices, [cluster.local] if any(c.key == cluster.local and c.enabled for c in choices) else [], multiple=True)
        requested = requested or [cluster.local]
        indexed = {node['node']: node for node in inventory}
        if any(name not in indexed for name in requested):
            raise ValueError('Unknown cluster node: ' + ', '.join(name for name in requested if name not in indexed))
        nodes = [indexed[name] for name in requested]
        if any(not supported(node, action) for node in nodes):
            raise ValueError(ui.t('unavailable') + ': ' + ', '.join(node['node'] for node in nodes if not supported(node, action)))
        if action in ('uninstall', 'finish', 'check'):
            modes = {node['node']: node['mode'] for node in nodes}
        else:
            mode = args.runtime
            fresh_only = False
            if interactive and any(node['mode'] is None for node in nodes) and mode is None:
                fresh_only = True
                mode = ui.choose(ui.t('runtime'), [Choice(name, name, ui.t(name + '_description')) for name in ('rootful', 'rootless')], ['rootless'])[0]
            modes = modes_for(nodes, mode, fresh_only=fresh_only)
        ui.output('  ' + ui.t('summary'))
        ui.output('  ' + ui.t(action))
        for node in nodes:
            ui.output('  ' + node['node'] + '  ' + (modes[node['node']] or ui.t('fresh')) + ('  ' + ui.t('fixed') if node['mode'] and action not in ('uninstall', 'finish', 'check') else ''))
        notice = 'uninstall_description' if action == 'uninstall' else ('check_description' if action == 'check' else 'restart_notice')
        ui.output('  ' + ui.t(notice))
        if interactive:
            ui.confirm(ui.t('start'))
        runner = Runner(cluster, ui)
        runner.development = args.development_source
        runner.allow_downgrade = args.allow_downgrade
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, runner.cancel)
        runner.prepare(requested)
        if action != 'finish':
            runner.preflight(requested, action, modes)
        if action == 'check':
            for node in requested:
                ui.output('  ' + node + '  ' + ui.t('done'))
        elif action != 'finish':
            runner.execute(requested, action, modes)
        ui.output()
        ui.output('  ' + ui.t('results'))
        for result in runner.results:
            ui.output('  ' + result['node'] + '  ' + ui.t('failed' if result['code'] else 'done'))
            if result['log']:
                ui.output('    ' + ui.t('log', path=result['log']))
        pending = runner.pending(requested) if action != 'check' else []
        if action == 'finish' and not pending:
            ui.output('  ' + ui.t('no_pending'))
        if pending:
            command = 'bash /var/lib/betterdocker/installer/source/start.sh finish --nodes=' + ','.join(pending) + ' --yes' + (' --development-source' if runner.development else '')
            ui.output('  ' + ui.t('pending_nodes', nodes=', '.join(pending)))
            ui.output('  ' + ui.t('resume', command=command))
            if not args.no_restart and not runner.interrupted:
                if interactive:
                    for name in handlers:
                        signal.signal(name, cancel_prompt)
                    ui.confirm(ui.t('reload_prompt'))
                    for name in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(name, runner.cancel)
                restarted = runner.finish(pending)
                runner.results += restarted
                if not runner.interrupted and all(result['code'] == 0 for result in restarted):
                    ui.output('  ' + ui.t('refresh_browser'))
        return 130 if runner.interrupted else int(any(result['code'] for result in runner.results))
    except (Cancelled, KeyboardInterrupt):
        changed = runner and any(item['phase'] == 'apply' for item in runner.results)
        ui.output(ui.t('cancelled' if changed else 'cancelled_before'))
        return 130
    finally:
        for name, previous in handlers.items():
            signal.signal(name, previous)
        if runner:
            runner.cleanup()
        ui.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
