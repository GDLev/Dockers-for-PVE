"""Node startup, Docker restart handling and guest hooks."""
from .secure_io import read_json
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import runtime as node_runtime


def parse_startup(value):
    if not isinstance(value, str):
        raise ValueError('Invalid startup order')
    result = {}
    for entry in value.split(','):
        if not entry.strip():
            continue
        match = re.fullmatch(r'(?:(order|up|down)=)?([0-9]+)', entry)
        if not match:
            raise ValueError('Invalid startup order')
        result[match[1] or 'order'] = int(match[2])
    return result


def access_state(identifier, node):
    info = worker.detail(identifier, node)
    directory = worker.ROOT / identifier
    snapshots = worker.snapshot_root(identifier)
    return {'status': info['status'], 'ipv4mode': info.get('ipv4mode', 'static'),
            'has_snapshots': snapshots.exists() and any(snapshots.iterdir()), 'protection': int(bool(info.get('protection'))),
            'template': bool(info.get('template')), 'migration': info.get('migration'),
            'replication': bool(info.get('replication')),
            'pending_network': worker.load_pending_network(directory),
            'pending_settings': bool(worker.load_pending(directory)),
            'operation': worker.operation_state(identifier),
            'active_containers': int(any(item['state'] in ('running', 'restarting', 'paused')
                                         for item in worker.container_records(identifier)))}


def lifecycle_config(identifier, node):
    info = worker.detail(identifier, node)
    return {'onboot': int(bool(info.get('onboot'))), 'startup': info.get('startup', ''), 'hookscript': info.get('hookscript', '')}


def restore_restart_policy(identifier, service=None):
    if (worker.ROOT / identifier / 'ha.json').exists():
        return
    marker = worker.ROOT.parent / 'restart-paused' / (identifier + '.json')
    if not marker.exists():
        return
    config = read_json(worker.ROOT / identifier / 'compose.runtime.json')
    for item in worker.container_records(identifier):
        if item['state'] == 'running' and (service is None or item['service'] == service):
            policy = config['services'][item['service']].get('restart', 'no')
            worker.run(['docker', 'update', '--restart=' + policy, item['id']], capture_output=True)


def daemon_prepare(node):
    # Record before changing Docker policies, so an interrupted stop can be retried.
    path = worker.ROOT.parent / 'lifecycle.json'
    with worker.locked():
        rows = []
        for info in worker.summaries():
            if info['node'] != node:
                continue
            records = worker.container_records(info['id'])
            if records:
                worker.write_json(worker.ROOT.parent / 'restart-paused' / (info['id'] + '.json'), {'paused': True})
            for item in records:
                host = json.loads(worker.capture(['docker', 'inspect', item['id']]))[0]['HostConfig']
                rows.append({'project': info['id'], 'id': item['id'], 'state': item['state'],
                             'policy': host['RestartPolicy']})
        previous = read_json(path) if path.exists() else {}
        saved = {row['id']: row for row in previous.get('containers', [])}
        for row in rows:
            if row['policy']['Name'] == 'no' and row['id'] in saved:
                row['policy'] = saved[row['id']]['policy']
        worker.write_json(path, {'boot_id': (worker.PROC / 'sys/kernel/random/boot_id').read_text().strip(), 'containers': rows})
        os.chmod(path, 0o600)
        if rows:
            worker.run(['docker', 'update', '--restart=no', *[row['id'] for row in rows]], capture_output=True)


def daemon_resume(node):
    path = worker.ROOT.parent / 'lifecycle.json'
    if not path.exists():
        return
    with worker.locked():
        if not path.exists():
            return
        state = read_json(path)
        if state['boot_id'] != (worker.PROC / 'sys/kernel/random/boot_id').read_text().strip():
            # PVE's startall owns boot ordering. Policies are restored by project Start.
            path.unlink()
            return
        owned = {item['id'] for info in worker.summaries() if info['node'] == node
                 for item in worker.container_records(info['id'])}
        from .hardening import check_profile
        for row in state['containers']:
            if row['id'] in owned and row['state'] in ('running', 'restarting', 'paused') and not (worker.ROOT / row['project'] / 'ha.json').exists():
                check_profile(row['project'])
                worker.project_limits.preflight(row['project'])
        projects = {row['project'] for row in state['containers'] if row['id'] in owned and row['state'] in ('running', 'restarting', 'paused') and not (worker.ROOT / row['project'] / 'ha.json').exists()}
        infos = {info['id']: info for info in worker.summaries() if info['node'] == node}
        for project in projects:
            worker.prepare_network_start(infos[project])
            worker.prepare_firewall(infos[project])
        for row in state['containers']:
            if (worker.ROOT / row['project'] / 'ha.json').exists():
                continue
            if row['id'] not in owned:
                continue
            if row['state'] in ('running', 'restarting', 'paused'):
                worker.run(['docker', 'start', row['id']], capture_output=True)
            policy = row['policy']['Name'] or 'no'
            if policy == 'on-failure' and row['policy'].get('MaximumRetryCount'):
                policy += ':' + str(row['policy']['MaximumRetryCount'])
            worker.run(['docker', 'update', '--restart=' + policy, row['id']], capture_output=True)
        try:
            for project in projects:
                worker.finish_network_start(infos[project])
        except Exception:
            for row in state['containers']:
                if row['id'] in owned and row['project'] in projects and row['state'] in ('running', 'restarting', 'paused'):
                    worker.run(['docker', 'stop', row['id']], capture_output=True)
            raise
        for project in {row['project'] for row in state['containers']}:
            (worker.ROOT.parent / 'restart-paused' / (project + '.json')).unlink(missing_ok=True)
        path.unlink()


