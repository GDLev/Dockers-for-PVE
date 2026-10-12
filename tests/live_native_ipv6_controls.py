#!/usr/bin/env python3
"""Compile and remove host exceptions in an isolated nftables namespace."""
import subprocess
import sys
import uuid


def main():
    namespace = 'bdnft-' + uuid.uuid4().hex[:8]
    code = '''import json,sys,subprocess
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import ipv6_controls
rules='table inet proxmox-firewall {\\n chain input { type filter hook input priority 0; policy drop; }\\n chain output { type filter hook output priority 0; policy drop; }\\n}\\n'
subprocess.run(['nft','-f','-'],input=rules,text=True,check=True)
ipv6_controls.ensure({'interface':'bdowned'})
ipv6_controls.ensure({'interface':'bdowned'})
state=json.loads(subprocess.check_output(['nft','-j','list','table','inet','proxmox-firewall']))
own=[entry['rule'] for entry in state['nftables'] if 'rule' in entry and entry['rule'].get('comment')=='betterdocker-control6:bdowned']
assert len(own)==4,own
ipv6_controls.ensure({'interface':'bdowned'},remove=True)
state=json.loads(subprocess.check_output(['nft','-j','list','table','inet','proxmox-firewall']))
assert not any(entry.get('rule',{}).get('comment')=='betterdocker-control6:bdowned' for entry in state['nftables'])
print('PASS: native nftables host controls compile, apply once and remove only owned endpoint rules',flush=True)
'''
    try:
        subprocess.run(['ip', 'netns', 'add', namespace], check=True)
        subprocess.run(['ip', 'netns', 'exec', namespace, sys.executable, '-c', code], check=True)
    finally:
        subprocess.run(['ip', 'netns', 'del', namespace], check=True)


if __name__ == '__main__':
    main()
