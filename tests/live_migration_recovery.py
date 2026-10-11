#!/usr/bin/env python3
"""Kill a disposable transfer after upload and verify source ownership recovery."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
sys.path[:0] = ['/usr/local/lib/betterdocker', '/tmp/bd-stage45-source/tests']
from live_stage67 import api, task, wait_for, base, get, current, power, command, fetch
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
    data = {'name': saved['name'], 'mode': 'compose', 'bridge': 'vmbr0', 'cidr': '192.0.2.235/20',
        'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo recovery-ok >/www/index.html; exec httpd -f -p 8080 -h /www"]\n    ports: ["8080:8080"]\n'}
    task(api('create', '/nodes/pve1/docker', deployment=json.dumps(data), start=1))
    identifier = next(row['id'] for row in w.summaries() if row['name'] == saved['name'])
    saved['id'] = identifier; MANIFEST.write_text(json.dumps(saved))
    command(identifier, 'sh', '-c', 'echo kept >/preserved')
    code = '''import sys,time
from pathlib import Path
sys.path.insert(0,"/usr/local/lib/betterdocker")
import transfer
original=transfer.remote
def remote(node,*args):
 if args[0]=="prepare":
  Path("/run/bd-migration-recovery-ready").write_text("uploaded")
  time.sleep(300)
 return original(node,*args)
transfer.remote=remote
transfer.migrate(ID,"pve1","pve2","",restart=1,timeout=10)
'''.replace('ID', repr(identifier))
    process = subprocess.Popen(['python3', '-c', code], start_new_session=True)
    try:
        wait_for(READY.exists, timeout=120)
        assert current(identifier)['status'] != 'running'
        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
        wait_for(lambda: not w.operation_state(identifier).get('active'))
        transfer.recover_all()
        assert get(identifier)['node'] == 'pve1' and 'migration' not in current(identifier)
        wait_for(lambda: fetch('192.0.2.235', path='').stdout.strip() == 'recovery-ok')
        assert command(identifier, 'cat', '/preserved') == 'kept'
        target = subprocess.run(transfer.ssh_command('pve2') + ["/usr/local/lib/betterdocker/runtime.py docker ps -aq --filter label=com.docker.compose.project=bd-" + identifier], capture_output=True, text=True, check=True)
        assert not target.stdout.strip()
        print('PASS killed transfer restores source ownership, service and writable layer; no target containers', flush=True)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait()
    cleanup()


if __name__ == '__main__':
    cleanup() if '--cleanup' in sys.argv else run()
