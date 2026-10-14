#!/usr/bin/env python3
"""Rootless acceptance on a deployed test node. Retain only recorded fixtures for GUI checks."""
import live_environment as env
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
import runtime
import guest_state
import live_pools_hooks as live

STATE = Path('/root/betterdocker-rootless-fixture.json')


def task(upid):
    return live.task(upid)


def save(value):
    STATE.write_text(json.dumps(value, indent=2)); STATE.chmod(0o600)


def cleanup():
    if not STATE.exists():
        return
    value = json.loads(STATE.read_text())
    identifier = worker.project_id(value['id'])
    info_path = worker.METADATA / (identifier + '.json')
    if info_path.exists():
        info = json.loads(info_path.read_text())
        if info['name'] != value['name']:
            raise ValueError('Fixture ownership changed')
        base = '/nodes/' + info['node'] + '/docker/' + identifier
        task(live.api('create', base + '/action', action='force-stop'))
        for snapshot in live.api('get', base + '/snapshots'):
            if snapshot['name'] != 'current':
                task(live.api('create', base + '/snapshot', action='delete', snapname=snapshot['name']))
        task(live.api('delete', base))
    volumes = worker.capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier]).split()
    if volumes:
        worker.run(['docker', 'volume', 'rm', *volumes], capture_output=True)
    project = worker.ROOT / identifier
    if project.exists():
        if json.loads((project / 'request.json').read_text())['name'] != value['name']:
            raise ValueError('Retained fixture ownership changed')
        shutil.rmtree(project)
    STATE.unlink()


def run(address, keep=False):
    selected = runtime.load()
    assert selected.rootless, 'Select rootless before acceptance'
    if STATE.exists():
        raise ValueError('Previous fixture exists; inspect or clean it first')
    node = worker.current_node()
    engine = json.loads(worker.capture(['docker', 'info', '--format', '{{json .}}']))
    assert any('rootless' in option for option in engine['SecurityOptions'])
    assert engine['CgroupVersion'] == '2' and engine['CgroupDriver'] == 'systemd'
    # This probe crosses the actual process boundary, without mocking the UID.
    code = 'import os,socket; assert os.geteuid()!=0; assert not os.getgroups();\nfor p in ("/etc/shadow","/root/.docker/config.json"):\n try: open(p).read(); raise AssertionError("root file readable")\n except (PermissionError,FileNotFoundError): pass\ns=socket.socket(socket.AF_UNIX);\ntry: s.connect("/var/run/docker.sock"); raise AssertionError("root socket accessible")\nexcept (PermissionError,FileNotFoundError,ConnectionRefusedError): pass\nprint(os.geteuid())'
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True,
                            env=selected.environment(), **runtime.drop_options(selected))
    assert int(result.stdout) == selected.uid
    print('PASS rootless daemon, cgroup driver and unprivileged worker boundary', flush=True)
    name = 'rootless-acceptance-' + uuid.uuid4().hex[:8]
    vmid = live.api('get', '/cluster/nextid')
    deployment = dict(vmid=int(vmid), name=name, mode='compose', bridge='vmbr0', address=address,
        compose='services:\n  app:\n    image: busybox:stable\n    init: true\n    command: [sh, -c, "mkdir -p /data; test -f /data/index.html || echo original > /data/index.html; exec httpd -f -p 8080 -h /data"]\n    ports: ["80:8080"]\n    volumes: ["data:/data"]\nvolumes:\n  data: {}\n', environment='')
    upid = live.api('create', '/nodes/' + node + '/docker', deployment=json.dumps(deployment))
    row = None
    for _ in range(60):
        row = next((i for i in live.api('get', '/cluster/resources', type='vm') if i.get('vmid') == int(vmid)), None)
        if row:
            break
        time.sleep(.2)
    if not row:
        task(upid)
        raise AssertionError('Created DCT was not published')
    identifier = row['id'].split('/')[-1]
    value = dict(name=name, vmid=int(vmid), id=identifier, node=node, address=address)
    save(value)
    task(upid)
    base = '/nodes/' + node + '/docker/' + identifier
    try:
        for _ in range(30):
            probe = subprocess.run(['curl', '--fail', '--silent', '--max-time', '2', 'http://' + address], capture_output=True, text=True)
            if probe.returncode == 0:
                break
            time.sleep(.5)
        assert probe.returncode == 0 and probe.stdout.strip() == 'original', probe.stderr
        info = live.api('get', base + '/management')
        task(live.api('set', base + '/settings', service='app', settings=json.dumps(dict(cpus=.5, memory=64, pids=64)), digest=info['digest']))
        if any('pending' in row for row in worker.pending_settings(identifier, node, 'app')):
            task(live.api('create', base + '/action', action='reboot', timeout=10))
        cid = worker.container_records(identifier)[0]['id']
        inspected = json.loads(worker.capture(['docker', 'inspect', cid]))[0]
        assert inspected['HostConfig']['Memory'] == 67108864 and inspected['HostConfig']['PidsLimit'] == 64
        assert inspected['HostConfig']['NanoCpus'] == 500000000 or inspected['HostConfig']['CpuQuota'] > 0
        print('PASS published port 80 and actual CPU/memory/PID limits', flush=True)
        task(live.api('create', base + '/snapshot', action='create', snapname='before-change'))
        worker.run(['docker', 'exec', cid, 'sh', '-c', 'echo changed > /data/index.html; chown 999:999 /data/index.html'], capture_output=True)
        task(live.api('create', base + '/snapshot', action='restore', snapname='before-change'))
        task(live.api('create', base + '/action', action='start'))
        cid = worker.container_records(identifier)[0]['id']
        assert worker.capture(['docker', 'exec', cid, 'cat', '/data/index.html']).strip() == 'original'
        assert worker.capture(['docker', 'exec', cid, 'stat', '-c', '%u:%g', '/data/index.html']).strip() == '0:0'
        print('PASS snapshot rollback restores data and container owners', flush=True)
        task(live.api('create', base + '/action', action='shutdown', timeout=10))
        folder = Path('/root') / (name + '-state')
        guest_state.capture_state(identifier, node, folder)
        manifest = guest_state.check_state(folder)
        assert manifest['version'] == 2 and manifest['runtime']['mode'] == 'rootless'
        volume = next(m for m in manifest['mounts'] if m['type'] == 'volume')
        assert (folder / volume['copy'] / 'index.html').stat().st_uid == 0
        print('PASS portable backup stores container ownership', flush=True)
        shutil.rmtree(folder)
        if keep:
            print(json.dumps(value), flush=True)
    finally:
        if not keep:
            cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', default=env.address(211))
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    cleanup() if args.cleanup else run(args.address, args.keep)
