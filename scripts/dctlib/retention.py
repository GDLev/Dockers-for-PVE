"""Administrator budgets for retained artifacts and temporary operation data."""
import contextlib
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat

from .secure_io import anchored, read_json
from . import operation_capacity as admission

GiB = 1024 ** 3
POLICY = Path('/etc/betterdocker/retention-policy.json')
ROOT = Path('/var/lib/betterdocker/retention-reservations')
DEFAULTS = {'snapshots': 16, 'snapshot_bytes': 32 * GiB, 'preserved_image_bytes': 8 * GiB,
            'log_file_bytes': 10 * 1024 ** 2, 'log_files': 3,
            'transfer_bytes': 32 * GiB, 'node_transfer_bytes': 64 * GiB}


def policy():
    value = read_json(POLICY) if POLICY.exists() else dict(DEFAULTS)
    if not isinstance(value, dict) or set(value) != set(DEFAULTS) or any(type(v) is not int or not 0 < v <= 1024 ** 5 for v in value.values()):
        raise ValueError('Retention policy requires finite positive integer budgets')
    if value['snapshots'] > 10000 or value['log_files'] > 100 or value['transfer_bytes'] > value['node_transfer_bytes']:
        raise ValueError('Invalid retention counts or transfer budgets')
    return value


def tree_bytes(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        return 0
    total = 0
    for folder, directories, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = (Path(folder) / name).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode): total += info.st_size
    return total


def distinct(paths):
    values = sorted({str(Path(p).absolute()) for p in paths}, key=lambda p: (len(Path(p).parts), p))
    result = []
    for path in values:
        if not any(Path(path).is_relative_to(parent) for parent in result): result.append(path)
    return result


def snapshots(identifier, estimated=0, incoming=None):
    from . import projects as w
    chosen = policy()
    root = w.snapshot_root(identifier)
    existing = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')] if root.exists() else []
    count = len(existing) + (len(incoming) if incoming is not None else 1)
    def payload(path):
        total = tree_bytes(path)
        manifest = Path(path) / 'manifest.json'
        raw = Path(path) / 'disk.raw'
        if manifest.exists() and read_json(manifest).get('native_storage') and raw.is_file() and not raw.is_symlink():
            total -= raw.stat().st_size
        return total
    total = sum(payload(p) for p in existing) + (sum(payload(p) for p in incoming) if incoming is not None else estimated)
    if count > chosen['snapshots']:
        raise ValueError('DCT snapshot count limit reached; remove a snapshot or increase the administrator limit')
    if total > chosen['snapshot_bytes']:
        raise ValueError('DCT snapshot file budget exceeded; remove a snapshot or increase the administrator limit')


def preservation(identifier, containers=None):
    from . import projects as w
    if containers == []:
        return
    containers = containers or []
    rows = w.run(['docker', 'image', 'ls', '--no-trunc', '--format', '{{json .}}'], capture_output=True)
    refs = [json.loads(line) for line in rows.stdout.splitlines()]
    ids = {row['ID'] for row in refs if row['Repository'] in ('betterdocker-settings/' + identifier,
           'betterdocker-snapshot/' + identifier, 'betterdocker-export/' + identifier) or
           row['Repository'] == 'betterdocker-rebind' and row['Tag'].startswith(identifier + '-')}
    ids.update(row['Image'] for row in containers if row.get('Image') and row['Image'] != (row.get('Config', {}).get('Labels') or {}).get('com.docker.compose.image', row['Image']))
    retained = json.loads(w.capture(['docker', 'image', 'inspect', *sorted(ids)])) if ids else []
    # Charge complete image sizes conservatively; shared layers are not subtracted.
    total = sum(row['Size'] for row in retained) + sum(max(0, row.get('SizeRootFs', row.get('SizeRw', 0))) for row in containers)
    if total > policy()['preserved_image_bytes']:
        raise ValueError('DCT preserved-image budget exceeded; increase the administrator limit before preserving files')


def logs(options):
    chosen = policy()
    raw = str(options.get('max-size', str(chosen['log_file_bytes'])))
    match = re.fullmatch(r'([0-9]+)([kmg]?)', raw, re.I)
    count = str(options.get('max-file', chosen['log_files']))
    size = int(match[1]) * 1024 ** ('', 'k', 'm', 'g').index(match[2].lower()) if match else 0
    if not 0 < size <= chosen['log_file_bytes'] or not count.isdigit() or not 0 < int(count) <= chosen['log_files']:
        raise ValueError('Log rotation exceeds the administrator retention policy')


