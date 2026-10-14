#!/usr/bin/env python3
"""Offline parser. The installer supplies an unprivileged account without Docker access."""
# Check the installed code tree before importing non-frozen modules.
import os as _boot_os
import sys as _boot_sys
if __file__.startswith('/usr/local/lib/betterdocker/'):
    _boot_root = '/usr/local/lib/betterdocker'
    for _boot_dir, _boot_dirs, _boot_files in _boot_os.walk(_boot_root, followlinks=False):
        for _boot_name in [_boot_dir, *(_boot_os.path.join(_boot_dir, n) for n in _boot_dirs + _boot_files)]:
            _boot_info = _boot_os.stat(_boot_name, follow_symlinks=False)
            if _boot_info.st_uid != 0 or _boot_info.st_mode & 0o022 or _boot_os.path.islink(_boot_name):
                _boot_sys.stderr.write('BetterDocker: unsafe installed helper ownership or permissions\n')
                raise SystemExit(1)
import json
import ctypes
import subprocess
import os
from pathlib import Path
import resource
import sys
import tempfile
import yaml
from dctlib.process import run
from dctlib.secure_io import structure


class Loader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream); self.nodes = 0; self.level = 0; self.aliases = 0

    def compose_node(self, parent, index):
        self.nodes += 1; self.level += 1
        if self.check_event(yaml.AliasEvent):
            self.aliases += 1
        if self.nodes > 100000 or self.level > 64 or self.aliases > 64:
            raise ValueError('YAML limits exceeded')
        try:
            return super().compose_node(parent, index)
        finally:
            self.level -= 1


def main():
    if os.geteuid() == 0:
        raise ValueError('The Compose parser must run without root privileges')
    if sys.platform == 'linux' and ctypes.CDLL(None).prctl(38, 1, 0, 0, 0):
        raise ValueError('Cannot enable no-new-privileges for parser')
    if sys.platform == 'linux':
        # Go reserves a large virtual arena even for `compose config`.
        # Bound allocations separately from that address-space reservation.
        resource.setrlimit(resource.RLIMIT_AS, (4 * 1024 ** 3,) * 2)
        resource.setrlimit(resource.RLIMIT_DATA, (512 * 1024 ** 2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Parser input is oversized')
    value = structure(json.loads(raw))
    parsed = structure(yaml.load(value['source'], Loader=Loader))
    if not value['canonical']:
        print(json.dumps(parsed)); return
    with tempfile.TemporaryDirectory(prefix='betterdocker-parse-') as temporary:
        folder = Path(temporary)
        (folder / 'compose.yaml').write_text(value['source']); (folder / '.env').write_text(value['environment'])
        (folder / 'compose.yaml').chmod(0o600); (folder / '.env').chmod(0o600)
        result = run(['docker', 'compose', '--project-name', value['project'], '--project-directory', str(folder),
                      '--env-file', str(folder / '.env'), '-f', str(folder / 'compose.yaml'),
                      'config', '--no-env-resolution', '--format', 'json'],
                 capture_output=True, text=True, check=True, timeout=15, start_new_session=False,
                     env={'PATH': '/usr/bin:/bin:/usr/local/bin', 'HOME': str(folder), 'DOCKER_CONFIG': str(folder)})
        config = structure(json.loads(result.stdout))
        for service in config.get('services', {}).values():
            for mount in service.get('volumes') or []:
                if mount.get('type') == 'bind' and Path(mount['source']).is_relative_to(folder):
                    mount['source'] = str(Path(value['directory']) / Path(mount['source']).relative_to(folder))
            for entry in service.get('env_file') or []:
                if isinstance(entry, dict) and entry.get('path') == str(folder / '.env'):
                    entry['path'] = str(Path(value['directory']) / '.env')
        print(json.dumps(config))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RecursionError, yaml.YAMLError, subprocess.SubprocessError):
        print('Invalid Compose configuration', file=sys.stderr); sys.exit(1)
