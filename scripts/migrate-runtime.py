#!/usr/bin/env python3
"""Explicit offline node runtime conversion with retained source data and rollback."""
from dctlib.secure_io import read_json, read_text
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import guest_state
import host
from dctlib import projects as worker
import runtime
import runtime_install
import security_install
import transfer

ROOT = Path('/var/lib/betterdocker/runtime-migrations')
PROFILE = Path('/var/lib/betterdocker/installer/source/assets/betterdocker.apparmor')
SERVICES = ('betterdocker-refresh.timer', 'betterdocker-integration.timer',
            'betterdocker-firewall.service', 'betterdocker-lifecycle.service')


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def save(path, value):
    worker.write_json(path, value)
    os.chmod(path, 0o600)


def rootful_profile():
    try:
        read_text(PROFILE, maximum=1024 * 1024)
    except (OSError, ValueError) as error:
        raise ValueError('Rootful conversion requires the installed security profile; repair installation first') from error
    return PROFILE


def inventory(target):
    source = runtime.load()
    if target == source.mode:
        raise ValueError('Node already uses the requested runtime')
    infos = [info for info in worker.summaries() if info['node'] == worker.current_node()]
    blockers = []
    try:
        runtime_install.preflight(target)
        if target == 'rootful':
            rootful_profile()
        destination = Path(runtime_install.HOME) / 'docker'
        if target == 'rootful':
            config = Path('/etc/docker/daemon.json')
            destination = Path(read_json(config).get('data-root', '/var/lib/docker')) if config.exists() else Path('/var/lib/docker')
        if destination.is_symlink():
            raise ValueError('Destination runtime data directory cannot be a symbolic link')
        containers = destination / 'containers'
        if containers.exists() and any(containers.iterdir()):
            raise ValueError('Destination runtime is not empty; use the original conversion journal to roll back, or prepare an empty destination')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        blockers.append(str(error))
    owned = set()
    for info in infos:
        identifier = worker.project_id(info['id'])
        if info.get('ipv4mode', 'static') != 'static':
            blockers.append(str(info['vmid']) + ': offline conversion currently requires a static endpoint')
        records = worker.container_records(identifier)
        owned.update(c['id'] for c in records)
        if any(c['state'] in ('running', 'restarting', 'paused') for c in records):
            blockers.append(str(info['vmid']) + ': stop all service instances')
        if info.get('ha_recovery') or (worker.ROOT / identifier / 'ha.json').exists():
            blockers.append(str(info['vmid']) + ': remove HA registration')
        if info.get('replication') or (worker.ROOT / identifier / 'replication.json').exists():
            blockers.append(str(info['vmid']) + ': remove replication jobs and their state disk')
        if info.get('migration'):
            blockers.append(str(info['vmid']) + ': node migration is in progress')
        if worker.snapshot_root(identifier).exists() and any(worker.snapshot_root(identifier).iterdir()):
            blockers.append(str(info['vmid']) + ': snapshot history conversion is unsupported')
        try:
            containers = worker.snapshot_containers(identifier)
            if not containers:
                raise ValueError('deploy all instances first')
            guest_state.owned_mounts(identifier, containers)
            config = read_json(worker.ROOT / identifier / 'compose.runtime.json')
            guest_state.check_portable_networks(config)
            for service in config['services'].values():
                if target == 'rootless' and service.get('storage_opt', {}).get('size'):
                    raise ValueError('remove unsupported writable-layer quota')
                for mount in service.get('volumes', []):
                    if mount.get('type') == 'bind':
                        worker.safe_bind(mount['source'], identifier)
        except (ValueError, OSError) as error:
            blockers.append(str(info['vmid']) + ': ' + str(error))
    existing = set(worker.capture(['docker', 'ps', '--no-trunc', '-aq']).split())
    retained = []
    for identifier in existing - owned:
        container = json.loads(worker.capture(['docker', 'inspect', identifier]))[0]
        labels = container['Config'].get('Labels') or {}
        try:
            project = worker.project_id(labels.get('betterdocker.id', ''))
        except ValueError:
            continue
        metadata = worker.METADATA / (project + '.json')
        control = worker.ROOT / project / 'request.json'
        if labels.get('com.docker.compose.project') == 'bd-' + project and metadata.is_file() and control.is_file():
            info = read_json(metadata)
            if info['node'] != worker.current_node() and container['State']['Status'] not in ('running', 'restarting', 'paused'):
                retained.append(identifier)
    if existing - owned - set(retained):
        blockers.append('Unrelated Docker containers are present; conversion does not move them')
    return {'source': source.mode, 'target': target,
            'guests': [{'vmid': i['vmid'], 'name': i['name'], 'id': i['id']} for i in infos],
            'retained_containers': retained, 'blockers': blockers,
            'downtime': 'All DCTs on this node stay stopped during conversion and acceptance'}