def check_transfer(paths, estimated=0):
    if type(estimated) is not int or estimated < 0:
        raise ValueError('Invalid temporary transfer estimate')
    used = sum(tree_bytes(p) for p in distinct(paths))
    if used + estimated > policy()['transfer_bytes']:
        raise ValueError('DCT temporary transfer budget exceeded; increase the administrator limit')
    return used


def remaining_transfer():
    record = read_json(ROOT / (admission.identity() + '.json'))
    return max(0, policy()['transfer_bytes'] - sum(tree_bytes(p) for p in distinct(record['paths'])))


def active(record):
    if not re.fullmatch(r'[0-9]+\.lock', record.get('slot', '')) or not re.fullmatch(r'[0-9a-f]{32}', record.get('token', '')):
        raise ValueError('Invalid operation reservation identity')
    path = admission.ROOT / record['slot']
    if not path.exists(): return False
    with anchored(path) as fd:
        if os.pread(fd, 32, 0).decode() != record['token']: return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False



def validate_record(record):
    if not isinstance(record, dict) or set(record) - {'token', 'slot', 'paths', 'disposable'} or not {'token', 'slot', 'paths'} <= set(record) or not re.fullmatch(r'[0-9a-f]{32}', record['token']) or not re.fullmatch(r'[0-9]+\.lock', record['slot']) or not isinstance(record['paths'], list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in record['paths']):
        raise ValueError('Invalid transfer budget record')
    if not isinstance(record.get('disposable', []), list):
        raise ValueError('Invalid temporary file ownership list')
    for item in record.get('disposable', []):
        if not isinstance(item, dict) or set(item) != {'path', 'device', 'inode', 'parent_device', 'parent_inode'} or not isinstance(item['path'], str) or any(type(item[key]) is not int for key in ('device', 'inode', 'parent_device', 'parent_inode')):
            raise ValueError('Invalid temporary file ownership record')
    return record


def own_partial(path, fd):
    """Enroll only the newly opened receive file, never a caller's backup output."""
    from . import projects as w
    path = Path(path).absolute()
    base = w.ROOT.parent / 'transfers'
    if path.name != 'state.partial' or path.parent.parent != base or not re.fullmatch(r'[0-9a-f]{32}', path.parent.name):
        raise ValueError('Invalid temporary receive path')
    info = os.fstat(fd)
    from .secure_io import trusted
    trusted(info)
    with anchored(path.parent, directory=True) as parent:
        actual = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        folder = os.fstat(parent)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
        raise ValueError('Temporary receive file identity changed')
    with anchored(ROOT, directory=True) as directory:
        gate = admission.open_lock(directory, 'allocation.lock')
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            file = ROOT / (admission.identity() + '.json')
            record = validate_record(read_json(file))
            if not any(path.is_relative_to(Path(p)) for p in record['paths']):
                raise ValueError('Temporary receive file is outside its operation')
            record.setdefault('disposable', []).append({'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
                'parent_device': folder.st_dev, 'parent_inode': folder.st_ino})
            w.write_json(file, record)
        finally:
            os.close(gate)


def remove_partial(item, protected):
    from . import projects as w
    path = Path(item['path'])
    base = w.ROOT.parent / 'transfers'
    if path.parent.parent != base or path.name != 'state.partial' or not re.fullmatch(r'[0-9a-f]{32}', path.parent.name):
        return False
    if any(path.is_relative_to(Path(p)) or Path(p).is_relative_to(path.parent) for p in protected):
        return False
    try:
        with anchored(path.parent, directory=True) as parent:
            folder = os.fstat(parent)
            if (folder.st_dev, folder.st_ino) != (item['parent_device'], item['parent_inode']): return False
            # Any archive, metadata or extracted state keeps this directory for recovery.
            if set(os.listdir(parent)) != {'state.partial'}: return False
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (item['device'], item['inode']): return False
            os.unlink(path.name, dir_fd=parent)
        # Leave the directory itself: another receiver may have opened it.
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def reap_locked():
    records = [(file, validate_record(read_json(file))) for file in ROOT.glob('*.json')]
    protected = [path for _, record in records if active(record) for path in record['paths']]
    result = {'records_removed': 0, 'partial_files_removed': 0}
    for file, record in records:
        if active(record): continue
        for item in record.get('disposable', []):
            result['partial_files_removed'] += int(remove_partial(item, protected))
        if not any(tree_bytes(path) for path in distinct(record['paths'])):
            file.unlink(); result['records_removed'] += 1
    return result


