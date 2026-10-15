#!/usr/bin/env python3
"""Check native guest operations and retain owned fixtures for GUI and cleanup."""
import live_environment as env
import argparse
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time
import uuid
import urllib.parse
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as worker
from transfer import ssh_command
from live_pools_hooks import api, task, wait_for
from live_permissions import http

MANIFEST=Path('/root/dct-native-management.json')
def remote(node,command):
    return subprocess.run(command if node==worker.current_node() else ssh_command(node)+[shlex.join(command)],capture_output=True,text=True,check=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    args = parser.parse_args()
    manifest = args.manifest
    state=json.loads(manifest.read_text()) if manifest.exists() else {'name':'native-'+uuid.uuid4().hex[:8]}
    def save(): manifest.write_text(json.dumps(state));manifest.chmod(0o600)
    save()
    def info(): return next(i for i in worker.summaries() if i['name']==state['name'])
    def base():
        item=info();return f'/nodes/{item["node"]}/docker/{item["id"]}'
    if args.cleanup:
        for item in worker.summaries():
            if item['name'] in (state['name'], state['name']+'-denied'):
                route = f'/nodes/{item["node"]}/docker/{item["id"]}'
                task(api('create',route+'/action',action='force-stop'))
                task(api('delete',route))
        for resource,key in [('users','user'),('roles','role')]:
            if state.get(key): api('delete','/access/'+resource+'/'+state[key])
        if state.get('pool'):api('delete','/pools',poolid=state['pool'])
        for archive in state.get('archives',[]):
            api('delete',f"/nodes/{archive['node']}/storage/{archive['storage']}/content/"+archive['volid'])
        manifest.unlink();print('PASS fixture cleanup',flush=True);return
    if not any(i['name']==state['name'] for i in worker.summaries()):
        state['pool']=state['name'];save();api('create','/pools',poolid=state['pool'])
        deployment={'name':state['name'],'mode':'compose','compose':'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "trap \'\' TERM; while :; do sleep 1; done"]\n',
                    'bridge':'vmbr0','ipv4mode':'static','cidr':f'{env.address(216)}/{env.prefix()}'}
        vmid=api('get','/cluster/nextid')
        path=Path('/root/'+state['name']+'.json');path.write_text(json.dumps(deployment));path.chmod(0o600)
        try:remote(env.node('primary'),['dct','create',str(vmid),str(path),'--node',env.node('rootless'),'--pool',state['pool']])
        finally:path.unlink()
        assert api('get',base()+'/management')['status']=='stopped'
        print('PASS CLI creation leaves guest stopped',flush=True)
    guest=info();vmid=guest['vmid'];state['vmid']=vmid;state['id']=guest['id'];save()
    remote(env.node('primary'),['dct','stop',str(vmid)])
    assert json.loads(remote(env.node('primary'),['dct','status',str(vmid),'--output-format','json']).stdout)['status']=='stopped'
    print('PASS CLI resolves guest on remote node',flush=True)
    denied_name = state['name'] + '-denied'
    denied = next((item for item in worker.summaries() if item['name'] == denied_name), None)
    if denied is None:
        task(api('create', f'/nodes/{env.node("primary")}/docker', deployment={
            'name': denied_name, 'mode': 'compose', 'bridge': 'vmbr0', 'ipv4mode': 'none',
            'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sleep, "86400"]\n    cpus: 0.1\n    mem_limit: 64m\n    pids_limit: 32\n'}, start=0))
        denied = next(item for item in worker.summaries() if item['name'] == denied_name)
    state['denied_id'], state['denied_vmid'] = denied['id'], denied['vmid']; save()
    state['role']='Native'+state['name'][-8:];state['user']=state['name']+'@pve';save()
    if not any(i['userid']==state['user'] for i in api('get','/access/users')):
        api('create','/access/roles',roleid=state['role'],privs='VM.Audit VM.PowerMgmt VM.Migrate')
        api('create','/access/users',userid=state['user'],password=secrets.token_urlsafe(24))
    if any(t['tokenid']=='native' for t in api('get','/access/users/'+state['user']+'/token')):
        api('delete','/access/users/'+state['user']+'/token/native')
    token=api('create','/access/users/'+state['user']+'/token/native',privsep=1)
    headers={'Authorization':'PVEAPIToken='+state['user']+'!native='+token['value']}
    for principal in ({'users':state['user']},{'tokens':state['user']+'!native'}):
        api('set','/access/acl',path='/pool/'+state['pool'],roles=state['role'],**principal)
    def bulk(action, values, expected=200):
        values=dict(values);ids=values.pop('vms')
        route='/cluster/bulk-action/guest/'+action+'?'+urllib.parse.urlencode({'vms':ids},doseq=True)
        return http('POST',route,values,headers=headers,expected=expected)
    for action in ('start','migrate'):
        values={'vms':[vmid,state['denied_vmid']]}
        if action=='migrate':values['target']=env.node('secondary')
        bulk(action,values,expected=403)
    task(bulk('start',{'vms':[vmid]}))
    assert api('get',base()+'/management')['status']=='running'
    try:task(bulk('migrate',{'vms':[vmid],'target':env.node('secondary')}))
    except RuntimeError as error: assert 'stop' in str(error).lower(),str(error)
    else:raise AssertionError('Running ordinary DCT migrated')
    task(bulk('shutdown',{'vms':[vmid],'timeout':1,'force-stop':1}))
    assert api('get',base()+'/management')['status']=='stopped'
    task(bulk('migrate',{'vms':[vmid],'target':env.node('secondary')}))
    assert info()['node']==env.node('secondary')
    task(http('POST',f"/nodes/{env.node('secondary')}/migrateall",{'vms':str(vmid),'target':env.node('rootless'),'maxworkers':1},headers=headers))
    assert info()['node']==env.node('rootless')
    print('PASS native cluster/node bulk operations, inherited token ACLs and running migration rejection',flush=True)
    remote(env.node('primary'),['dct','start',str(vmid)])
    upid=api('create',base()+'/action',action='shutdown',timeout=120)
    time.sleep(3)
    route='/nodes/'+upid.split(':')[1]+'/tasks/'+upid
    api('delete',route)
    wait_for(lambda:api('get',route+'/status')['status']=='stopped')
    assert api('get',route+'/status')['exitstatus']!='OK'
    wait_for(lambda:not api('get',base()+'/management')['operation'].get('active'))
    remote(env.node('primary'),['dct','stop',str(vmid)])
    upid=api('create',base()+'/shutdown',timeout=1,**{'force-stop':1})
    assert isinstance(upid,str) and upid.startswith('UPID:'),repr(upid)
    task(upid)
    assert api('get',base()+'/management')['status']=='stopped'
    remote(env.node('primary'),['dct','start',str(vmid)])
    print('PASS native task cancellation releases operation lock and permits the next command',flush=True)
    api('set',base()+'/config',protection=1)
    try:api('delete',base())
    except RuntimeError as error:assert 'protection' in str(error)
    else:raise AssertionError('Protected DCT removed')
    api('set',base()+'/config',protection=0)
    old=api('get',base()+'/management')['digest']
    api('set',base()+'/config',description='native management acceptance',digest=old)
    try:api('set',base()+'/config',description='stale',digest=old)
    except RuntimeError as error:assert 'modified' in str(error)
    else:raise AssertionError('Stale configuration overwritten')
    print('PASS protection and stale configuration rejection',flush=True)
    api('delete','/access/users/'+state['user']);api('delete','/access/roles/'+state['role'])
    state.pop('user');state.pop('role');save()
    print('PASS native management acceptance; retained DCT',vmid,'for GUI',flush=True)

if __name__=='__main__':main()
