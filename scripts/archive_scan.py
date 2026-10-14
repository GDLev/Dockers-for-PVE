#!/usr/bin/env python3
"""Unprivileged header scan; no member is extracted or executed here."""
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
import os
from pathlib import PurePosixPath
import resource
import sys
import tarfile
from dctlib.archives import record, MAX_MEMBERS, MAX_HEADERS, MAX_CAPACITY


def main():
    if os.geteuid() == 0:
        raise ValueError('Archive inspection cannot run as root')
    if sys.platform == 'linux' and ctypes.CDLL(None).prctl(38, 1, 0, 0, 0):
        raise ValueError('Cannot enable no-new-privileges for archive inspection')
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (1200, 1200))
    maximum = min(int(sys.argv[2]), MAX_CAPACITY)
    if maximum < 0:
        raise ValueError('Invalid archive capacity')
    records = []; names = set(); total = 0; output_size = 2
    with os.fdopen(os.dup(int(sys.argv[1])), 'rb') as source, tarfile.open(fileobj=source, mode='r:*') as archive:
        for entry in archive:
            value = record(entry)
            name = PurePosixPath(entry.name)
            if str(name) in names or len(records) >= MAX_MEMBERS:
                raise ValueError('Duplicate member or archive limit exceeded')
            names.add(str(name)); total += entry.size
            if total > maximum:
                raise ValueError('Archive exceeds available capacity')
            encoded = json.dumps(value, separators=(',', ':'))
            output_size += len(encoded) + bool(records)
            if output_size + 1 > MAX_HEADERS:
                raise ValueError('Archive headers exceed their size limit')
            records.append(encoded)
    print('[' + ','.join(records) + ']')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, tarfile.TarError):
        print('Invalid archive', file=sys.stderr); sys.exit(1)