def sweep():
    if not ROOT.exists(): return {'records_removed': 0, 'partial_files_removed': 0}
    with anchored(ROOT, directory=True) as directory:
        gate = admission.open_lock(directory, 'allocation.lock')
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            return reap_locked()
        finally:
            os.close(gate)


def register(paths):
    from . import projects as w
    chosen = policy()
    paths = distinct(paths)
    fd = admission.descriptor()
    token = admission.identity()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with anchored(ROOT, directory=True) as directory:
        gate = admission.open_lock(directory, 'allocation.lock')
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            reap_locked()
            name = ROOT / (token + '.json')
            previous = read_json(name)['paths'] if name.exists() else []
            paths = distinct(previous + paths)
            check_transfer(paths)
            total = chosen['transfer_bytes']
            covered = list(paths)
            records = list(ROOT.glob('*.json'))
            if len(records) > 4096: raise ValueError('Too many retained transfer records; review temporary data')
            for file in records:
                if file == name: continue
                record = read_json(file)
                validate_record(record)
                record_paths = distinct(record['paths'])
                size = sum(tree_bytes(p) for p in record_paths)
                uncharged = [p for p in record_paths if not any(Path(p).is_relative_to(root) or Path(root).is_relative_to(p) for root in covered)]
                overlap = any(Path(a).is_relative_to(b) or Path(b).is_relative_to(a) for a in paths for b in record_paths)
                running = active(record)
                if overlap and running:
                    raise ValueError('Temporary transfer data is in use by another operation')
                if overlap:
                    total += sum(tree_bytes(p) for p in uncharged)
                elif running: total += max(size, chosen['transfer_bytes'])
                elif size: total += sum(tree_bytes(p) for p in uncharged)
                else: file.unlink()  # Only empty accounting records, never data.
                covered.extend(record_paths)
            transfers = w.ROOT.parent / 'transfers'
            if transfers.exists():
                total += sum(tree_bytes(p) for p in transfers.iterdir() if not any(p.is_relative_to(Path(root)) or Path(root).is_relative_to(p) for root in covered))
            if total > chosen['node_transfer_bytes']:
                raise ValueError('Node temporary transfer budget exhausted; finish or clean up retained transfers')
            slot = next(str(index) + '.lock' for index in range(admission.MAXIMUM)
                        if (admission.ROOT / (str(index) + '.lock')).stat().st_ino == os.fstat(fd).st_ino)
            receipts = read_json(name).get('disposable', []) if name.exists() else []
            w.write_json(name, {'token': token, 'slot': slot, 'paths': paths, **({'disposable': receipts} if receipts else {})})
        finally:
            os.close(gate)
    return paths


def temporary(select):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with admission.reserve():
                paths = register(select(*args, **kwargs))
                try:
                    return function(*args, **kwargs)
                finally:
                    check_transfer(paths)
        return call
    return decorate


def copy_stream(source, output, maximum):
    count = 0
    while True:
        block = source.read(min(1024 * 1024, maximum - count + 1))
        if not block: return count
        if count + len(block) > maximum:
            raise ValueError('Incoming DCT archive exceeds the temporary transfer budget')
        output.write(block); count += len(block)


def capture_paths(identifier, node, folder, *args, **kwargs):
    return [folder]


def archive_paths(folder, archive):
    return [folder, archive]


def history_paths(identifier, folder, *args, **kwargs):
    return [folder]


def saved_images(containers):
    total = sum(max(0, row.get('SizeRootFs', row.get('SizeRw', 0))) for row in containers)
    if total > policy()['preserved_image_bytes']:
        raise ValueError('Saved container filesystems exceed the preserved-image budget')


def imported_images(containers):
    from . import projects as w
    ids = sorted({row['SnapshotImage'] for row in containers})
    rows = json.loads(w.capture(['docker', 'image', 'inspect', *ids])) if ids else []
    if sum(row['Size'] for row in rows) > policy()['preserved_image_bytes']:
        raise ValueError('Imported images exceed the preserved-image budget')


def unpack_paths(archive, folder, *args, **kwargs):
    return [archive, folder]


def transfer_paths(token, *args, **kwargs):
    from . import projects as w
    w.project_id(token)
    return [w.ROOT.parent / 'transfers' / token]


def container_logs(body):
    config = body.setdefault('HostConfig', {}).setdefault('LogConfig', {'Type': 'json-file', 'Config': {}})
    if config.get('Type', 'json-file') in ('json-file', 'local'):
        chosen = policy()
        options = config.setdefault('Config', {})
        options.setdefault('max-size', str(chosen['log_file_bytes']))
        options.setdefault('max-file', str(chosen['log_files']))
        logs(options)
