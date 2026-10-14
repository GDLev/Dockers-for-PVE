"""Administrator-owned ceilings for the sum of all service and replica limits."""
from .secure_io import read_json
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import copy

KEYS = {'cpus', 'memory', 'pids'}
COUNTS = {'services', 'replicas'}
POLICY = Path('/etc/betterdocker/resource-policy.json')
DEFAULT_FILE = 'resource-defaults.json'
DEFAULTS = {'container': {'cpus': 1, 'memory': 512, 'pids': 128},
            'project': {'cpus': 4, 'memory': 4096, 'pids': 1024, 'services': 8, 'replicas': 16}}


def validate(value):
    if not isinstance(value, dict) or set(value) - (KEYS | COUNTS | {'exempt'}):
        raise ValueError('Invalid project limit fields')
    result = {}
    for key, maximum in (('cpus', 8192), ('memory', 1048576), ('pids', 4194304)):
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError('Invalid project ' + key + ' limit')
        number = Decimal(str(raw))
        if not number.is_finite() or not 0 <= number <= maximum or (key != 'cpus' and number != int(number)):
            raise ValueError('Invalid project ' + key + ' limit')
        result[key] = raw
    for key in COUNTS & value.keys():
        if type(value[key]) is not int or not 0 <= value[key] <= 100000:
            raise ValueError('Invalid project ' + key + ' limit')
        result[key] = value[key]
    if 'exempt' in value:
        if type(value['exempt']) is not bool:
            raise ValueError('Project limit exemption must be a boolean')
        result['exempt'] = value['exempt']
    return result


def policy():
    value = read_json(POLICY) if POLICY.exists() else copy.deepcopy(DEFAULTS)
    if not isinstance(value, dict) or set(value) != {'container', 'project'}:
        raise ValueError('Invalid node resource policy')
    container = validate(value['container'])
    project = validate(value['project'])
    if set(container) != KEYS or set(project) != KEYS | COUNTS or any(number <= 0 for number in (*container.values(), *project.values())):
        raise ValueError('Node resource policy requires finite positive defaults and ceilings')
    if any(container[key] > project[key] for key in KEYS):
        raise ValueError('Container defaults exceed the project resource ceilings')
    return {'container': container, 'project': project}


def initialize(directory):
    from . import projects as w
    if w.trusted_management():
        return
    chosen = policy()
    w.write_json(Path(directory) / 'limits.json', chosen['project'])
    w.write_json(Path(directory) / DEFAULT_FILE, chosen['container'])


def apply_defaults(config, directory, creating=False):
    from . import projects as w
    path = Path(directory) / DEFAULT_FILE
    if load(directory).get('exempt'):
        return config
    if path.exists():
        defaults = validate(read_json(path))
        if set(defaults) != KEYS or any(number <= 0 for number in defaults.values()):
            raise ValueError('Invalid saved container resource defaults')
    elif creating and not w.trusted_management():
        defaults = policy()['container']
    else:
        return config
    for service in config['services'].values():
        declared = service.get('deploy', {}).get('resources', {}).get('limits', {})
        if 'cpus' not in service and 'cpus' not in declared and 'cpu_quota' not in service:
            service['cpus'] = defaults['cpus']
        if 'mem_limit' not in service and 'memory' not in declared:
            service['mem_limit'] = defaults['memory'] * 1048576
        if 'pids_limit' not in service and 'pids' not in declared:
            service['pids_limit'] = defaults['pids']
    return config


def load(directory):
    path = Path(directory) / 'limits.json'
    return validate(read_json(path)) if path.exists() else dict.fromkeys(KEYS, 0)


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
    if ceilings.get('exempt'):
        return {key: 0.0 for key in KEYS}
    if ceilings.get('services') and len(config.get('services', {})) > ceilings['services']:
        raise ValueError('Project service count budget exceeded')
    instances = 0
    for name, service in config.get('services', {}).items():
        deploy = service.get('deploy') or {}
        replicas = deploy.get('replicas', 1)
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
            raise ValueError('Invalid service replica count')
        instances += replicas
        if ceilings.get('replicas') and instances > ceilings['replicas']:
            raise ValueError('Project replica count budget exceeded')
        if not replicas:
            continue
        declared = deploy.get('resources', {}).get('limits', {})
        quota = service.get('cpu_quota', 0)
        quota_cpus = quota / (service.get('cpu_period') or 100000) if quota > 0 else 0
        values = {'cpus': service.get('cpus', declared.get('cpus', quota_cpus)),
                  'memory': service.get('mem_limit', declared.get('memory', 0)),
                  'pids': service.get('pids_limit', declared.get('pids', 0))}
        for key in KEYS:
            ceiling = ceilings[key]
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


