#!/usr/bin/env python3
"""dhclient hook: configure only the managed project NIC; never host routing or DNS."""
import os
import sys
from dctlib.projects import dhcp_event

if __name__ == '__main__':
    try:
        if os.environ.get('reason', '').endswith('6'):
            from dctlib.ipv6_dynamic import event
            event(os.environ)
        else:
            dhcp_event(os.environ)
    except Exception as error:
        print(f'BetterDocker DHCP: {error}', file=sys.stderr)
        sys.exit(1)
