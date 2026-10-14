"""Container measurements and chart history."""
from datetime import datetime
from .secure_io import read_json
import json
import os
from pathlib import Path
import re
import subprocess
import time


def byte_size(value):
    match = re.fullmatch(r'\s*([0-9.]+)\s*([kmgtep]?i?b)?\s*', value, re.I)
    if not match:
        raise ValueError('Invalid Docker size measurement')
    unit = (match[2] or 'b').lower()
    power = 'bkmgtpe'.index(unit[0]) if unit[0] != 'b' else 0
    return float(match[1]) * (1024 if 'i' in unit else 1000) ** power


def metric_snapshot(containers, stats, services):
    active = [item for item in containers if item['state'] == 'running']
    pairs = lambda field: [tuple(worker.byte_size(part) for part in row[field].split('/')) for row in stats]
    memory, network, disk = pairs('MemUsage'), pairs('NetIO'), pairs('BlockIO')
    cores = sum(float(row['CPUPerc'].rstrip('%')) / 100 for row in stats)
    cpus = min(os.cpu_count() or 1, sum((item['settings']['cpus'] or (os.cpu_count() or 1)) * item.get('replicas', 1) for item in services)) or 1
    host_memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    assigned_memory = sum(item['settings']['memory'] * 1048576 * item.get('replicas', 1) for item in services)
    maxmem = min(host_memory, assigned_memory) if services and all(item['settings']['memory'] for item in services) else host_memory
    started = []
    for item in active:
        try:
            # Docker timestamps have nanosecond precision; datetime accepts it.
            started.append(datetime.fromisoformat(item['started'].replace('Z', '+00:00')).timestamp())
        except (ValueError, KeyError):
            pass
    return {'time': int(time.time()), 'cpu': cores / cpus, 'cpus': cpus,
            'mem': sum(pair[0] for pair in memory), 'maxmem': maxmem,
            'netin_total': sum(pair[0] for pair in network), 'netout_total': sum(pair[1] for pair in network),
            'diskread_total': sum(pair[0] for pair in disk), 'diskwrite_total': sum(pair[1] for pair in disk),
            'uptime': max(0, int(time.time() - min(started))) if started else 0,
            'instances': ','.join(sorted(item['id'] for item in active))}


def swap_usage(containers, services):
    try:
        match = re.search(r'^SwapTotal:\s+(\d+)\s+kB$', (worker.PROC / 'meminfo').read_text(), re.M)
        if not match:
            return {'swap': None, 'maxswap': None}
        host_swap = int(match[1]) * 1024
        active = [item for item in containers if item['state'] == 'running']
        used, capacity, seen = 0, 0, set()
        for item in active:
            pid = item.get('pid', 0)
            if not isinstance(pid, int) or pid <= 0:
                return {'swap': None, 'maxswap': None}
            groups = (worker.PROC / str(pid) / 'cgroup').read_text().splitlines()
            relative = next((line[3:] for line in groups if line.startswith('0::')), None)
            # A recycled PID must never attribute another workload's swap to this project.
            if not relative or item['id'] not in relative:
                return {'swap': None, 'maxswap': None}
            path = (worker.CGROUP / relative.lstrip('/')).resolve()
            if not path.is_relative_to(worker.CGROUP.resolve()):
                return {'swap': None, 'maxswap': None}
            if path in seen:
                continue
            seen.add(path)
            used += int((path / 'memory.swap.current').read_text())
            limit = (path / 'memory.swap.max').read_text().strip()
            capacity += host_swap if limit == 'max' else int(limit)
        if not active:
            unlimited = any(not item['settings']['memory'] or item['settings']['swap'] == -1 for item in services)
            capacity = host_swap if unlimited else sum(item['settings']['swap'] * 1048576 * item.get('replicas', 1) for item in services)
        return {'swap': used, 'maxswap': min(capacity, host_swap)}
    except (OSError, ValueError):
        return {'swap': None, 'maxswap': None}


def disk_usage(identifier, containers):
    path = worker.ROOT / identifier / 'disk-usage.json'
    ids = sorted(item['id'] for item in containers)
    try:
        cached = read_json(path) if path.exists() else {}
        if cached.get('ids') == ids and time.time() - cached.get('time', 0) < 60:
            return cached['usage']
    except (OSError, ValueError, KeyError):
        pass
    try:
        data_root = worker.capture(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip()
        if not data_root or not Path(data_root).is_absolute():
            raise ValueError('Invalid Docker storage directory')
        filesystem = os.statvfs(data_root)
        inspected = json.loads(worker.capture(['docker', 'inspect', '--type', 'container', '--size', *ids])) if ids else []
        if {item['Id'] for item in inspected} != set(ids) or any(not isinstance(item.get('SizeRootFs'), int) or item['SizeRootFs'] < 0 for item in inspected):
            raise ValueError('Docker root filesystem measurement unavailable')
        # Logical root filesystem sizes. Capacity is shared Docker storage, not a project quota.
        usage = {'disk': sum(item['SizeRootFs'] for item in inspected),
                 'maxdisk': filesystem.f_blocks * filesystem.f_frsize}
        worker.write_json(path, {'ids': ids, 'time': int(time.time()), 'usage': usage})
        os.chmod(path, 0o600)
        return usage
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'disk': None, 'maxdisk': None}


def record_metrics(info):
    directory = worker.ROOT / info['id']
    path = directory / 'metrics.json'
    history = read_json(path) if path.exists() else []
    snapshot = info['metrics']
    previous = history[-1] if history else None
    if previous and snapshot['time'] - previous['time'] < 60:
        return
    elapsed = snapshot['time'] - previous['time'] if previous else 0
    for key in ('netin', 'netout', 'diskread', 'diskwrite'):
        snapshot[key] = max(0, (snapshot[key + '_total'] - previous[key + '_total']) / elapsed) if (
            previous and elapsed > 0 and snapshot['instances'] == previous['instances']) else 0
    if info.get('vmid') and not info.get('template'):
        published = dict(snapshot)
        if previous and snapshot['instances'] != previous['instances']:
            for key in ('netin', 'netout', 'diskread', 'diskwrite'):
                published[key + '_total'] = None
        worker.run(['perl', str(worker.HELPERS / 'rrd.pl')],
                   input=json.dumps({'id': info['id'], 'action': 'update', 'metrics': published}), capture_output=True)
    # Only the previous sample is needed to detect counter resets. History lives in PVE RRD.
    worker.write_json(path, [snapshot])
    os.chmod(path, 0o600)


def rrddata(identifier, node, timeframe, cf):
    worker.detail(identifier, node)
    if timeframe not in ('hour', 'day', 'week', 'month', 'year', 'decade') or cf not in ('AVERAGE', 'MAX'):
        raise ValueError('Invalid chart range or consolidation function')
    return json.loads(worker.run(['perl', str(worker.HELPERS / 'rrd.pl')], capture_output=True,
                      input=json.dumps({'id': identifier, 'action': 'read', 'timeframe': timeframe, 'cf': cf})).stdout)


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
