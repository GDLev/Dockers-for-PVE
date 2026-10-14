#!/usr/bin/env python3
"""Exercise hardened image updates, clone and PBS restore on recorded fixtures."""
import json
from pathlib import Path
import sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
from live_security_stage23 import STATE, check_kernel, SOURCE
from live_pools_hooks import api, task
from live_firewall import remote
from dctlib import projects as w


def main():
    saved = json.loads(STATE.read_text())
    assert saved.get('complete')
    def save(): STATE.write_text(json.dumps(saved))
    for node in ('pve1', 'pve3'):
        info = next(r for r in w.summaries() if r['name'] == saved['prefix'] + '-' + node)
        base = '/nodes/' + node + '/docker/' + info['id']
        progress = saved.setdefault('mutation_progress', {}).setdefault(node, {})
        if not progress.get('image'):
            task(api('create', base + '/images/preview', images={'app': 'busybox:1.36'}, digest=api('get', base + '/management')['digest']))
            plan = api('get', base + '/images')
            task(api('create', base + '/images', token=plan['token'], recovery='image-policy-check', digest=api('get', base + '/management')['digest']))
            task(api('create', base + '/action', action='start')); check_kernel(info)
            task(api('create', base + '/action', action='force-stop'))
            print('PASS ' + node + ' image update retains mandatory policy', flush=True)
            progress['image'] = True; save()
        if not progress.get('clone'):
            vmid = int(api('get', '/cluster/nextid'))
            name = saved['prefix'] + '-clone-' + node
            task(api('create', base + '/clone', newid=vmid, deployment={'name': name, 'bridge': 'vmbr0', 'ipv4mode': 'none'}, digest=api('get', base + '/management')['digest']))
            clone = next(r for r in w.summaries() if r['name'] == name)
            saved['guests'].append(clone['id']); save()
            clonebase = '/nodes/' + node + '/docker/' + clone['id']
            task(api('create', clonebase + '/action', action='start')); check_kernel(clone)
            task(api('create', clonebase + '/action', action='force-stop'))
            print('PASS ' + node + ' clone retains mandatory policy', flush=True)
            progress['clone'] = True; save()
        if not progress.get('restore'):
            task(api('create', '/nodes/' + node + '/vzdump', vmid=info['vmid'], storage='pbs1', mode='stop'))
            archive = max(api('get', '/nodes/' + node + '/storage/pbs1/content', content='backup', vmid=info['vmid']), key=lambda r: r.get('ctime', 0))['volid']
            saved.setdefault('backups', []).append({'node': node, 'archive': archive}); save()
            vmid = int(api('get', '/cluster/nextid')); name = saved['prefix'] + '-restore-' + node
            task(api('create', '/nodes/' + node + '/docker', archive=archive, newid=vmid,
                     deployment={'name': name, 'bridge': 'vmbr0', 'ipv4mode': 'none'}))
            restored = next(r for r in w.summaries() if r['name'] == name)
            saved['guests'].append(restored['id']); save()
            restorebase = '/nodes/' + node + '/docker/' + restored['id']
            task(api('create', restorebase + '/action', action='start')); check_kernel(restored)
            task(api('create', restorebase + '/action', action='force-stop'))
            print('PASS ' + node + ' PBS archive restore retains mandatory policy', flush=True)
            progress['restore'] = True; save()
        if node == 'pve1' and not progress.get('migration'):
            task(api('create', base + '/migrate', target='pve2', digest=api('get', base + '/management')['digest']))
            moved = dict(info, node='pve2'); destination = '/nodes/pve2/docker/' + info['id']
            task(api('create', destination + '/action', action='start')); check_kernel(moved)
            task(api('create', destination + '/action', action='force-stop'))
            task(api('create', destination + '/migrate', target='pve1', digest=api('get', destination + '/management')['digest']))
            task(api('create', base + '/action', action='start')); check_kernel(info)
            task(api('create', base + '/action', action='force-stop'))
            print('PASS pve1/pve2 migration with snapshot history retains mandatory policy', flush=True)
            progress['migration'] = True; save()
    saved['mutations_complete'] = True; save()


if __name__ == '__main__': main()