def daemon_unmanage(node):
    with worker.locked():
        for info in worker.summaries():
            if info['node'] != node:
                continue
            config = read_json(worker.ROOT / info['id'] / 'compose.runtime.json')
            for item in worker.container_records(info['id']):
                policy = config['services'][item['service']].get('restart', 'no')
                worker.run(['docker', 'update', '--restart=' + policy, item['id']], capture_output=True)
            (worker.ROOT.parent / 'restart-paused' / (info['id'] + '.json')).unlink(missing_ok=True)
        (worker.ROOT.parent / 'lifecycle.json').unlink(missing_ok=True)


def graceful_shutdown(identifier, timeout=60, service=None):
    timeout = int(timeout)
    if not 0 <= timeout <= 86400:
        raise ValueError('Invalid shutdown timeout')
    def selected_records():
        return [item for item in worker.container_records(identifier) if service is None or item['service'] == service]

    records = selected_records()
    active_states = ('running', 'restarting', 'paused')
    for container in records:
        worker.check_shutdown_cancellation(identifier)
        if container['state'] not in active_states:
            continue
        try:
            worker.run(['docker', 'kill', '--signal', container.get('stop_signal') or 'SIGTERM', container['id']],
                capture_output=True, timeout=30)
        except subprocess.CalledProcessError:
            if any(item['id'] == container['id'] and item['state'] in active_states
                   for item in selected_records()):
                raise
    deadline = time.monotonic() + int(timeout)
    while any(item['state'] in active_states for item in selected_records()):
        worker.check_shutdown_cancellation(identifier)
        if time.monotonic() >= deadline:
            raise ValueError('Shutdown timed out. DCT remains running; use Stop to force termination.')
        time.sleep(0.2)
    return [item['id'] for item in records]


def pve_hook(script, vmid=None, phase=None):
    if not isinstance(script, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]*:snippets/[^\s:]+', script):
        raise ValueError('Hookscript must be a snippet volume ID')
    payload = {'action': 'run' if phase else 'check', 'hookscript': script}
    if phase:
        payload.update(vmid=vmid, phase=phase)
    try:
        result = worker.run(['perl', str(worker.HELPERS / 'hooks.pl')], input=json.dumps(payload),
                     capture_output=True, timeout=1800)
    except subprocess.CalledProcessError as error:
        raise ValueError('Hookscript: ' + (error.stderr or error.stdout or 'execution failed').strip()) from error
    if phase:
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            print('\n'.join(lines[:-1]))
        if result.stderr:
            print(result.stderr, file=sys.stderr, end='')
    return json.loads(result.stdout.strip().splitlines()[-1])


def hook_journal(identifier):
    return worker.ROOT.parent / 'hooks' / (worker.project_id(identifier) + '.json')


def sync_guest_runtime_state(info):
    states = [item['state'] for item in worker.container_records(info['id'])]
    info['status'] = 'running' if states and len(states) >= info.get('expected', 1) and all(state == 'running' for state in states) else (
        'partial' if 'running' in states else 'stopped')
    worker.write_json(worker.METADATA / (info['id'] + '.json'), {key: value for key, value in info.items() if key not in ('deployment', 'digest')})


def guest_hook(info, phase):
    script = info.get('hookscript')
    if not script:
        return
    path = worker.hook_journal(info['id'])
    # A killed hook may already have changed external state. Do not run it twice.
    if path.exists() and read_json(path).get('executing'):
        raise ValueError('A hook was interrupted. Inspect Task History, then set or remove the hookscript to acknowledge it.')
    worker.write_json(path, {'phase': phase, 'executing': True})
    try:
        worker.pve_hook(script, info['vmid'], phase)
    except Exception as error:
        path.unlink(missing_ok=True)
        if phase == 'pre-start':
            try:
                worker.sync_guest_runtime_state(info)
            except Exception as state_error:
                print(f'WARNING: could not update guest status: {state_error}', file=sys.stderr)
            raise
        print(f'WARNING: {phase} hook failed: {error}', file=sys.stderr)
    else:
        path.unlink(missing_ok=True)


def recover_guest_hook(info):
    if not info.get('hookscript'):
        return
    path = worker.hook_journal(info['id'])
    if not path.exists():
        return
    state = read_json(path)
    if state.get('executing'):
        worker.sync_guest_runtime_state(info)
        raise ValueError('A hook was interrupted. Inspect Task History, then set or remove the hookscript to acknowledge it.')
    running = any(item['state'] in ('running', 'restarting', 'paused') for item in worker.container_records(info['id']))
    phase = state['phase']
    if (phase == 'post-start' and running) or (phase == 'post-stop' and not running):
        worker.guest_hook(info, phase)
    path.unlink(missing_ok=True)


def expect_guest_hook(info, phase):
    if info.get('hookscript'):
        worker.write_json(worker.hook_journal(info['id']), {'phase': phase, 'executing': False})


# Projects exports these operations, so bind it after defining them.
from . import projects as worker
