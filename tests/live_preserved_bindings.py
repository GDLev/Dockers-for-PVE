#!/usr/bin/env python3
"""Preserved files must survive endpoint edits and unchanged-image previews."""
import json
from pathlib import Path
import sys
import tempfile
from live_service_security import api, call, runtime, w


def main():
    before = api('get','/cluster/resources',type='vm')
    vmid = next(i for i in range(940,980) if i not in {int(r['vmid']) for r in before})
    identifier, anonymous = None, None
    with tempfile.TemporaryDirectory(prefix='bd-binding-test-') as folder:
        source = Path(folder)/'compose.json'
        source.write_text(json.dumps({'services': {'app': {'image':'busybox:1.37', 'init':True, 'user':'65534',
            'command':['sleep','86400'], 'volumes':['/anonymous']}}}))
        try:
            call('dct','create',vmid,source,'--hostname','preserved-binding-test','--net0','bridge=vmbr0,ip='+sys.argv[1],'--start',1)
            row = next(r for r in api('get','/cluster/resources',type='vm') if int(r['vmid'])==vmid)
            identifier,node = row['id'].split('/')[-1],row['node']; base='/nodes/'+node+'/docker/'+identifier
            original=w.container_records(identifier)[0]['id']
            call('docker','exec','--user','0',original,'sh','-c','echo layer > /marker; echo volume > /anonymous/value')
            original_row=json.loads(call('docker','inspect',original))[0]
            anonymous=next(m['Name'] for m in original_row['Mounts'] if m['Destination']=='/anonymous')
            image=json.loads(call('docker','image','inspect',original_row['Image']))[0]
            call('dct','stop',vmid)
            api('set',base+'/settings',service='app',settings=json.dumps({'user':'','read_only':True,'no_new_privileges':True}),digest=w.detail(identifier,node)['digest'])
            current=w.container_records(identifier)[0]['id']
            api('create',base+'/images/preview',images=json.dumps({'app':image['RepoDigests'][0]}),digest=w.detail(identifier,node)['digest'])
            plan=api('get',base+'/images')
            assert plan['changes'][0]['current']==[original_row['Config']['Labels']['com.docker.compose.image']] and not plan['changes'][0]['changed'],plan
            assert w.container_records(identifier)[0]['id']==current
            api('set',base+'/network',net='net0',mtu=1400,digest=w.detail(identifier,node)['digest'])
            call('dct','start',vmid)
            current=w.container_records(identifier)[0]['id']
            assert call('docker','exec',current,'cat','/marker').strip()=='layer'
            assert call('docker','exec',current,'cat','/anonymous/value').strip()=='volume'
            assert call('docker','exec',current,'id','-u').strip()=='0'
            actual=json.loads(call('docker','inspect',current))[0]
            assert actual['HostConfig']['ReadonlyRootfs']
            assert next(m['Name'] for m in actual['Mounts'] if m['Destination']=='/anonymous')==anonymous
            assert 'NoNewPrivs:\t1' in call('docker','exec',current,'cat','/proc/self/status')
            print('PASS: unchanged-image preview and network edits retain files, anonymous volume identity and security on '+node,flush=True)
        finally:
            if identifier:
                call('dct','stop',vmid);call('dct','destroy',vmid,'--destroy-owned-data',1)
                if anonymous:call('docker','volume','rm',anonymous)
            assert {r['vmid'] for r in api('get','/cluster/resources',type='vm')}=={r['vmid'] for r in before}
            print('PASS: fixture removed, original guests preserved',flush=True)


if __name__=='__main__': main()
