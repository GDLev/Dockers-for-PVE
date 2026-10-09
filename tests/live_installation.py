#!/usr/bin/env python3
"""Package lifecycle on the disposable PVE cluster. Never install on PBS.

Run on pve1 after live_native_management.py. A saved manifest and --restore
recover the temporarily detached HA/replication configuration after interruption.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
sys.path.insert(0,'/usr/local/lib/betterdocker')
from transfer import ssh_command
from live_pools_hooks import api

STATE=Path('/root/dct-installation-acceptance.json')
PACKAGE=Path('/root/betterdocker-stage4-package/proxmox-betterdockers_0.19.0_all.deb')

def remote(node,command,**kwargs):
    return subprocess.run(['bash','-c',command] if node=='pve1' else ssh_command(node)+[command],check=True,**kwargs)

def restore(state):
    jobs=api('get','/cluster/replication')
    for job in state['replication']:
        if not any(i['id']==job['id'] for i in jobs):
            api('create','/cluster/replication',**{k:v for k,v in job.items() if k in ('id','type','target','schedule','rate','disable')})
    resources=api('get','/cluster/ha/resources')
    for resource in state['ha']:
        if not any(i['sid']==resource['sid'] for i in resources):
            api('create','/cluster/ha/resources',**{k:v for k,v in resource.items() if k in ('sid','state','group','comment','failback','max_restart','max_relocate')})
    print('PASS original HA and replication configuration restored',flush=True)

def containers(node):
    script="import json,subprocess;ids=subprocess.check_output(['docker','ps','-aq'],text=True).split();items=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True)) if ids else [];print(json.dumps({i['Id']:[i['State']['Status'],i['State']['StartedAt']] for i in items}))"
    return json.loads(remote(node,'python3 -c '+shlex.quote(script),capture_output=True,text=True).stdout)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--restore',action='store_true');args=parser.parse_args()
    if args.restore:restore(json.loads(STATE.read_text()));return
    if STATE.exists():raise RuntimeError('Previous installation test exists; inspect it and use --restore first')
    state={'ha':api('get','/cluster/ha/resources'),'replication':api('get','/cluster/replication')}
    assert [i['sid'] for i in state['ha']]==['docker:115'], 'Unexpected HA resources: review before testing'
    assert [i['id'] for i in state['replication']]==['115-0'], 'Unexpected replication jobs: review before testing'
    state['containers']={node:containers(node) for node in ('pve1','pve2','pve3')}
    STATE.write_text(json.dumps(state));STATE.chmod(0o600)
    fixture=json.loads(Path('/root/dct-native-management.json').read_text())
    base='/nodes/pve3/docker/'+fixture['id']
    hook='/var/lib/vz/snippets/native-install-'+str(fixture['vmid'])+'.pl'
    remote('pve3','mkdir -p /var/lib/vz/snippets && printf %s '+shlex.quote('#!/usr/bin/perl\nexit 0;\n')+' > '+shlex.quote(hook)+' && chmod 0755 '+shlex.quote(hook))
    api('set',base+'/config',hookscript='local:snippets/'+Path(hook).name)
    api('set',base+'/firewall/options',enable=1,policy_in='DROP')
    firewall=Path('/etc/pve/firewall/'+str(fixture['vmid'])+'.fw').read_bytes()
    archive=Path('/root/betterdocker-stage4.tgz')
    for node in ('pve3','pve2','pve1'):
        if node!='pve1':
            remote(node,'cat > /root/betterdocker-stage4.tgz',input=archive.read_bytes())
            remote(node,'cat > /root/betterdocker-stage4.deb',input=PACKAGE.read_bytes())
        package='/root/betterdocker-stage4.deb' if node!='pve1' else str(PACKAGE)
        remote(node,'mkdir -p /root/betterdocker-stage4 && tar -xzf /root/betterdocker-stage4.tgz -C /root/betterdocker-stage4 && dpkg -i '+shlex.quote(package)+' > /root/stage4-package.log 2>&1')
        assert containers(node)==state['containers'][node],node+' workload restarted'
        print('PASS package install/update with workloads',node,flush=True)
    guarded= subprocess.run(ssh_command('pve3')+['bash /root/betterdocker-stage4/start.sh uninstall'],capture_output=True,text=True)
    assert guarded.returncode and 'Remove DCT HA resources' in guarded.stderr
    print('PASS uninstall refuses active DCT HA registration before mutation',flush=True)
    try:
        # Force removes only the job configuration, preserving replicas and snapshots.
        # The workload remains running while its HA registration is temporarily detached.
        for resource in state['ha']:api('delete','/cluster/ha/resources/'+resource['sid'])
        for job in state['replication']:api('delete','/cluster/replication/'+job['id'],force=1)
        remote('pve3','dpkg -r proxmox-betterdockers > /root/stage4-remove.log 2>&1')
        remote('pve3','test ! -e /usr/sbin/dct && test ! -e /usr/share/perl5/PVE/API2/BetterDocker.pm && systemctl is-active --quiet betterdocker-firewall.service')
        assert containers('pve3')==state['containers']['pve3']
        assert Path('/etc/pve/firewall/'+str(fixture['vmid'])+'.fw').read_bytes()==firewall
        remote('pve3','test -f '+shlex.quote(hook))
        print('PASS package removal retains workloads, firewall, pool and hook files',flush=True)
        remote('pve3','bash /root/betterdocker-stage4/start.sh install > /root/stage4-clean-install.log 2>&1')
        remote('pve3','dpkg -i /root/betterdocker-stage4.deb > /root/stage4-reinstall.log 2>&1 && dpkg-trigger --no-await /usr/share/perl5/PVE/API2/Nodes.pm && dpkg --triggers-only proxmox-betterdockers > /root/stage4-trigger.log 2>&1')
        remote('pve3','python3 /usr/local/lib/betterdocker/patch.py status && dct status '+str(fixture['vmid'])+' > /root/stage4-cli-status.json')
        assert containers('pve3')==state['containers']['pve3']
        assert api('get',base+'/config')['hookscript']=='local:snippets/'+Path(hook).name
        assert any(i.get('vmid')==fixture['vmid'] for i in api('get','/pools',poolid=fixture['pool'])[0]['members'])
        print('PASS clean installation, package reinstall and triggers preserve configuration and runtime',flush=True)
    finally:
        restore(state)
    for node in state['containers']:
        assert containers(node)==state['containers'][node],node+' workload changed'
    api('set',base+'/config',delete='hookscript')
    remote('pve3','rm -- '+shlex.quote(hook))
    state['complete']=True;STATE.write_text(json.dumps(state))
    print('PASS package lifecycle acceptance',flush=True)

if __name__=='__main__':main()
