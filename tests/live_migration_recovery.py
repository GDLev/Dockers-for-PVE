#!/usr/bin/env python3
"""Kill a disposable transfer after upload and verify source ownership recovery."""
import live_environment as env
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
sys.path[:0] = ['/usr/local/lib/betterdocker', '/tmp/bd-stage45-source/tests']
from live_cluster import api, task, wait_for, base, get, current, power, command, fetch
from dctlib import projects as w
import transfer

MANIFEST = Path('/root/bd-migration-recovery.json')
READY = Path('/run/bd-migration-recovery-ready')


def cleanup():
    saved = json.loads(MANIFEST.read_text())
    identifier = saved.get('id')
    if identifier and any(row['id'] == identifier for row in w.summaries()):
        assert get(identifier)['name'] == saved['name']
        power(identifier, 'force-stop')
        task(api('delete', base(get(identifier)), **{'destroy-owned-data': 1}))
    MANIFEST.unlink(); READY.unlink(missing_ok=True)


def run():
    assert not MANIFEST.exists()
    saved = {'name': 'migration-recovery-' + uuid.uuid4().hex[:8]}; MANIFEST.write_text(json.dumps(saved))
    data = {'name': saved['name'], 'mode': 'compose', 'bridge': 'vmbr0', 'cidr': f'{env.address(235)}/{env.prefix()}',
        'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo recovery-ok >/www/index.html; exec httpd -f -p 8080 -h /www"]\n    ports: ["8080:8080"]\n'}
    task(api('create', f"/nodes/{env.node('primary')}/docker", deployment=json.dumps(data), start=1))
    identifier = next(row['id'] for row in w.summaries() if row['name'] == saved['name'])
    saved['id'] = identifier; MANIFEST.write_text(json.dumps(saved))
    command(identifier, 'sh', '-c', 'echo kept >/preserved')
    code = f'''import sys,time\nfrom pathlib import Path\nsys.path.insert(0,"/usr/local/lib/betterdocker")\nimport transfer\noriginal=transfer.remote\ndef remote(node,*args):\n if args[0]=="prepare":\n  Path("/run/bd-migration-recovery-ready").write_text("uploaded")\n  time.sleep(300)\n return original(node,*args)\ntransfer.remote=remote\ntransfer.migrate(ID,"{env.node('primary')}","{env.node('secondary')}","",restart=1,timeout=10)\n'''.replace('ID', repr(identifier))
    process = subprocess.Popen(['python3', '-c', code], start_new_session=True)
    try:
        wait_for(READY.exists, timeout=120)
        assert current(identifier)['status'] != 'running'
        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
        wait_for(lambda: not w.operation_state(identifier).get('active'))
        transfer.recover_all()
        assert get(identifier)['node'] == env.node('primary') and 'migration' not in current(identifier)
        wait_for(lambda: fetch(env.address(235), path='').stdout.strip() == 'recovery-ok')
        assert command(identifier, 'cat', '/preserved') == 'kept'
        target = subprocess.run(transfer.ssh_command(env.node('secondary')) + ["/usr/local/lib/betterdocker/runtime.py docker ps -aq --filter label=com.docker.compose.project=bd-" + identifier], capture_output=True, text=True, check=True)
        assert not target.stdout.strip()
        print('PASS killed transfer restores source ownership, service and writable layer; no target containers', flush=True)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait()
    cleanup()


if __name__ == '__main__':
    cleanup() if '--cleanup' in sys.argv else run()
