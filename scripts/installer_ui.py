"""Small terminal forms. Read /dev/tty so curl's stdin remains untouched."""
from dataclasses import dataclass
import contextlib
import io
import os
import re
import select
import textwrap
import termios
import tty


MESSAGES = {
    'installer': 'Installer',
    'operation': 'Choose an operation',
    'install': 'Install / Update',
    'repair': 'Repair',
    'uninstall': 'Uninstall',
    'finish': 'Finish pending reload',
    'check': 'Check compatibility',
    'install_description': 'Install dependencies, Docker and the PVE extension. Existing runtime modes are preserved.',
    'repair_description': 'Restore extension files and registration. Missing dependencies require Install / Update.',
    'uninstall_description': 'Remove the PVE integration. Docker, workloads, data and network/firewall maintenance remain. Remove DCT HA resources and replication jobs first.',
    'finish_description': 'Reload PVE services after an interrupted or deferred installation.',
    'rootful_description': 'Docker runs as root. Docker AppArmor and writable-layer quotas are available when supported by the host. Containers share a privileged daemon.',
    'rootless_description': 'Docker runs as the betterdocker user. PVE host operations still need root. Requires cgroup v2 and compatible rootless binaries. Container AppArmor and writable-layer quotas are unavailable.',
    'multi_keys': 'Up/Down move  Space select  Enter continue  Esc cancel',
    'keys': 'Up/Down choose  Enter continue  Esc cancel',
    'numbers': 'Node numbers separated by commas [Enter keeps selection, q cancels]:',
    'number': 'Option number [Enter keeps selection, q cancels]:',
    'enter': 'Enter to continue, q to cancel:',
    'invalid_choice': 'Choose an available option.',
    'no_terminal': 'No interactive terminal. Specify an operation and --yes; fresh installations also need --runtime=rootful or --runtime=rootless.',
    'no_nodes': 'No supported nodes are available.',
    'local': 'local',
    'fresh': 'not configured',
    'pending': 'reload pending',
    'unavailable': 'unavailable',
    'unsupported': 'Supported PVE releases: 9.1 and 9.2.',
    'runtime': 'Runtime for new installations',
    'targets': 'Choose target nodes',
    'summary': 'Summary',
    'fixed': '[saved mode]',
    'restart_notice': 'Changes apply node by node. Panel/API reload follows the result summary.',
    'start': 'Press Enter to start. Esc cancels without applying changes.',
    'results': 'Results',
    'log': 'Log: {path}',
    'pending_nodes': 'Panel/API reload pending on: {nodes}',
    'resume': 'To finish later: {command}',
    'reload_prompt': 'Press Enter to reload the PVE panel and API now. Esc leaves reload pending.',
    'refresh_browser': 'Reload complete. Refresh the browser with Ctrl+Shift+R.',
    'cancelled': 'Cancelled. Completed changes remain; use the printed finish command if a reload is pending.',
    'done': 'Done',
    'failed': 'Failed',
    'reloaded': 'Reloaded',
    'unknown_restart': 'Cannot check reload state on {node}. Retry the finish command after restoring SSH access.',
    'finishing_node': 'Cancellation requested. Finishing the current operation on {node}; remaining nodes will be skipped.',
    'stage_apply': 'Applying changes',
    'stage_restart': 'Reloading PVE services',
    'stage_dependencies': 'Installing dependencies',
    'stage_backup': 'Saving integration backups',
    'stage_runtime': 'Configuring Docker runtime',
    'stage_integration': 'Registering the PVE extension',
    'stage_services': 'Starting extension services',
    'stage_remove': 'Removing PVE integration',
    'stage_preflight': 'Checking prerequisites',
    'cancelled_before': 'Cancelled. No changes applied.',
    'check_description': 'Read-only compatibility check. No installation changes or service reload.',
    'no_pending': 'No panel/API reload is pending.',
}


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    description: str = ''
    enabled: bool = True


