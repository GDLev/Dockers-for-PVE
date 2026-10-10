#!/usr/bin/env python3
"""Command entry point used by PVE and the installed services."""
import subprocess
import sys
from dctlib.projects import main


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'BetterDocker: {error}', file=sys.stderr)
        sys.exit(1)
