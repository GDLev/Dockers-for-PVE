"""Parse untrusted YAML and delegated Compose with no host or daemon privileges."""
import json
import os
from pathlib import Path
import pwd
import sys
from .process import run
from .secure_io import structure

ACCOUNT = 'betterdocker-parser'


def parse(source, environment='', directory='', canonical=False, identifier=''):
    script = Path(__file__).resolve().parents[1] / 'parse_config.py'
    options = {}
    if os.geteuid() == 0:
        account = pwd.getpwnam(ACCOUNT)
        options.update(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
    result = run([sys.executable, str(script)], input=json.dumps({'source': source, 'environment': environment,
                 'directory': str(directory), 'canonical': canonical, 'project': 'bd-' + identifier}), capture_output=True, text=True,
                 check=False, timeout=20, env={'PATH': '/usr/bin:/bin:/usr/local/bin', 'HOME': '/nonexistent'}, **options)
    if result.returncode:
        raise ValueError('Invalid or oversized Compose configuration')
    try:
        return structure(json.loads(result.stdout))
    except (ValueError, RecursionError):
        raise ValueError('Invalid Compose parser response') from None
