#!/usr/bin/env python3
"""Fresh rootless install, failed-install recovery and workload-retaining removal.

Test cluster only. Hold HA managers while temporarily detaching their native
configuration, so a previously errored resource is not reactivated by testing.
--restore releases the maintenance hold after any interruption.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
import transfer
from live_firewall import remote
from live_pools_hooks import api, task
from live_rootless_package import containers

STATE = Path('/root/dct-rootless-installation.json')
NODES = ('pve1', 'pve2', 'pve3')
DROPIN = '/etc/systemd/system/{unit}.service.d/betterdocker-rootless-install-test.conf'
CONDITION = '/run/betterdocker-rootless-install-test-allow'


def hold(state):
    for node in NODES:
        for unit in ('pve-ha-crm', 'pve-ha-lrm'):
            script = 'from pathlib import Path;p=Path(' + repr(DROPIN.format(unit=unit)) + ');assert not p.exists();p.parent.mkdir(parents=True,exist_ok=True);p.write_text(' + repr('[Unit]\nConditionPathExists=' + CONDITION + '\n') + ')'
            remote(node, ['python3', '-c', script], check=True)
        remote(node, ['systemctl', 'daemon-reload'], check=True)
        remote(node, ['systemctl', 'stop', 'pve-ha-crm', 'pve-ha-lrm'], check=True, timeout=180)
    for resource in state['ha']:
        api('delete', '/cluster/ha/resources/' + resource['sid'], purge=0)
    for job in state['replication']:
        api('delete', '/cluster/replication/' + job['id'], force=1)


def restore(state):
    jobs = api('get', '/cluster/replication')
    for job in state['replication']:
        if not any(i['id'] == job['id'] for i in jobs):
            api('create', '/cluster/replication', **{k:v for k,v in job.items() if k in ('id','type','target','schedule','rate','disable')})
    resources = api('get', '/cluster/ha/resources')
    for resource in state['ha']:
        if not any(i['sid'] == resource['sid'] for i in resources):
            api('create', '/cluster/ha/resources', **{k:v for k,v in resource.items() if k in ('sid','state','group','comment','failback','max_restart','max_relocate')})
    for node in NODES:
        for unit in ('pve-ha-crm', 'pve-ha-lrm'):
            remote(node, ['rm', '-f', DROPIN.format(unit=unit)], check=True)
        remote(node, ['systemctl', 'daemon-reload'], check=True)
        remote(node, ['systemctl', 'start', 'pve-ha-crm', 'pve-ha-lrm'], check=True)
    print('PASS original HA/replication configuration and HA managers restored', flush=True)


def fresh_install(state):
    assert not [i for i in worker.summaries() if i['node'] == 'pve3']
    selected = remote('pve3', ['cat', '/root/betterdocker-rootless-before-fresh/runtime.json'], check=True).stdout
    code = "from pathlib import Path\narchive=Path('/root/betterdocker-rootless-before-fresh')\nassert archive.is_dir() and not Path('/etc/betterdocker/runtime.json').exists()\nprojects=Path('/var/lib/betterdocker/projects')\nif projects.exists():\n if (archive/'projects').exists(): assert not any(projects.iterdir())\n else: projects.rename(archive/'projects')\n"
    remote('pve3', ['python3', '-c', code], check=True)
    remote('pve3', ['debconf-set-selections'], input='proxmox-betterdockers proxmox-betterdockers/runtime select rootless\n', check=True)
    failed = '/root/betterdocker-rootless-failed-source'
    code = 'from pathlib import Path;import shutil;p=Path(' + repr(failed) + ');shutil.copytree("/root/betterdocker-rootless-source",p,dirs_exist_ok=True);s=p/"scripts/patch.py";t=s.read_text();assert "args = parser.parse_args()" in t;s.write_text(t.replace("args = parser.parse_args()","args = parser.parse_args()\\n    if args.action == \'apply\': raise ValueError(\'Injected late installation failure\')"))'
    remote('pve3', ['python3', '-c', code], check=True)
    remote('pve3', ['bash', failed + '/packaging/build-deb.sh', '/root/betterdocker-rootless-failed-package'], check=True)
    failed_result = remote('pve3', ['bash', '-c', 'DEBIAN_FRONTEND=noninteractive dpkg -i /root/betterdocker-rootless-failed-package/proxmox-betterdockers_0.19.0_all.deb > /root/dct-rootless-failed-install.log 2>&1'], timeout=300)
    assert failed_result.returncode != 0
    log = remote('pve3', ['cat', '/root/dct-rootless-failed-install.log'], check=True).stdout
    assert 'Injected late installation failure' in log and 'restoring integration' in log, log
    for path in ('/etc/betterdocker/runtime.json', '/etc/systemd/system/betterdocker-rootless.service', '/var/lib/systemd/linger/betterdocker'):
        assert remote('pve3', ['test', '-e', path]).returncode != 0, path
    assert remote('pve3', ['systemctl', 'is-active', '--quiet', 'docker']).returncode != 0
    print('PASS failed fresh installation restores integration and removes newly enabled rootless services', flush=True)
    remote('pve3', ['bash', '-c', 'DEBIAN_FRONTEND=noninteractive dpkg -i /root/betterdocker-rootless-package.deb > /root/dct-rootless-fresh-install.log 2>&1'], check=True, timeout=300)
    current = json.loads(remote('pve3', ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout)
    assert current == json.loads(selected)
    print('PASS fresh DEB rootless choice initializes the selected daemon with stable identity', flush=True)
    state['complete'] = True; STATE.write_text(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--resume-fresh', action='store_true')
    args = parser.parse_args()
    if args.restore:
        restore(json.loads(STATE.read_text())); return
    assert worker.current_node() == 'pve1'
    if args.resume_fresh:
        state = json.loads(STATE.read_text())
        assert not state.get('complete')
        try:
            hold(state)
            fresh_install(state)
        finally:
            restore(state)
        return
    assert not STATE.exists()
    state = dict(ha=api('get', '/cluster/ha/resources'), replication=api('get', '/cluster/replication'))
    assert [i['sid'] for i in state['ha']] == ['docker:115']
    assert [i['id'] for i in state['replication']] == ['115-0']
    STATE.write_text(json.dumps(state, indent=2)); STATE.chmod(0o600)
    guarded = remote('pve3', ['bash', '/opt/betterdocker/start.sh', 'uninstall'])
    assert guarded.returncode != 0 and 'Remove DCT HA resources' in guarded.stderr
    print('PASS uninstall guard rejects HA registration before changing the node', flush=True)
    try:
        hold(state)
        fixture = json.loads(remote('pve3', ['cat', '/root/betterdocker-rootless-fixture.json'], check=True).stdout)
        base = '/nodes/pve3/docker/' + fixture['id']
        task(api('create', base + '/action', action='start'))
        selected = remote('pve3', ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout
        before = containers('pve3')
        remote('pve3', ['bash', '-c', 'dpkg -r proxmox-betterdockers > /root/dct-rootless-remove.log 2>&1'], check=True, timeout=300)
        assert containers('pve3') == before
        assert remote('pve3', ['cat', '/etc/betterdocker/runtime.json'], check=True).stdout == selected
        remote('pve3', ['systemctl', 'is-active', '--quiet', 'betterdocker-firewall', 'betterdocker-rootless'], check=True)
        remote('pve3', ['bash', '-c', 'DEBIAN_FRONTEND=noninteractive dpkg -i /root/betterdocker-rootless-package.deb > /root/dct-rootless-reinstall.log 2>&1'], check=True, timeout=300)
        assert containers('pve3') == before
        print('PASS package removal and reinstall retain selected runtime, live workloads and host maintenance', flush=True)
        remote('pve3', ['bash', '-c', 'cd /root/betterdocker-rootless-source/tests && python3 live_rootless.py --cleanup'], check=True, timeout=300)
        remote('pve3', ['bash', '-c', 'dpkg -r proxmox-betterdockers > /root/dct-rootless-empty-remove.log 2>&1'], check=True, timeout=300)
        code = '''import sys,json,subprocess,shutil
from pathlib import Path
sys.path.insert(0,"/usr/local/lib/betterdocker")
import runtime,host,provision as w
r=runtime.load();assert r.rootless and not [i for i in w.summaries() if i["node"]=="pve3"]
assert not runtime.run(["docker","ps","-aq"],text=True,capture_output=True,check=True).stdout.strip()
runtime.service("stop").check_returncode()
subprocess.run(["systemctl","disable","--now","betterdocker-rootless.service"],check=True)
host.remove()
subprocess.run(["loginctl","disable-linger","betterdocker"],check=True)
subprocess.run(["systemctl","stop","user@"+str(r.uid)],check=True)
for p in (Path("/etc/systemd/system/betterdocker-rootless.service"),Path("/etc/systemd/system/betterdocker-lifecycle.service.d/runtime.conf"),Path("/etc/systemd/system")/("user@"+str(r.uid)+".service.d/betterdocker.conf")):p.unlink(missing_ok=True)
archive=Path("/root/betterdocker-rootless-before-fresh");archive.mkdir(mode=0o700)
runtime.CONFIG.rename(archive/"runtime.json")
Path(r.home).rename(archive/"runtime")
subprocess.run(["systemctl","daemon-reload"],check=True)
'''
        remote('pve3', ['python3', '-c', code], check=True, timeout=180)
        fresh_install(state)
    finally:
        restore(state)


if __name__ == '__main__':
    main()