class Terminal:
    def __init__(self):
        self.stream = None
        self.rows = 0
        self.frame = 0
        self.color = False
        self.cursor = False

    def t(self, key, **values):
        return MESSAGES[key].format(**values)

    def open(self):
        try:
            self.stream = io.TextIOWrapper(io.FileIO('/dev/tty', 'r+'), line_buffering=True, write_through=True)
        except OSError:
            return False
        self.cursor = os.environ.get('TERM', 'dumb') != 'dumb'
        self.color = self.cursor and 'NO_COLOR' not in os.environ
        return True

    def close(self):
        if self.stream:
            self.stream.write('\033[?25h' if self.cursor else '')
            self.stream.close()
            self.stream = None

    def style(self, text, code='38;5;208'):
        return '\033[' + code + 'm' + text + '\033[0m' if self.color else text

    def output(self, text=''):
        clean = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(text))
        target = self.stream if self.stream else __import__('sys').stdout
        target.write(clean + '\n')
        target.flush()

    def heading(self):
        self.output()
        target = self.stream if self.stream else __import__('sys').stdout
        target.write(self.style('  BetterDockers') + '  ' + self.t('installer') + '\n\n')

    def width(self):
        try:
            return max(20, (os.get_terminal_size(self.stream.fileno()).columns or 80) - 2)
        except OSError:
            return 78

    def paint(self, lines):
        width = self.width()
        wrapped = []
        for line in lines:
            wrapped.extend(textwrap.wrap(line, width=width, subsequent_indent='    ', replace_whitespace=False) or [''])
        if self.rows:
            self.stream.write('\033[' + str(self.rows) + 'A')
        for line in wrapped:
            code = '38;5;208' if line.startswith('  >') else ('2' if line.startswith('    ') else '0')
            self.stream.write('\033[2K' + self.style(line, code) + '\n')
        for _ in range(max(0, self.rows - len(wrapped))):
            self.stream.write('\033[2K\n')
        if self.rows > len(wrapped):
            self.stream.write('\033[' + str(self.rows - len(wrapped)) + 'A')
        self.rows = len(wrapped)
        self.stream.flush()

    @contextlib.contextmanager
    def keys(self):
        previous = termios.tcgetattr(self.stream.fileno())
        try:
            tty.setraw(self.stream.fileno())
            raw = termios.tcgetattr(self.stream.fileno())
            raw[1] = previous[1]
            termios.tcsetattr(self.stream.fileno(), termios.TCSANOW, raw)
            self.stream.write('\033[?25l')
            yield
        finally:
            termios.tcsetattr(self.stream.fileno(), termios.TCSADRAIN, previous)
            self.stream.write('\033[?25h')
            self.stream.flush()

    def key(self):
        value = os.read(self.stream.fileno(), 1)
        if value == b'\x1b':
            suffix = b''
            while select.select([self.stream.fileno()], [], [], .08)[0]:
                suffix += os.read(self.stream.fileno(), 1)
                if suffix in (b'[A', b'[B', b'OA', b'OB') or len(suffix) >= 5:
                    break
            return {b'[A': 'up', b'[B': 'down', b'OA': 'up', b'OB': 'down'}.get(suffix, 'cancel')
        if value in (b'\x03', b'\x04', b''):
            return 'cancel'
        if value in (b'\r', b'\n'):
            return 'enter'
        return value.decode('ascii', errors='ignore')

    def choose(self, title, choices, selected=None, multiple=False):
        picked = {c.key for c in choices if c.enabled and c.key in (selected or [])}
        focus = next((i for i, c in enumerate(choices) if c.key in picked), 0)
        if not self.cursor:
            return self.numbered(title, choices, picked, multiple)
        with self.keys():
            while True:
                lines = ['  ' + title, '']
                for i, choice in enumerate(choices):
                    mark = '[x]' if choice.key in picked else '[ ]'
                    if not multiple:
                        mark = '(*)' if i == focus else '( )'
                    lines.append('  ' + ('>' if i == focus else ' ') + ' ' + mark + ' ' + choice.label)
                lines += ['', '    ' + choices[focus].description, '', '  ' + self.t('multi_keys' if multiple else 'keys')]
                self.paint(lines)
                key = self.key()
                if key == 'cancel':
                    raise Cancelled()
                if key in ('up', 'k'):
                    focus = (focus - 1) % len(choices)
                elif key in ('down', 'j'):
                    focus = (focus + 1) % len(choices)
                elif key == ' ' and multiple and choices[focus].enabled:
                    name = choices[focus].key
                    picked.remove(name) if name in picked else picked.add(name)
                elif key == 'enter' and ((multiple and picked) or (not multiple and choices[focus].enabled)):
                    result = [c.key for c in choices if c.key in picked] if multiple else [choices[focus].key]
                    label = ', '.join(result) if multiple else choices[focus].label
                    self.paint(['  ' + title + '  ' + label])
                    self.rows = 0
                    self.output()
                    return result

    def numbered(self, title, choices, picked, multiple):
        self.output('  ' + title)
        for i, choice in enumerate(choices, 1):
            self.output('  ' + str(i) + '. ' + choice.label + (' [x]' if choice.key in picked else ''))
            self.output('     ' + choice.description)
        while True:
            self.stream.write('  ' + self.t('numbers' if multiple else 'number') + ' ')
            line = self.stream.readline()
            if not line or line.strip().lower() in ('q', 'cancel'):
                raise Cancelled()
            if not line.strip() and picked:
                return [c.key for c in choices if c.key in picked]
            try:
                positions = [int(i) - 1 for i in line.replace(',', ' ').split()]
                if not positions or (not multiple and len(positions) != 1):
                    raise ValueError()
                if any(i < 0 or i >= len(choices) or not choices[i].enabled for i in positions):
                    raise ValueError()
                if not multiple:
                    self.output(choices[positions[0]].description)
                    self.stream.write('  ' + self.t('enter') + ' ')
                    if not self.stream.readline():
                        raise Cancelled()
                return list(dict.fromkeys(choices[i].key for i in positions))
            except ValueError:
                self.output(self.t('invalid_choice'))

    def confirm(self, message):
        self.output('  ' + message)
        if self.cursor:
            with self.keys():
                while True:
                    value = self.key()
                    if value == 'enter':
                        return
                    if value == 'cancel':
                        raise Cancelled()
        else:
            line = self.stream.readline()
            if not line or line.strip().lower() in ('q', 'cancel'):
                raise Cancelled()

    def progress(self, node, phase, elapsed):
        if not self.stream or not self.cursor:
            return
        self.frame += 1
        status = textwrap.shorten(node + '  ' + self.t('stage_' + phase) + '  ' + str(int(elapsed)) + 's', width=max(10, self.width() - 4), placeholder='...')
        self.stream.write('\r\033[2K  ' + self.style('|/-\\'[self.frame % 4]) + ' ' + status)
        self.stream.flush()

    def end_progress(self):
        if self.stream and self.cursor:
            self.stream.write('\r\033[2K')
