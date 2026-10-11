"""Administrator-owned ceilings for the sum of all service and replica limits."""
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

KEYS = {'cpus', 'memory', 'pids'}


def validate(value):
    if not isinstance(value, dict) or set(value) - KEYS:
        raise ValueError('Project limits support cpus, memory (MiB) and pids')
    result = {}
    for key, maximum in (('cpus', 8192), ('memory', 1048576), ('pids', 4194304)):
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError('Invalid project ' + key + ' limit')
        number = Decimal(str(raw))
        if not number.is_finite() or not 0 <= number <= maximum or (key != 'cpus' and number != int(number)):
            raise ValueError('Invalid project ' + key + ' limit')
        result[key] = raw
    return result


def load(directory):
    path = Path(directory) / 'limits.json'
    return validate(json.loads(path.read_text())) if path.exists() else dict.fromkeys(KEYS, 0)


def memory_bytes(value):
    if isinstance(value, bool):
        raise ValueError('Invalid service memory limit')
    if isinstance(value, int):
        return value
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([bkmg]?)', str(value), re.I)
    if not match:
        raise ValueError('Invalid service memory limit')
    return int(Decimal(match[1]) * 1024 ** {'': 0, 'b': 0, 'k': 1, 'm': 2, 'g': 3}[match[2].lower()])


def check(config, ceilings):
    ceilings = validate(ceilings)
    totals = dict.fromkeys(KEYS, Decimal(0))
    for name, service in config.get('services', {}).items():
        deploy = service.get('deploy') or {}
        replicas = deploy.get('replicas', 1)
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
            raise ValueError('Invalid service replica count')
        if not replicas:
            continue
        declared = deploy.get('resources', {}).get('limits', {})
        quota = service.get('cpu_quota', 0)
        quota_cpus = quota / (service.get('cpu_period') or 100000) if quota > 0 else 0
        values = {'cpus': service.get('cpus', declared.get('cpus', quota_cpus)),
                  'memory': service.get('mem_limit', declared.get('memory', 0)),
                  'pids': service.get('pids_limit', declared.get('pids', 0))}
        for key, ceiling in ceilings.items():
            if not ceiling:
                continue
            raw = values[key]
            try:
                amount = Decimal(memory_bytes(raw)) / 1048576 if key == 'memory' else Decimal(str(raw))
            except (InvalidOperation, TypeError):
                raise ValueError('Invalid service ' + key + ' limit') from None
            if not amount.is_finite() or amount <= 0:
                raise ValueError(name + ': set a finite ' + key + ' limit before applying a project budget')
            totals[key] += amount * replicas
            if totals[key] > Decimal(str(ceiling)):
                raise ValueError('Project ' + key + ' budget exceeded by services and replicas')
    return {key: float(value) for key, value in totals.items()}


def check_directory(directory, config=None):
    ceilings = load(directory)
    if not any(ceilings.values()):
        return
    if config is None:
        config = json.loads((Path(directory) / 'compose.runtime.json').read_text())
    return check(config, ceilings)


def check_containers(containers, ceilings):
    services = {}
    for index, container in enumerate(containers):
        host = container.get('HostConfig') or {}
        cpus = host.get('NanoCpus', 0) / 1000000000
        if not cpus and host.get('CpuQuota', 0) > 0:
            cpus = host['CpuQuota'] / (host.get('CpuPeriod') or 100000)
        services[str(index)] = {'cpus': cpus, 'mem_limit': host.get('Memory', 0), 'pids_limit': host.get('PidsLimit') or 0}
    return check({'services': services}, ceilings)
