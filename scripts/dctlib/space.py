"""Peak-space plans and atomic node-local reservations."""
import contextlib
import contextvars
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .secure_io import anchored, read_json
from . import operation_capacity as admission, retention

POLICY = Path('/etc/betterdocker/space-policy.json')
ROOT = Path('/var/lib/betterdocker/space-reservations')
_current = contextvars.ContextVar('betterdocker_space_plan', default=None)


def policy():
    value = read_json(POLICY) if POLICY.exists() else {'reserve_bytes': 1024 ** 3, 'overhead_percent': 10}
    if not isinstance(value, dict) or set(value) != {'reserve_bytes', 'overhead_percent'} or any(type(n) is not int for n in value.values()) or not 0 <= value['reserve_bytes'] <= 1024 ** 5 or not 0 <= value['overhead_percent'] <= 100:
        raise ValueError('Invalid peak-space policy')
    return value


def number(value):
    if type(value) is not int or not 0 <= value <= 1024 ** 5:
        raise ValueError('Cannot measure peak storage requirements')
    return value


def filesystem(path, required):
    path = Path(path).absolute()
    while not path.exists():
        if path.parent == path: raise ValueError('Cannot find destination filesystem')
        path = path.parent
    return {'key': 'fs:' + str(path.stat().st_dev), 'free': number(shutil.disk_usage(path).free),
            'bytes': number(required), 'name': 'destination filesystem'}