def pause_maintenance():
    active = [unit for unit in SERVICES if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0]
    for unit in active:
        command(['systemctl', 'stop', unit])
    return active


def resume_maintenance(active):
    for unit in active:
        command(['systemctl', 'start', unit])


def select_target(mode):
    current = runtime.load()
    if mode == 'rootful':
        security_install.initialize(mode, rootful_profile())
    if current.mode == mode:
        runtime_install.initialize(mode)
        if mode == 'rootful':
            command(['systemctl', 'start', 'docker.service'])
        return
    if not current.rootless:
        command(['systemctl', 'disable', '--now', 'docker.socket'])
        command(['systemctl', 'disable', 'docker.service'])
    result = runtime.service('stop')
    if result.returncode:
        raise ValueError('Cannot stop the source Docker runtime')
    if current.rootless:
        Path(current.home + '/.config/systemd/user/default.target.wants/docker.service').unlink(missing_ok=True)
        command(['systemctl', 'disable', '--now', 'betterdocker-rootless.service'])
        Path('/etc/systemd/system/betterdocker-lifecycle.service.d/runtime.conf').unlink(missing_ok=True)
        host.remove()
    runtime_install.initialize(mode, conversion=True)
    if mode == 'rootful':
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'disable', 'betterdocker-lifecycle.service'])
        command(['systemctl', 'enable', 'betterdocker-lifecycle.service'])
        command(['systemctl', 'enable', '--now', 'docker.service', 'docker.socket'])


def convert(target, resume=None):
    if resume:
        folder = ROOT / worker.project_id(resume)
        value = read_json(folder / 'journal.json')
        if value['target'] != target or value['phase'] in ('rollback', 'rolled-back'):
            raise ValueError('Migration journal does not describe a pending conversion to this mode')
        if value['phase'] == 'complete':
            resume_maintenance(value['active'])
            print(json.dumps({'journal': folder.name, 'phase': 'complete'}))
            return
    else:
        report = inventory(target)
        if report['blockers']:
            raise ValueError('\n'.join(report['blockers']))
        folder = ROOT / uuid.uuid4().hex
        folder.mkdir(parents=True, mode=0o700)
        value = dict(report, phase='backup', saved=[], restored=[], active=[],
                     runtime_config=runtime.CONFIG.read_text() if runtime.CONFIG.exists() else None,
                     source_enabled={unit: subprocess.run(['systemctl', 'is-enabled', '--quiet', unit]).returncode == 0
                                     for unit in ('docker.service', 'docker.socket')})
        value['active'] = [unit for unit in SERVICES if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0]
        save(folder / 'journal.json', value)
    with contextlib.ExitStack() as locks:
        for guest in value['guests']:
            handle = locks.enter_context(worker.operation(guest['id'], 'runtime-conversion', wait_timeout=300))
            guest['lock_fd'] = handle.fileno()
        if value['phase'] == 'backup':
            for guest in value['guests']:
                identifier = guest['id']
                destination = folder / identifier
                if not (destination / 'state.tar').exists():
                    if (destination / 'state').exists():
                        shutil.rmtree(destination / 'state')
                    destination.mkdir(exist_ok=True, mode=0o700)
                    guest_state.capture_state(identifier, worker.current_node(), destination / 'state', lock_fd=guest['lock_fd'])
                    guest_state.pack_state(destination / 'state', destination / 'state.tar')
                guest_state.check_state(destination / 'state')
                info = read_json(worker.METADATA / (identifier + '.json'))
                save(destination / 'metadata.json', info)
                if info.get('ipv4mode', 'static') != 'static':
                    raise ValueError('Offline runtime conversion currently requires static DCT endpoints')
            pause_maintenance()
            value['phase'] = 'detach'
            save(folder / 'journal.json', value)
        if value['phase'] == 'detach':
            for guest in value['guests']:
                identifier = guest['id']
                destination = folder / identifier
                project = worker.ROOT / identifier
                if not (destination / 'original').exists():
                    worker.remove_network(read_json(destination / 'metadata.json'))
                    if worker.storage_config(identifier):
                        unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(project / 'storage-data')]).strip()
                        unit_path = Path('/etc/systemd/system') / unit
                        if unit_path.exists():
                            (destination / 'mount-unit').write_text(unit_path.read_text())
                        worker.storage_unmount(identifier)
                    project.rename(destination / 'original')
                (worker.METADATA / (identifier + '.json')).unlink(missing_ok=True)
                if identifier not in value['saved']:
                    value['saved'].append(identifier)
                    save(folder / 'journal.json', value)
            value['phase'] = 'switch'
            save(folder / 'journal.json', value)
        if value['phase'] == 'switch':
            select_target(value['target'])
            if worker.capture(['docker', 'ps', '-aq']).strip():
                raise ValueError('Destination runtime is not empty; retained containers were not overwritten')
            value['phase'] = 'restore'
            save(folder / 'journal.json', value)
        if value['phase'] == 'restore':
            for guest in value['guests']:
                identifier = guest['id']
                destination = folder / identifier
                if identifier not in value['restored']:
                    original = read_json(destination / 'metadata.json')
                    path = worker.METADATA / (identifier + '.json')
                    project = worker.ROOT / identifier
                    completed = path.exists() and (project / 'snapshot-restored.json').exists() and not (worker.ROOT.parent / 'imports' / (identifier + '.json')).exists()
                    if completed:
                        restored = read_json(path)
                        if restored.get('vmid') != original['vmid'] or restored.get('runtime_mode') != target or restored.get('status') != 'stopped':
                            raise ValueError('Interrupted conversion found an unexpected destination guest')
                    else:
                        if project.exists():
                            guest_state.cleanup_import(identifier, keep_reservation=True)
                        restored = guest_state.restore_state(destination / 'state', identifier, worker.current_node(),
                            {k: original[k] for k in ('name', 'bridge', 'address', 'cidr', 'hwaddr', 'mtu') if k in original},
                            lock_fd=guest['lock_fd'], template=bool(original.get('template')), trusted_hook=True,
                            live_firewall=original.get('firewall', 0))
                    for key in ('protection', 'tags', 'onboot', 'startup', 'hookscript', 'notes'):
                        if key in original:
                            restored[key] = original[key]
                    worker.write_json(worker.METADATA / (identifier + '.json'), restored)
                    value['restored'].append(identifier)
                    save(folder / 'journal.json', value)
            value['phase'] = 'complete'
            save(folder / 'journal.json', value)
            resume_maintenance(value['active'])
    print(json.dumps({'journal': folder.name, 'phase': value['phase'], 'guests': value['guests'],
                      'retained': str(folder), 'rollback': 'migrate-runtime.py --rollback ' + folder.name}))