def check_directory(directory, config=None, creating=False):
    ceilings = load(directory)
    if creating and not (Path(directory) / 'limits.json').exists():
        from . import projects as w
        if not w.trusted_management():
            ceilings = policy()['project']
    if not any(ceilings.values()):
        return
    if config is None:
        config = read_json(Path(directory) / 'compose.runtime.json')
    return check(config, ceilings)


def check_containers(containers, ceilings, config=None):
    ceilings = validate(ceilings)
    if ceilings.get('exempt') or not any(ceilings.values()):
        return
    if config is not None:
        names = [(row.get('Config', {}).get('Labels') or {}).get('com.docker.compose.service') for row in containers]
        if any(name not in config['services'] for name in names):
            raise ValueError('Container service does not match the budgeted configuration')
        if ceilings.get('services') and len(set(names)) > ceilings['services']:
            raise ValueError('Project service count budget exceeded')
    services = {}
    for index, container in enumerate(containers):
        host = container.get('HostConfig') or {}
        cpus = host.get('NanoCpus', 0) / 1000000000
        if not cpus and host.get('CpuQuota', 0) > 0:
            cpus = host['CpuQuota'] / (host.get('CpuPeriod') or 100000)
        services[str(index)] = {'cpus': cpus, 'mem_limit': host.get('Memory', 0), 'pids_limit': host.get('PidsLimit') or 0}
    # Engine rows represent replicas. Service count is checked against Compose.
    return check({'services': services}, {key: value for key, value in ceilings.items() if key != 'services'})


def inspect_live(identifier):
    from . import projects as w
    records = w.container_records(identifier)
    return json.loads(w.capture(['docker', 'inspect', *[row['id'] for row in records]])) if records else []


def pending_config(config, directory):
    from . import projects as w
    queued = copy.deepcopy(config)
    changes = {}
    for name, pending in w.load_pending(directory).items():
        if name not in queued['services']:
            raise ValueError('Unknown Compose service in pending settings')
        service = copy.deepcopy(queued['services'][name])
        declared = service.get('deploy', {}).get('resources', {}).get('limits', {})
        service['mem_limit'] = memory_bytes(service.get('mem_limit', declared.get('memory', 0)))
        if 'cpus' not in service and 'cpus' not in declared and service.get('cpu_quota', 0) > 0:
            service['cpus'] = service['cpu_quota'] / (service.get('cpu_period') or 100000)
        changes[name] = dict(w.service_values(service), **pending)
    w.apply_settings(queued, changes)
    return queued


def preflight(identifier, config=None, entries=None, service=None):
    """Check stored, queued and actual/proposed Engine caps before mutations."""
    from . import projects as w
    directory = w.ROOT / identifier
    ceilings = load(directory)
    if ceilings.get('exempt') or not any(ceilings.values()):
        return
    config = config if config is not None else read_json(directory / 'compose.runtime.json')
    check(config, ceilings)
    queued = pending_config(config, directory)
    check(queued, ceilings)
    rows = inspect_live(identifier)
    if entries is not None:
        replaced = {entry['source']['Id'] for entry in entries if entry.get('source')}
        rows = [row for row in rows if row['Id'] not in replaced]
        rows.extend({'Config': entry['body'], 'HostConfig': entry['body'].get('HostConfig', {})}
                    for entry in entries if entry.get('body'))
    elif service is not None:
        # Hotplug changes these instances to the proposed caps. Other services
        # keep their actual caps, even when configuration drifted externally.
        remaining = [row for row in rows if (row.get('Config', {}).get('Labels') or {}).get('com.docker.compose.service') != service]
        count = len(rows) - len(remaining)
        proposed = copy.deepcopy(config['services'][service])
        proposed.setdefault('deploy', {})['replicas'] = count
        totals = check({'services': {service: proposed}}, ceilings)
        actual = check_containers(remaining, ceilings, config) or dict.fromkeys(KEYS, 0)
        if ceilings.get('replicas') and len(rows) > ceilings['replicas']:
            raise ValueError('Project replica count budget exceeded')
        if any(ceilings[key] and Decimal(str(actual[key])) + Decimal(str(totals[key])) > Decimal(str(ceilings[key])) for key in KEYS):
            raise ValueError('Project resource budget exceeded by live containers and proposed settings')
        return
    return check_containers(rows, ceilings, config)


def import_policy(directory, config, containers, new_guest):
    """Inherited administrator exceptions do not authorize a new delegated guest."""
    from . import projects as w
    ceilings = load(directory)
    defaults_path = Path(directory) / DEFAULT_FILE
    defaults = read_json(defaults_path) if defaults_path.exists() else None
    if new_guest and not w.trusted_management():
        chosen = policy()
        ceilings = {key: min(ceilings.get(key) or maximum, maximum) for key, maximum in chosen['project'].items()}
        defaults = chosen['container']
    check(config, ceilings)
    check_containers(containers, ceilings, config)
    queued = pending_config(config, directory)
    check(queued, ceilings)
    return ceilings, defaults
