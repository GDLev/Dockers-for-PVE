#!/usr/bin/env python3
"""Verify native pool backup selection and storage entry points for a test DCT."""
import json
from pathlib import Path
import sys
sys.path.insert(0,'/usr/local/lib/betterdocker')
from live_pools_hooks import api,task
state=json.loads(Path('/root/dct-native-management.json').read_text())
job=state['name']+'-backup'
existing=api('get','/cluster/backup')
if any(i['id']==job for i in existing):api('delete','/cluster/backup/'+job)
try:
    api('create','/cluster/backup',id=job,schedule='sat 03:00',enabled=0,pool=state['pool'],storage='pbs1',mode='snapshot')
    config=api('get','/cluster/backup/'+job)
    assert config['pool']==state['pool'] and config['enabled']==0
    task(api('create','/nodes/pve3/vzdump',pool=state['pool'],storage='pbs1',mode='snapshot'))
    rows=api('get','/nodes/pve3/storage/pbs1/content',content='backup',vmid=state['vmid'])
    assert rows and all(row['vmid']==state['vmid'] for row in rows)
    archive=max(rows,key=lambda row:row['ctime'])
    assert archive['subtype']=='docker'
    state['archive']=archive['volid'];Path('/root/dct-native-management.json').write_text(json.dumps(state))
    print('PASS native backup job retains pool selection; pool vzdump produced the DCT PBS archive',flush=True)
finally:
    if any(i['id']==job for i in api('get','/cluster/backup')):api('delete','/cluster/backup/'+job)