def rollback(token):
    folder = ROOT / worker.project_id(token)
    value = read_json(folder / 'journal.json')
    if value['phase'] == 'rolled-back':
        resume_maintenance(value['active'])
        return
    if value['phase'] == 'backup':
        value['phase'] = 'rolled-back'; save(folder / 'journal.json', value)
        return
    # Persist rollback direction before cleanup so a retry does not enter conversion.
    value['phase'] = 'rollback'
    save(folder / 'journal.json', value)
    active = pause_maintenance()
    with contextlib.ExitStack() as locks:
        for guest in value['guests']:
            locks.enter_context(worker.operation(guest['id'], 'runtime-rollback', wait_timeout=300))
        if runtime.load().mode == value['target']:
            for guest in value['guests']:
                worker.require_stopped(worker.container_records(guest['id']))
        for guest in value['guests']:
            identifier = guest['id']
            destination = folder / identifier
            if runtime.load().mode == value['target']:
                guest_state.cleanup_import(identifier, keep_reservation=True)
            if runtime.load().mode == value['target'] and (worker.ROOT / identifier).exists():
                current = read_json(worker.METADATA / (identifier + '.json'))
                worker.require_stopped(worker.container_records(identifier))
                transfer.clean_local(identifier, current)
                (worker.METADATA / (identifier + '.json')).unlink(missing_ok=True)
        select_target(value['source'])
        if value['source'] == 'rootful':
            for unit, enabled in value.get('source_enabled', {}).items():
                command(['systemctl', 'enable' if enabled else 'disable', unit])
        for guest in value['guests']:
            identifier = guest['id']
            destination = folder / identifier
            project = worker.ROOT / identifier
            if (destination / 'original').exists():
                if project.exists():
                    raise ValueError('Rollback destination is occupied')
                (destination / 'original').rename(project)
            if (destination / 'metadata.json').exists():
                info = read_json(destination / 'metadata.json')
                worker.write_json(worker.METADATA / (identifier + '.json'), info)
                if (destination / 'mount-unit').exists():
                    unit = worker.capture(['systemd-escape', '--path', '--suffix=mount', str(project / 'storage-data')]).strip()
                    (Path('/etc/systemd/system') / unit).write_text((destination / 'mount-unit').read_text())
                    command(['systemctl', 'daemon-reload'])
                    command(['systemctl', 'enable', unit])
                    worker.storage_mount(identifier)
                worker.ensure_address(info)
        value['phase'] = 'rolled-back'
        save(folder / 'journal.json', value)
    resume_maintenance(value['active'] or active)
    print(json.dumps({'journal': token, 'phase': value['phase']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', choices=('rootful', 'rootless'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Root required')
    with open('/run/lock/betterdocker-install.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            if args.runtime or args.resume:
                raise ValueError('Rollback cannot be combined with another operation')
            if not args.apply:
                raise ValueError('Rollback changes the node; pass --apply explicitly')
            rollback(args.rollback)
        elif not args.runtime:
            raise ValueError('Choose --runtime=rootful or --runtime=rootless')
        elif args.apply:
            convert(args.runtime, args.resume)
        else:
            print(json.dumps(inventory(args.runtime), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
