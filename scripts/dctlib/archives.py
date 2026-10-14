"""Inspect archive headers in the offline account while retaining the input inode."""
import json
import contextlib
import os
from pathlib import Path
import pwd
import sys
import tarfile
from .process import run
from .secure_io import structure
from pathlib import PurePosixPath

MAX_MEMBERS = 100000
MAX_PATH = 4096
MAX_HEADERS = 32 * 1024 * 1024
MAX_CAPACITY = 1024 ** 4


def record(member):
    name = PurePosixPath(member.name)
    if len(member.name.encode()) > MAX_PATH or '\x00' in member.name or name.is_absolute() or '..' in name.parts:
        raise ValueError('Invalid archive path')
    if len(member.linkname.encode()) > MAX_PATH or '\x00' in member.linkname:
        raise ValueError('Invalid archive link length')
    if type(member.size) is not int or member.size < 0:
        raise ValueError('Invalid archive member size')
    kind = next((kind for kind, matches in (('file', member.isfile()), ('dir', member.isdir()),
                 ('symlink', member.issym()), ('hardlink', member.islnk())) if matches), None)
    if kind is None:
        raise ValueError('Archive contains a special file')
    return {'name': member.name, 'size': member.size, 'link': member.linkname, 'kind': kind}


def members(fd, capacity):
    if type(capacity) is not int or capacity < 0:
        raise ValueError('Invalid archive capacity')
    capacity = min(capacity, MAX_CAPACITY)
    options = {}
    if os.geteuid() == 0:
        account = pwd.getpwnam('betterdocker-parser')
        options.update(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
    result = run([sys.executable, str(Path(__file__).resolve().parents[1] / 'archive_scan.py'), str(fd), str(capacity)],
                 pass_fds=(fd,), capture_output=True, text=True, check=False, timeout=1800, output_limit=MAX_HEADERS,
                 env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'}, **options)
    if result.returncode:
        raise ValueError('Invalid, oversized or unsupported archive')
    records = structure(json.loads(result.stdout), maximum=1000000)
    if not isinstance(records, list) or len(records) > MAX_MEMBERS:
        raise ValueError('Invalid archive inspection response')
    output = []; names = set(); total = 0
    kinds = {'file': tarfile.REGTYPE, 'dir': tarfile.DIRTYPE, 'symlink': tarfile.SYMTYPE, 'hardlink': tarfile.LNKTYPE}
    for value in records:
        if not isinstance(value, dict) or set(value) != {'name', 'size', 'link', 'kind'} or not all(isinstance(value[key], str) for key in ('name', 'link', 'kind')) or value['kind'] not in kinds:
            raise ValueError('Invalid archive inspection response')
        member = tarfile.TarInfo(value['name']); member.size = value['size']; member.linkname = value['link']
        member.type = kinds[value['kind']]
        record(member)
        name = str(PurePosixPath(member.name))
        total += member.size
        if name in names or total > capacity:
            raise ValueError('Duplicate member or archive capacity exceeded')
        names.add(name)
        output.append(member)
    return output


def validate_images(path=None, fd=None):
    from .secure_io import anchored
    from pathlib import PurePosixPath
    with (anchored(path) if fd is None else contextlib.nullcontext(fd)) as source:
        entries = members(source, 1024 ** 4)
        os.lseek(source, 0, os.SEEK_SET)
    regular = {entry.name for entry in entries if entry.isfile()}
    for entry in entries:
        if entry.issym() or entry.islnk():
            target = PurePosixPath(entry.linkname)
            if entry.issym() or target.is_absolute() or '..' in target.parts or str(target) not in regular:
                raise ValueError('Unsafe Docker image archive link')
        if entry.name.endswith('.json') and entry.size > 32 * 1024 * 1024:
            raise ValueError('Docker image metadata exceeds its size limit')