def docker_root():
    from . import projects as w
    path = w.capture(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip()
    if not path or not Path(path).is_absolute(): raise ValueError('Cannot determine Docker storage capacity')
    return Path(path)


def storage(sid, required):
    from . import projects as w
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', sid): raise ValueError('Invalid storage identifier')
    code = '''use PVE::Storage;use JSON;my $c=PVE::Storage::config();my $s=PVE::Storage::storage_config($c,$ARGV[0]);PVE::Storage::storage_check_enabled($c,$ARGV[0]);my $i=PVE::Storage::storage_info($c,'rootdir')->{$ARGV[0]};die "Storage unavailable\\n" unless $i && $i->{active}; print encode_json({%$i,map {$_=>$s->{$_}} qw(type path vgname thinpool pool)});'''
    info = json.loads(w.capture(['perl', '-e', code, sid]))
    if info['type'] in ('dir', 'nfs', 'cifs', 'cephfs', 'btrfs'):
        if not info.get('path') or not Path(info['path']).is_dir(): raise ValueError('Destination storage is not mounted')
        target = filesystem(info['path'], required)
        target['free'] = min(target['free'], number(info['avail']))
    else:
        if info['type'] == 'zfspool' and info.get('pool'):
            info['pool'] = info['pool'].split('/', 1)[0]
        identity = '/'.join(str(info.get(k) or '') for k in ('type', 'vgname', 'thinpool', 'pool'))
        if identity == info['type'] + '///': identity += sid
        target = {'key': 'pool:' + identity, 'free': number(info['avail']), 'bytes': number(required)}
    target['name'] = 'PVE storage ' + sid
    return target


def group(targets):
    result = {}
    for item in targets:
        key = item['key']
        current = result.setdefault(key, dict(item, bytes=0))
        current['bytes'] += number(item['bytes'])
        current['free'] = min(current['free'], number(item['free']))
        number(current['bytes'])
    return result


def process_start(pid):
    from . import projects as w
    return (w.PROC / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19]


def slot_owner():
    fd = admission.descriptor()
    if fd is None: return None
    slot = next(str(i) + '.lock' for i in range(admission.MAXIMUM) if (admission.ROOT / (str(i) + '.lock')).stat().st_ino == os.fstat(fd).st_ino)
    return {'kind': 'slot', 'slot': slot, 'token': admission.identity()}


def owner(identifier=None):
    from . import projects as w
    if identifier:
        state = w.operation_state(identifier)
        if state.get('active'):
            return {'kind': 'guest', 'guest': identifier, 'pid': state['pid'], 'start': state['process_start'], 'borrowers': [slot_owner()] if admission.descriptor() is not None else []}
    if admission.descriptor() is not None:
        return slot_owner()
    return {'kind': 'process', 'pid': os.getpid(), 'start': process_start(os.getpid())}


def active(value):
    from . import projects as w
    if any(retention.active(borrower) for borrower in value.get('borrowers', [])): return True
    if value['kind'] == 'slot': return retention.active(value)
    if value['kind'] == 'guest':
        state = w.operation_state(value['guest'])
        return bool(state.get('active') and state.get('pid') == value['pid'] and state.get('process_start') == value['start'])
    try: return process_start(value['pid']) == value['start']
    except (OSError, IndexError): return False


@contextlib.contextmanager
def reserve(targets, identifier=None, key=None):
    from . import projects as w
    proposed = group(targets)
    chosen = policy()
    loan = _current.get()
    who = owner(identifier)
    identity = {field: value for field, value in who.items() if field != 'borrowers'}
    key = loan or key or hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{32,64}', key): raise ValueError('Invalid peak-space reservation key')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with anchored(ROOT, directory=True) as directory:
        gate = admission.open_lock(directory, 'allocation.lock')
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            file = ROOT / (key + '.json')
            own = read_json(file) if file.exists() else None
            if own and not active(own['owner']): own = None
            claims = dict(own['claims']) if own else {}
            pending = {}
            for source in ROOT.glob('*.json'):
                if source == file: continue
                saved = read_json(source)
                if not active(saved['owner']): source.unlink(); continue
                for device, amount in saved['claims'].items(): pending[device] = pending.get(device, 0) + number(amount)
            for device, item in proposed.items():
                amount = number(item['bytes'])
                required = amount + (amount * chosen['overhead_percent'] + 99) // 100 + chosen['reserve_bytes']
                if own and claims.get(device, 0) >= required:
                    continue
                if required + pending.get(device, 0) > item['free']:
                    raise ValueError('Insufficient peak space on ' + item['name'] + '; free capacity is reserved or below the required safety margin')
                claims[device] = required
            holder = own['owner'] if own else who
            borrowers = [entry for entry in holder.get('borrowers', []) if retention.active(entry)]
            for entry in who.get('borrowers', []):
                if entry not in borrowers: borrowers.append(entry)
            if borrowers or 'borrowers' in holder: holder['borrowers'] = borrowers
            w.write_json(file, {'owner': holder, 'claims': claims})
        finally: os.close(gate)
    token = _current.set(key)
    try: yield
    finally: _current.reset(token)


def sweep():
    removed = 0
    if not ROOT.exists(): return removed
    with anchored(ROOT, directory=True) as directory:
        gate = admission.open_lock(directory, 'allocation.lock')
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            for file in ROOT.glob('*.json'):
                if not active(read_json(file)['owner']):
                    file.unlink(); removed += 1
        finally:
            os.close(gate)
    return removed


@contextlib.contextmanager
def transfer_loan(token):
    file = ROOT / (token + '.json')
    loan = None
    if ROOT.exists():
        with anchored(ROOT, directory=True) as directory:
            gate = admission.open_lock(directory, 'allocation.lock')
            try:
                fcntl.flock(gate, fcntl.LOCK_EX)
                saved = read_json(file) if file.exists() else None
                if saved and active(saved['owner']):
                    borrower = slot_owner()
                    if borrower:
                        from . import projects as w
                        saved['owner']['borrowers'] = [entry for entry in saved['owner'].get('borrowers', []) if retention.active(entry)]
                        if borrower not in saved['owner']['borrowers']: saved['owner']['borrowers'].append(borrower)
                        w.write_json(file, saved)
                    loan = token
            finally:
                os.close(gate)
    current = _current.set(loan)
    try: yield
    finally: _current.reset(current)


def measured_mounts(mounts):
    from . import projects as w
    return sum(number(int(w.capture(['du', '-sx', '-B1', entry['source']]).split()[0])) for entry in mounts)


def export_size(identifier, history=False):
    from . import projects as w
    import guest_state
    rows = w.snapshot_containers(identifier, allow_running=True)
    image = sum(number(row.get('SizeRootFs')) for row in rows)
    from .images import saved_images
    extra = sorted(set(saved_images(w.ROOT / identifier)) - {row.get('Image') for row in rows})
    if extra:
        image += sum(number(row['Size']) for row in json.loads(w.capture(['docker', 'image', 'inspect', *extra])))
    data = measured_mounts(guest_state.owned_mounts(identifier, rows))
    size = image + data + 1024 * 1024
    if history:
        for file in w.snapshot_root(identifier).glob('*/manifest.json'):
            size += retention.tree_bytes(file.parent)
            if read_json(file).get('native_storage'):
                size += number(read_json(file.parent / 'configuration/storage.json')['size_mib']) * 1048576
    return {'state_bytes': number(size), 'image_bytes': number(image), 'disks': list(disks(identifier).values())}


def disks(identifier):
    from . import projects as w
    from .disks import records
    root = w.storage_config(identifier)
    return {key: {'storage': disk.get('storage', ''), 'bytes': number(disk['size_mib']) * 1048576}
            for key, disk in dict(records(root), root=root).items() if disk.get('size_mib')}


def source_targets(identifier, folder, history=False):
    spec = export_size(identifier, history)
    return [filesystem(folder, spec['state_bytes'] * 2), filesystem(docker_root(), spec['image_bytes'])]


def destination_targets(token, spec, sid=''):
    from . import projects as w
    if not isinstance(spec, dict) or set(spec) != {'state_bytes', 'image_bytes', 'disks'} or not isinstance(spec['disks'], list): raise ValueError('Invalid destination peak-space plan')
    targets = [filesystem(w.ROOT.parent / 'transfers' / token, number(spec['state_bytes']) * 2),
               filesystem(docker_root(), number(spec['image_bytes']) * 2)]
    for disk in spec['disks']:
        if not isinstance(disk, dict) or set(disk) != {'storage', 'bytes'}: raise ValueError('Invalid destination disk plan')
        if sid or disk['storage']: targets.append(storage(sid or disk['storage'], number(disk['bytes'])))
        else: targets.append(filesystem(w.ROOT, number(disk['bytes'])))
    return targets


def claim(targets, identifier=None):
    with reserve(targets, identifier): pass


def replacement(identifier, entries, candidate):
    from . import projects as w
    writable = sum(number(entry['source'].get('SizeRw')) for entry in entries if entry.get('source') and entry.get('body') and entry.get('preserve', True))
    target = filesystem(docker_root(), writable * 2 + 1024 * 1024)
    claim([target, filesystem(w.ROOT / identifier, 1024 * 1024)], identifier)


def snapshot(identifier):
    from . import projects as w
    rows = w.snapshot_containers(identifier, allow_running=True)
    images = sum(number(row.get('SizeRootFs')) for row in rows)
    mounts = w.snapshot_mounts(identifier, rows)
    managed = w.storage_config(identifier)
    point = w.ROOT / identifier / 'storage-data'
    mounts = [entry for entry in mounts if not (managed.get('native_snapshots') and Path(entry['source']) == point)]
    targets = [filesystem(w.snapshot_root(identifier), images + measured_mounts(mounts) + 1024 * 1024), filesystem(docker_root(), images)]
    if managed.get('native_snapshots'): targets.append(storage(managed['storage'], managed['size_mib'] * 1048576))
    claim(targets, identifier)


def restore(identifier, folder, sid='', existing=False):
    from . import projects as w
    folder = Path(folder)
    rows = read_json(folder / 'containers.json')
    images = sum(number(row.get('SizeRootFs')) for row in rows)
    archive = folder / 'images.tar'
    if not archive.is_file(): raise ValueError('Cannot measure saved images before restore')
    images = max(images, archive.stat().st_size)
    targets = [filesystem(docker_root(), images * 2), filesystem(w.ROOT, retention.tree_bytes(folder) + 1024 * 1024)]
    saved = folder / 'configuration/storage.json'
    if saved.exists() and not existing:
        disk = read_json(saved)
        from .disks import records
        for data in [disk, *records(disk).values()]:
            if sid or data.get('storage'): targets.append(storage(sid or data['storage'], number(data['size_mib']) * 1048576))
            else: targets.append(filesystem(w.ROOT, number(data['size_mib']) * 1048576))
    claim(targets, identifier)


def data_disk(identifier, sid, required):
    from . import projects as w
    rows = w.snapshot_containers(identifier, allow_running=True)
    writable = sum(number(row.get('SizeRw')) for row in rows)
    claim([storage(sid, required), filesystem(docker_root(), writable * 2 + 1024 * 1024)], identifier)


def loaned_transfer(function):
    @wraps(function)
    def call(token, *args, **kwargs):
        from . import projects as w
        w.project_id(token)
        with transfer_loan(token):
            return function(token, *args, **kwargs)
    return call
