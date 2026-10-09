#!/usr/bin/env python3
"""Traffic acceptance tests on disposable DCTs. Run on the test cluster only.

Retains a manifest after failures so the same fixtures can be checked again or
removed with --cleanup. Changes only recorded guests and firewall options.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid
sys.path.insert(0, '/usr/local/lib/betterdocker')
import provision as worker
import transfer
from live_pools_hooks import api, task, wait_for


def remote(node, args, **kwargs):
    command = args if node == worker.current_node() else transfer.ssh_command(node) + [shlex.join(args)]
    return subprocess.run(command, text=True, capture_output=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default='/root/dct-firewall-acceptance.json')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--from-reload', action='store_true')
    parser.add_argument('--phase', choices=['basic', 'extended', 'lifecycle'], default='basic')
    args = parser.parse_args()
    path = Path(args.manifest)
    state = json.loads(path.read_text()) if path.exists() else {'prefix': 'fwtest-' + uuid.uuid4().hex[:8], 'guests': [],
        'cluster': api('get', '/cluster/firewall/options'), 'node': 'pve3'}
    def save():
        path.write_text(json.dumps(state)); path.chmod(0o600)
    def base(info):
        owner = next(i for i in worker.summaries() if i['id'] == info['id'])['node']
        return f'/nodes/{owner}/docker/{info["id"]}'
    def power(info, action):
        task(api('create', base(info) + '/action', action=action, timeout=10))
    def probe(info, allowed, origin=None, port=18080):
        command = ['wget', '-q', '-T', '3', '-O', '-', f'http://{info["address"]}:{port}/']
        if not origin:
            command.insert(1, '--tries=1')
        if origin:
            ids = remote(origin['node'], ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=bd-' + origin['id'], '--filter', 'label=com.docker.compose.service=app'], check=True).stdout.strip()
            command = ['docker', 'exec', ids, *command]
        result = remote(origin['node'] if origin else worker.current_node(), command, timeout=10)
        assert (result.returncode == 0 and 'firewall-ok' in result.stdout) == allowed, (info['vmid'], allowed, result.returncode, result.stdout, result.stderr)
    save()
    if args.cleanup:
        if state.get('ha_sid') and any(i['sid'] == state['ha_sid'] for i in api('get', '/cluster/ha/resources')):
            api('set', '/cluster/ha/resources/' + state['ha_sid'], state='stopped')
            owned = next(i for i in worker.summaries() if str(i['vmid']) == state['ha_sid'].split(':')[1])
            wait_for(lambda: api('get', base(owned) + '/management')['status'] == 'stopped')
            api('delete', '/cluster/ha/resources/' + state['ha_sid'])
        for info in worker.summaries():
            if info['name'].startswith(state['prefix']):
                for job in api('get', '/cluster/replication'):
                    if str(job['guest']) == str(info['vmid']):
                        api('delete', '/cluster/replication/' + job['id'])
                        remote(info['node'], ['pvesr', 'run', '--id', job['id']], check=True)
                if info.get('replication'):
                    remote(info['node'], ['/usr/local/lib/betterdocker/replica.py', 'cleanup', info['id'], info['node']], check=True)
                power(info, 'force-stop'); task(api('delete', base(info)))
                if info.get('replication'):
                    remote(info['node'], ['/usr/local/lib/betterdocker/provision.py', 'purge-retained-storage', info['id']], check=True)

                assert not Path(f'/etc/pve/firewall/{info["vmid"]}.fw').exists()
        if state.get('user'): api('delete', '/access/users/' + state['user'])
        if state.get('role'): api('delete', '/access/roles/' + state['role'])
        if state.get('group') and any(g['group'] == state['group'] for g in api('get', '/cluster/firewall/groups')):
            group_path = '/cluster/firewall/groups/' + state['group']
            for rule in sorted(api('get', group_path), key=lambda r: r['pos'], reverse=True):
                api('delete', group_path + '/' + str(rule['pos']))
            api('delete', group_path)
        original = state['cluster']; values = {k: original[k] for k in ('enable', 'policy_in', 'policy_out') if k in original}
        missing = [k for k in ('enable', 'policy_in', 'policy_out') if k not in original]
        if missing: values['delete'] = ','.join(missing)
        api('set', '/cluster/firewall/options', **values)
        path.unlink(); print('PASS cleanup', flush=True); return
    if args.phase != 'basic':
        guests = [next(i for i in worker.summaries() if i['id'] == identifier) for identifier in state['guests'][:2]]
        a, b = guests
        def rules(info, entries):
            fw = base(info) + '/firewall'
            for rule in api('get', fw + '/rules')[::-1]:
                api('delete', fw + '/rules/' + str(rule['pos']))
            for entry in entries:
                api('create', fw + '/rules', enable=1, **entry)
        def exec_guest(info, command):
            ids = remote(info['node'], ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=bd-' + info['id'], '--filter', 'label=com.docker.compose.service=app'], check=True).stdout.strip()
            return remote(info['node'], ['docker', 'exec', ids, *command], timeout=10)
        allow = dict(type='in', action='ACCEPT', proto='tcp', dport='18080')
        if args.phase == 'extended':
            power(a, 'start'); power(b, 'start')
            from live_permissions import http
            import secrets
            fw = base(a) + '/firewall'
            if not args.from_reload:
                rules(a, [allow]); rules(b, [allow])
                api('set', fw + '/options', policy_out='DROP')
                assert exec_guest(a, ['ping', '-c', '1', '-W', '2', '192.0.2.40']).returncode != 0
                assert exec_guest(a, ['wget', '-q', '-T', '3', '-O', '-', 'http://internal/']).returncode == 0
                api('create', fw + '/rules', type='out', action='ACCEPT', macro='Ping', enable=1)
                assert exec_guest(a, ['ping', '-c', '1', '-W', '2', '192.0.2.40']).returncode == 0
                print('PASS host access, native Ping macro and internal service traffic', flush=True)
                api('set', fw + '/options', policy_out='ACCEPT')
                for alias in api('get', fw + '/aliases'):
                    if alias['name'] == 'testclient': api('delete', fw + '/aliases/testclient')
                api('create', fw + '/aliases', name='testclient', cidr='192.0.2.28')
                rules(a, [dict(allow, source='testclient')])
                probe(a, True); probe(a, False, b)
                rules(a, [])
                sets = api('get', fw + '/ipset')
                if not any(i['name'] == 'testclients' for i in sets):
                    api('create', fw + '/ipset', name='testclients')
                    api('create', fw + '/ipset/testclients', cidr='192.0.2.28')
                rules(a, [dict(allow, source='+testclients')])
                probe(a, True); probe(a, False, b)
                print('PASS native aliases and IP sets enforce client identity through DNAT', flush=True)
                group = state['prefix'].replace('-', '')
                state['group'] = group; save()
                if not any(g['group'] == group for g in api('get', '/cluster/firewall/groups')):
                    api('create', '/cluster/firewall/groups', group=group)
                    api('create', '/cluster/firewall/groups/' + group, **allow, enable=1)
                rules(a, [dict(type='group', action=group)])
                probe(a, True); probe(a, True, b)
                print('PASS native security group rules', flush=True)
            # A broken manual edit must retain the last applied kernel rules.
            config = Path('/etc/pve/firewall/' + str(a['vmid']) + '.fw')
            original = config.read_text()
            try:
                config.write_text(original + '\n[RULES]\nIN ACCEPT -source not_a_real_alias\n')
                wait_for(lambda: api('get', fw + '/options')['status'] == 'error', timeout=45)
                probe(a, True)
            finally:
                config.write_text(original)
            wait_for(lambda: api('get', fw + '/options')['status'] == 'active', timeout=45)
            print('PASS failed reload retains working rules and recovers', flush=True)
            role = 'FWNet' + state['prefix'][-8:]
            user = state['prefix'] + '@pve'
            state['role'], state['user'] = role, user; save()
            api('create', '/access/roles', roleid=role, privs='VM.Audit VM.Config.Network')
            api('create', '/access/users', userid=user, password=secrets.token_urlsafe(24))
            token = api('create', '/access/users/' + user + '/token/test', privsep=1)
            headers = {'Authorization': 'PVEAPIToken=' + user + '!test=' + token['value']}
            for principal in ({'users': user}, {'tokens': user + '!test'}):
                api('set', '/access/acl', path='/vms/' + str(a['vmid']), roles=role, **principal)
            assert http('GET', fw + '/options', headers=headers)['enable'] == 1
            http('PUT', fw + '/options', {'log_level_in': 'info'}, headers=headers)
            http('GET', base(b) + '/firewall/rules', headers=headers, expected=403)
            http('PUT', base(b) + '/firewall/options', {'enable': 0}, headers=headers, expected=403)
            http('GET', fw + '/log', headers=headers, expected=403)
            api('set', '/access/roles/' + role, privs='VM.Audit')
            http('PUT', fw + '/options', {'enable': 0}, headers=headers, expected=403)
            api('delete', '/access/users/' + user); api('delete', '/access/roles/' + role)
            state.pop('user'); state.pop('role'); save()
            print('PASS network-only separated token, audit writes and cross-DCT denial', flush=True)
            for option in ('dhcp', 'macfilter', 'ipfilter', 'ndp', 'radv', 'ips'):
                try: api('set', fw + '/options', **{option: 1})
                except RuntimeError as error: assert '400' in str(error)
                else: raise AssertionError('Unsupported option accepted: ' + option)
            rules(a, [allow])
            rules(a, [dict(allow, action='DROP', log='info')])
            since = int(time.time()) - 2
            probe(a, False)
            wait_for(lambda: any('DPT=' in row['t'] and 'DROP' in row['t'] for row in api('get', fw + '/log', since=since, limit=1000)), timeout=30)
            rules(a, [allow])
            print('PASS native firewall log contains the blocked published connection', flush=True)
            state['node_options'] = api('get', '/nodes/' + state['node'] + '/firewall/options'); save()
            try:
                for backend in (0, 1):
                    api('set', '/nodes/' + state['node'] + '/firewall/options', nftables=backend)
                    time.sleep(5)
                    probe(a, True)
                    rules(a, [])
                    probe(a, False)
                    rules(a, [allow])
                    probe(a, True); probe(b, True, a)
                    print('PASS native firewall backend', backend, flush=True)
            finally:
                if 'nftables' in state['node_options']:
                    api('set', '/nodes/' + state['node'] + '/firewall/options', nftables=state['node_options']['nftables'])
                else:
                    api('set', '/nodes/' + state['node'] + '/firewall/options', delete='nftables')
            print('PASS extended traffic and permission checks', flush=True)
        else:
            rules(a, [allow]); rules(b, [allow])
            power(a, 'shutdown')
            name = state['prefix'] + '-clone'
            if not any(i['name'] == name for i in worker.summaries()):
                task(api('create', base(a) + '/clone', deployment={'name': name, 'bridge': 'vmbr0', 'cidr': '192.0.2.220/20'}))
            clone = next(i for i in worker.summaries() if i['name'] == name)
            assert api('get', base(clone) + '/firewall/options')['enable'] == 1
            assert api('get', base(clone) + '/network')['firewall'] == 1
            power(clone, 'start'); probe(clone, True)
            rules(clone, []); probe(clone, False); rules(clone, [allow]); probe(clone, True)
            print('PASS clone firewall ownership and enforced policy', flush=True)
            task(api('create', '/nodes/' + a['node'] + '/vzdump', vmid=a['vmid'], storage='pbs1', mode='stop'))
            backups = api('get', '/nodes/' + a['node'] + '/storage/pbs1/content', content='backup', vmid=a['vmid'])
            archive = max(backups, key=lambda i: i.get('ctime', 0))['volid']
            state['archive'] = archive; save()
            name = state['prefix'] + '-restore'
            task(api('create', '/nodes/' + a['node'] + '/docker', archive=archive, storage='local-lvm', deployment={'name': name, 'bridge': 'vmbr0', 'cidr': '192.0.2.219/20'}))
            restored = next(i for i in worker.summaries() if i['name'] == name)
            power(restored, 'start'); probe(restored, True)
            rules(restored, []); probe(restored, False); rules(restored, [allow]); probe(restored, True)
            print('PASS PBS restore restores firewall and actual traffic policy', flush=True)
            task(api('create', base(a) + '/migrate', target='pve2', storage='local-lvm'))
            power(a, 'start'); probe(a, True)
            rules(a, []); probe(a, False); rules(a, [allow]); probe(a, True)
            power(a, 'shutdown')
            task(api('create', base(a) + '/migrate', target=state['node'], storage='local-lvm'))
            power(a, 'start')
            print('PASS cross-node migration preserves and enforces firewall', flush=True)
        return
    source = 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo firewall-ok > /www/index.html; exec httpd -f -p 80 -h /www"]\n    ports: ["18080:80"]\n  internal:\n    image: busybox:1.37\n    init: true\n    command: [sh, -c, "mkdir -p /www; echo firewall-ok > /www/index.html; exec httpd -f -p 80 -h /www"]\n'
    for index, address in enumerate(('192.0.2.222', '192.0.2.221')):
        name = state['prefix'] + '-' + str(index)
        info = next((i for i in worker.summaries() if i['name'] == name), None)
        if not info:
            task(api('create', '/nodes/' + state['node'] + '/docker', deployment=dict(name=name, mode='compose', compose=source, bridge='vmbr0', cidr=address + '/20', firewall=1)))
            info = next(i for i in worker.summaries() if i['name'] == name)
            state['guests'].append(info['id']); save()
        if info['id'] not in state['guests']:
            state['guests'].append(info['id']); save()
        fw = base(info) + '/firewall'
        for rule in api('get', fw + '/rules')[::-1]:
            api('delete', fw + '/rules/' + str(rule['pos']))
        api('set', fw + '/options', enable=1, policy_in='DROP', policy_out='ACCEPT')
        power(info, 'start')
    guests = [next(i for i in worker.summaries() if i['id'] == identifier) for identifier in state['guests'][:2]]
    a, b = guests
    api('set', '/cluster/firewall/options', enable=1, policy_in='ACCEPT', policy_out='ACCEPT')
    time.sleep(4)
    for info in guests:
        status = api('get', base(info) + '/firewall/options')
        assert status['status'] == 'active', status
        probe(info, False)
    print('PASS default ingress deny on both DCTs', flush=True)
    for info in guests:
        api('create', base(info) + '/firewall/rules', type='in', action='ACCEPT', proto='tcp', dport='18080', enable=1)
        probe(info, True)
    print('PASS published-port rule (18080 rather than container port 80)', flush=True)
    probe(b, True, a)
    api('set', base(a) + '/firewall/options', policy_out='DROP')
    probe(b, False, a)
    api('set', base(a) + '/firewall/options', policy_out='ACCEPT')
    api('delete', base(b) + '/firewall/rules/0')
    probe(b, False, a)
    print('PASS both source OUT and destination IN enforced across DCTs', flush=True)
    print('FIXTURES', str(path), flush=True)

if __name__ == '__main__':
    main()
