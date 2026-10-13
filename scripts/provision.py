#!/usr/bin/env python3
"""Command entry point used by PVE and the installed services."""
import subprocess
import sys
from dctlib.projects import main
from dctlib.security_errors import public_error
from dctlib.authorization import require_host_root


if __name__ == '__main__':
    try:
        require_host_root(sys.argv[1:])
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('BetterDocker: ' + public_error(error), file=sys.stderr)
        sys.exit(1)
