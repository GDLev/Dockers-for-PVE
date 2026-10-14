#!/usr/bin/env python3
"""Command entry point used by PVE and the installed services."""
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
import subprocess
import sys
from dctlib.cli import main
from dctlib.security_errors import public_error
from dctlib.authorization import require_host_root


if __name__ == '__main__':
    try:
        require_host_root(sys.argv[1:])
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('BetterDocker: ' + public_error(error), file=sys.stderr)
        sys.exit(1)
