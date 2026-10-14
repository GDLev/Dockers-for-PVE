#!/usr/bin/env python3
"""Policy conversion, real kernel flags and adversarial recovery on disposable DCTs."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as w
from live_pools_hooks import api, task
from live_permissions import http
from live_firewall import remote

STATE = Path('/root/bd-security-stage23.json')
CAP_BITS = {'CHOWN':0,'DAC_OVERRIDE':1,'FOWNER':3,'FSETID':4,'KILL':5,'SETGID':6,'SETUID':7,'SETPCAP':8,'NET_BIND_SERVICE':10,'AUDIT_WRITE':29,'SETFCAP':31}
SOURCE = 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sleep, "86400"]\n    volumes: ["./data:/data", "/anonymous"]\n'


def check_kernel(info):
    data = api('get', '/nodes/'+info['node']+'/docker/'+info['id']+'/management')
    cid = data['containers'][0]['id']
    row = json.loads(remote(info['node'], ['docker','inspect',cid],check=True).stdout)[0]
    status = remote(info['node'], ['docker','exec',cid,'cat','/proc/1/status'],check=True).stdout
    fields = dict(line.split(':',1) for line in status.splitlines() if ':' in line)
    assert fields['NoNewPrivs'].strip() == '1' and fields['Seccomp'].strip() == '2'
    mask = int(fields['CapBnd'].strip(),16)
    assert mask & ~sum(1<<value for value in CAP_BITS.values()) == 0
    assert 'ALL' in row['HostConfig']['CapDrop']
    if info['node'] in ('pve1', 'pve2'): assert row['AppArmorProfile'] == 'betterdocker-default'
    # Inspect the mapped UID in host proc, not only the in-container identity.
    code = '''import sys,json,socket,struct
sys.path.insert(0,'/usr/local/lib/betterdocker')
import runtime
row=json.loads(runtime.run(['docker','inspect',sys.argv[1]],capture_output=True,text=True,check=True).stdout)[0]
from pathlib import Path
selected=runtime.load();pid=row['State']['Pid'];matches=[]
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
 connection.connect(selected.socket)
 daemon,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
 assert uid==selected.uid
daemon_fields=dict(line.split(':',1) for line in (Path('/proc')/str(daemon)/'status').read_text().splitlines() if ':' in line)
level=len(daemon_fields['NSpid'].split())-1
for path in Path('/proc').glob('[0-9]*/status'):
 try:
  fields=dict(line.split(':',1) for line in path.read_text().splitlines() if ':' in line)
  ids=fields.get('NSpid','').split()
  if len(ids)>level+1 and int(ids[level])==pid and ids[-1]=='1':
   matches.append(int(fields['Uid'].split()[0]))
 except (OSError,ValueError): pass
assert matches==[selected.host_id(0)],matches
'''
    result = remote(info['node'], ['python3','-c',code,cid])
    assert result.returncode == 0, result.stderr
    return cid


def rejected(method, base, values):
    result = http(method, base, values, headers=HEADERS, expected=(200,400,403,500))
    if isinstance(result,str) and result.startswith('UPID:'):
        try: task(result)
        except RuntimeError: return
        raise AssertionError('Forbidden mutation completed successfully')
    # Errors return null data; a successful synchronous mutation is not expected.
    assert result is None


def run():
    global HEADERS
    assert not STATE.exists() or '--resume' in sys.argv, 'Use --resume or clean previous owned fixtures first'
    before={int(r['vmid']) for r in api('get','/cluster/resources',type='vm')}
    nonce=uuid.uuid4().hex[:8]
    saved=json.loads(STATE.read_text()) if STATE.exists() else {'prefix':'security23-'+nonce,'guests':[],'users':[],'roles':[],'before':sorted(before)}
    nonce=saved['prefix'].removeprefix('security23-')
    STATE.write_text(json.dumps(saved));STATE.chmod(0o600)
    def save(): STATE.write_text(json.dumps(saved))
    fixtures=[]
    try:
        for node in (('pve3',) if '--rootless-only' in sys.argv else ('pve1','pve3')):
            name=saved['prefix']+'-'+node
            existing=next((r for r in w.summaries() if r['name']==name),None)
            if not existing:
                task(api('create','/nodes/'+node+'/docker',deployment={'name':name,'mode':'compose','compose':SOURCE,'bridge':'vmbr0','ipv4mode':'none'},start=1))
            info=next(r for r in w.summaries() if r['name']==name)
            if info['id'] not in saved['guests']: saved['guests'].append(info['id'])
            save();fixtures.append(info)
            base='/nodes/'+node+'/docker/'+info['id']
            cid=api('get',base+'/management')['containers'][0]['id']
            if not existing:
                remote(node,['docker','exec',cid,'sh','-c','echo writable >/marker; echo anonymous >/anonymous/value; echo bind >/data/value'],check=True)
                task(api('create',base+'/snapshot',action='create',snapname='before-policy'))
                task(api('create',base+'/action',action='force-stop'))
                task(api('set',base+'/security-policy',mode='hardened',digest=api('get',base+'/management')['digest']))
                task(api('create',base+'/action',action='start'))
            cid=check_kernel(info)
            assert remote(node,['docker','exec',cid,'cat','/marker','/anonymous/value','/data/value'],check=True).stdout.splitlines()==['writable','anonymous','bind']
            user='bd23-'+nonce+'-'+node+'@pve';role='BD23'+nonce+node
            api('create','/access/roles',roleid=role,privs='VM.Audit VM.Console VM.PowerMgmt VM.Config.CPU VM.Config.Memory VM.Config.Network VM.Config.Disk VM.Config.Options VM.Allocate VM.Clone VM.Snapshot VM.Snapshot.Rollback SDN.Use')
            saved['roles'].append(role);save()
            api('create','/access/users',userid=user);saved['users'].append(user);save()
            token=api('create','/access/users/'+user+'/token/test',privsep=1)
            for principals in ({'users':user},{'tokens':user+'!test'}):
                api('set','/access/acl',path='/vms/'+str(info['vmid']),roles=role,**principals)
                api('set','/access/acl',path='/sdn/zones/localnetwork/vmbr0',roles=role,**principals)
            HEADERS={'Authorization':'PVEAPIToken='+user+'!test='+token['value']}
            old=api('get',base+'/management')['digest']
            for settings in ({'no_new_privileges':False},{'cap_drop':[]},{'apparmor':'unconfined'}):
                rejected('PUT',base+'/settings',{'service':'app','settings':settings,'digest':old})
            http('PUT',base+'/security-policy',{'mode':'standard','digest':old},headers=HEADERS,expected=403)
            source=SOURCE.replace('    init:', '    security_opt: [no-new-privileges:false]\n    init:')
            deployment=api('get',base)['deployment'];deployment['compose']=source
            rejected('PUT',base,{'deployment':deployment,'digest':old})
            assert api('get',base+'/management')['digest']==old
            task(api('create',base+'/action',action='force-stop'))
            task(api('create',base+'/snapshot',action='restore',snapname='before-policy'))
            assert api('get',base+'/management')['security_policy']=='hardened'
            task(api('create',base+'/action',action='start'));check_kernel(info)
            # Tampered recovery is rejected before any container can be removed.
            code='''import json,sys
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as w
from dctlib.secure_io import read_json
p=w.ROOT/sys.argv[1]/'settings-transaction.json'
w.write_json(p,{'id':sys.argv[1],'phase':'preparing'})
value=json.loads(p.read_text());value['id']='b'*32;p.write_text(json.dumps(value))
try:
 with w.operation(sys.argv[1],'recovery'): raise AssertionError('forged journal accepted')
except ValueError as e: assert 'integrity' in str(e)
finally: p.unlink()
'''
            remote(node,['python3','-c',code,info['id']],check=True)
            check_kernel(info)
            print('PASS '+node+' conversion, real UID/NNP/seccomp/caps, immutable policy, rollback and forged recovery',flush=True)
            # New delegated allocation must select Hardened without trusting a YAML flag.
            vmid=next(n for n in range(980,999) if n not in {r['vmid'] for r in w.summaries()})
            for principals in ({'users':user},{'tokens':user+'!test'}): api('set','/access/acl',path='/vms/'+str(vmid),roles=role,**principals)
            name=saved['prefix']+'-delegated-'+node
            task(http('POST','/nodes/'+node+'/docker',{'deployment':{'vmid':vmid,'name':name,'mode':'compose','compose':SOURCE,'bridge':'vmbr0','ipv4mode':'none'}},headers=HEADERS))
            created=next(r for r in w.summaries() if r['name']==name)
            saved['guests'].append(created['id']);save();assert created['security_policy']=='hardened';check_kernel(created)
            print('PASS '+node+' delegated default and offline Compose parsing',flush=True)
        # Keep a single owned stopped workload for the brief final panel review.
        for info in fixtures:
            task(api('create','/nodes/'+info['node']+'/docker/'+info['id']+'/action',action='force-stop'))
        saved['before']=sorted(before);saved['complete']=True;save()
        print('PASS Stage 2/3 backend acceptance; fixtures retained for final UI review',flush=True)
    except BaseException:
        print('Fixtures retained in '+str(STATE)+'; use --cleanup after investigating the failure',flush=True)
        raise


def cleanup():
    if not STATE.exists(): return
    saved=json.loads(STATE.read_text())
    for backup in saved.get('backups', []):
        api('delete', '/nodes/' + backup['node'] + '/storage/pbs1/content/' + backup['archive'])
    saved['backups'] = []; STATE.write_text(json.dumps(saved))
    recorded = {r['id'] for r in w.summaries() if r['name'].startswith(saved['prefix'])}
    for identifier in reversed(list(dict.fromkeys(saved['guests'] + sorted(recorded)))):
        info=next((r for r in w.summaries() if r['id']==identifier),None)
        if info:
            assert info['name'].startswith(saved['prefix'])
            base='/nodes/'+info['node']+'/docker/'+identifier
            task(api('create',base+'/action',action='force-stop'))
            for snapshot in api('get',base+'/snapshots'):
                if snapshot['name']!='current': task(api('create',base+'/snapshot',action='delete',snapname=snapshot['name']))
            task(api('delete',base,**{'destroy-owned-data':1}))
    for user in saved['users']: api('delete','/access/users/'+user)
    for role in saved['roles']: api('delete','/access/roles/'+role)
    STATE.unlink();print('PASS owned security fixtures removed',flush=True)


if __name__=='__main__':
    cleanup() if '--cleanup' in sys.argv else run()
