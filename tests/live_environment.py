"""Explicit configuration for tests that use real PVE nodes."""
from functools import lru_cache
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import ssl


@lru_cache(maxsize=1)
def config():
    filename = os.environ.get('BETTERDOCKER_TEST_CONFIG')
    if not filename:
        raise ValueError('Set BETTERDOCKER_TEST_CONFIG to a private live-test JSON file')
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Live-test configuration must be owned by the current user with mode 0600')
        with os.fdopen(descriptor, 'r', closefd=False) as handle:
            value = json.load(handle)
    finally:
        os.close(descriptor)
    for name in ('primary', 'secondary', 'rootless'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', value['nodes'][name]):
            raise ValueError('Invalid test node name: ' + name)
    ipaddress.IPv4Interface(value['addresses'])
    os.umask(0o077)
    return value


def node(name):
    return config()['nodes'][name]


def require_node(value):
    if value not in config()['nodes'].values():
        raise ValueError('Target is not a configured test node')
    return value


def address(offset):
    value = ipaddress.IPv4Interface(config()['addresses'])
    result = value.ip + offset
    if result not in value.network:
        raise ValueError('Test address is outside the configured subnet')
    return str(result)


def prefix():
    return ipaddress.IPv4Interface(config()['addresses']).network.prefixlen


def backup_storage():
    return config()['backup_storage']


def source():
    return config().get('source', str(Path(__file__).resolve().parents[1]))


def package():
    return config()['package']


def server():
    return config().get('api_host', node('primary'))


def tls():
    context = ssl.create_default_context(cafile=config().get('ca_file', '/etc/pve/pve-root-ca.pem'))
    # Native PVE CAs omit extensions required by Python 3.13 strict mode.
    # Signature, expiry and hostname verification remain enabled.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context
