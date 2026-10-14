"""Start preserved instances in Compose dependency order without recreating them."""
from .secure_io import read_json
import json
import sys
import time


def order(services, selected=None):
    result, visiting, done = [], set(), set()
    def visit(name):
        if name in visiting:
            raise ValueError('Circular Compose service dependency')
        if name in done:
            return
        if name not in services:
            raise ValueError('Unknown Compose dependency: ' + name)
        visiting.add(name)
        dependencies = dict(services[name].get('depends_on') or {})
        for value in services[name].get('volumes_from') or []:
            if not value.startswith('container:'):
                dependencies.setdefault(value.split(':')[0], {})
        for dependency, options in dependencies.items():
            if dependency not in services and not options.get('required', True):
                continue
            visit(dependency)
        visiting.remove(name)
        done.add(name)
        result.append(name)
    for name in selected or services:
        visit(name)
    return result


def start(identifier, selected=None, timeout=180):
    from . import projects as w
    from .hardening import check_start
    check_start(identifier)
    w.project_limits.preflight(identifier)
    config = read_json(w.ROOT / identifier / 'compose.runtime.json')
    services = config['services']
    ordered = order(services, [selected] if selected else None)
    records = w.container_records(identifier)
    ids = {name: [r['id'] for r in records if r['service'] == name] for name in services}
    if any(len(ids[name]) != services[name].get('deploy', {}).get('replicas', 1) for name in ordered):
        raise ValueError('Deploy the missing service instances before starting the DCT')
    deadline = time.monotonic() + timeout
    for name in ordered:
        for dependency, options in (services[name].get('depends_on') or {}).items():
            if dependency not in services and not options.get('required', True):
                continue
            condition = options.get('condition', 'service_started')
            if condition not in ('service_started', 'service_healthy', 'service_completed_successfully'):
                raise ValueError('Unsupported Compose dependency condition')
            while ids.get(dependency):
                rows = json.loads(w.capture(['docker', 'inspect', *ids[dependency]]))
                states = [row['State'] for row in rows]
                ready = all(state['Running'] for state in states) if condition == 'service_started' else (
                    all(state.get('Health', {}).get('Status') == 'healthy' for state in states) if condition == 'service_healthy' else
                    all(not state['Running'] and state['Status'] == 'exited' and state['ExitCode'] == 0 for state in states))
                if ready:
                    break
                failed = (condition == 'service_healthy' and any(state.get('Health', {}).get('Status') in (None, 'unhealthy') for state in states)) or (
                    condition == 'service_completed_successfully' and any(state['Status'] == 'exited' and state['ExitCode'] != 0 for state in states))
                if failed or time.monotonic() >= deadline:
                    message = ('Dependency is not ready: ' if failed else 'Timed out waiting for dependency ') + dependency
                    if options.get('required', True):
                        raise ValueError(message)
                    print('WARNING: ' + message, file=sys.stderr)
                    break
                time.sleep(0.2)
        active = {row['id'] for row in w.container_records(identifier) if row['state'] == 'running'}
        pending = [cid for cid in ids[name] if cid not in active]
        if pending:
            w.run(['docker', 'start', *pending], timeout=1800)
