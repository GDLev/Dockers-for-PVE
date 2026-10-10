#!/usr/bin/env python3
"""dhclient hook: configure only the managed project NIC; never host routing or DNS."""
import os
import sys
from dctlib.projects import dhcp_event

if __name__ == '__main__':
    try:
        dhcp_event(os.environ)
    except Exception as error:
        print(f'BetterDocker DHCP: {error}', file=sys.stderr)
        sys.exit(1)
