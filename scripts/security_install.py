#!/usr/bin/env python3
"""Provision the offline parser, recovery key and rootful AppArmor profile."""
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys
from dctlib.secure_io import KEY, atomic_text, read_json, journal


def initialize(mode, profile):
    if os.geteuid() != 0 or mode not in ('rootful', 'rootless'):
        raise ValueError('Security setup requires host root and a selected runtime')
    try:
        account = pwd.getpwnam('betterdocker-parser')
    except KeyError:
        subprocess.run(['/usr/sbin/useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent',
                        '--shell', '/usr/sbin/nologin', 'betterdocker-parser'], check=True)
        account = pwd.getpwnam('betterdocker-parser')
    if account.pw_uid == 0 or account.pw_dir != '/nonexistent' or account.pw_shell != '/usr/sbin/nologin':
        raise ValueError('Offline parser account has an unexpected identity')
    groups = subprocess.check_output(['/usr/bin/id', '-Gn', account.pw_name], text=True).split()
    if set(groups) & {'docker', 'root', 'sudo', 'adm', 'www-data'}:
        raise ValueError('Offline parser account has privileged group membership')
    if not KEY.exists():
        root = Path('/var/lib/betterdocker')
        pending = [p for p in (root / 'projects').glob('*/*.json') if journal(p)] + list((root / 'imports').glob('*.json'))
        if pending:
            raise ValueError('Finish interrupted DCT operations with the previous version before installing recovery authentication')
        atomic_text(KEY, secrets.token_hex(32))
    if mode == 'rootful':
        enabled = Path('/sys/module/apparmor/parameters/enabled')
        if enabled.exists() and enabled.read_text().strip() == 'Y':
            target = Path('/etc/apparmor.d/betterdocker')
            atomic_text(target, Path(profile).read_text())
            target.chmod(0o644)
            subprocess.run(['/usr/sbin/apparmor_parser', '-r', str(target)], check=True)


if __name__ == '__main__':
    initialize(*sys.argv[1:])
