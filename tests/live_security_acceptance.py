#!/usr/bin/env python3
"""Bounded traffic on registered DCTs plus read-only PVE security evidence.

Never changes cluster/node firewall options, runtime modes, HA or quorum.
No raw packets, spoofing, host-service stops, stress tests or reboot.
"""
import argparse
import ipaddress
import json
import hashlib
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import time

sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
import transfer
from acceptance_report import Report

SOURCE = Path(__file__).resolve().parents[1]


def api(method, route, **values):
    args = ['pvesh', method, route, '--output-format', 'json']
    for key, value in values.items():
        args += ['--' + key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1500:])
    raw = result.stdout.strip()
    if not raw: return None
    try: return json.loads(raw)
    except json.JSONDecodeError: return json.loads(raw.splitlines()[-1])


def task(upid):
    matches=re.findall(r'UPID:[^\"\s]+:',upid)
    if len(matches)!=1:raise ValueError('Unexpected task response')
    upid=matches[0]
    node = upid.split(':')[1]
    end = time.monotonic() + 240
    while time.monotonic() < end:
        status = api('get', '/nodes/' + node + '/tasks/' + upid + '/status')
        if status['status'] == 'stopped':
            if status.get('exitstatus') != 'OK':
                log = api('get', '/nodes/' + node + '/tasks/' + upid + '/log', limit=50)
                detail = '\n'.join(row.get('t', '') for row in log)
                raise RuntimeError('Task failed: ' + upid + '\n' + detail[-1800:])
            return
        time.sleep(.5)
    raise TimeoutError('Fixture task did not finish; retain ownership journal')


def remote(node, args, **kwargs):
    if args[0] == 'docker': args = ['/usr/local/lib/betterdocker/runtime.py', 'docker', *args[1:]]
    command = args if node == worker.current_node() else transfer.ssh_command(node) + [shlex.join(args)]
    checked=kwargs.pop('check',False)
    result=subprocess.run(command,capture_output=True,text=True,timeout=kwargs.pop('timeout',60),**kwargs)
    if checked and result.returncode:raise RuntimeError(node+': '+result.stderr.strip()[-1800:])
    return result


def resources():
    return sorted([{k: r.get(k) for k in ('vmid', 'node', 'name', 'type', 'status')} for r in api('get', '/cluster/resources', type='vm')], key=lambda r: r['vmid'])


NODE_STATE = '''import sys,json,hashlib,subprocess
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/betterdocker')
import runtime,firewall,host
from dctlib import projects as w,firewall_reload as reload
r=runtime.load()
control={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/etc/betterdocker').glob('*.json')}
configuration={}
for p in w.METADATA.glob('*.json'):
 info=w.read_json(p)
 if info.get('node')!=w.current_node():continue
 root=w.ROOT/info['id']
 configuration[info['id']]={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('request.json','compose.source.yaml','compose.runtime.json','.env','security-policy.json','limits.json','pending.json','storage.json') if (root/name).is_file()}
fw=Path('/etc/pve/firewall')
files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in fw.glob('*.fw')}
rows=[json.loads(line) for line in runtime.run(['docker','ps','-a','--format','json'],capture_output=True,text=True,check=True).stdout.splitlines()]
containers=sorted([{k:row.get(k) for k in ('ID','Names','State')} for row in rows],key=lambda row:row['ID'])
services={name:subprocess.run(['systemctl','is-active',name],capture_output=True,text=True).stdout.strip() for name in ('pvedaemon','pveproxy','pve-firewall','proxmox-firewall','betterdocker-firewall','betterdocker-addresses')}
print(json.dumps(dict(mode=r.mode,pve=subprocess.check_output(['pveversion'],text=True).strip(),kernel=subprocess.check_output(['uname','-r'],text=True).strip(),docker=json.loads(runtime.run(['docker','version','--format','{{json .Server}}'],capture_output=True,text=True,check=True).stdout),control=control,configuration=configuration,firewall_files=files,containers=containers,services=services)))
'''

NODE_CHECK = '''import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/betterdocker')
import firewall as f,host,runtime
from dctlib import firewall_reload as r,projects as w
source=Path(sys.argv[1]);manifest=json.loads(Path(sys.argv[2]).read_text())
import hashlib
for name,digest in manifest.items():
 p=source/name
 assert hashlib.sha256(p.read_bytes()).hexdigest()==digest,name+' source mismatch'
 if name.startswith('scripts/') and (name.endswith('.py') or name.endswith('.sh')):
  live=Path('/usr/local/lib/betterdocker')/name.removeprefix('scripts/')
  if live.exists():assert live.read_bytes()==p.read_bytes(),name+' installed mismatch'
 if name.startswith('backend/PVE/') and name.endswith('.pm'):
  live=Path('/usr/share/perl5/PVE')/name.removeprefix('backend/PVE/')
  assert live.read_bytes()==p.read_bytes(),name+' installed mismatch'
for p in Path('/usr/local/lib/betterdocker').rglob('*'):
 st=p.lstat();assert st.st_uid==0 and not st.st_mode&0o022 and not p.is_symlink(),str(p)
status=f.read_json(f.STATE/'status.json');assert not status.get('error'),status
selected=runtime.load()
if not selected.rootless:
 assert Path('/etc/apparmor.d/betterdocker').read_bytes()==(source/'assets/betterdocker.apparmor').read_bytes(),'installed AppArmor profile mismatch'
def check(table,cache,prefix=(),fds=()):
 rules=f.read_json_text(f.STATE/cache)
 assert r.present(table,rules,prefix,fds),table+' generation missing'
 code=('delete table inet '+table+'\\n')+r.stamped(rules)
 subprocess.run(list(prefix)+['nft','--check','-f','-'],input=code,text=True,capture_output=True,pass_fds=fds,check=True)
check('betterdocker','applied.nft');check('betterdocker_endpoints','endpoints.nft')
if selected.rootless:
 check('betterdocker_rootless','rootless.nft')
 with host.namespace() as (_,_,prefix,fds):check('betterdocker','inner.nft',prefix,fds)
subprocess.run(['python3',str(source/'tests/check_native.py'),'/'],stdout=subprocess.DEVNULL,check=True)
subprocess.run(['perl','-MPVE::API2::Nodes','-e','PVE::API2::Nodes->validate_method_schemas(1)'],check=True)
print('installed hashes, ownership, kernel generations, nft check, native integration and API schemas')
'''


def state(node):
    return json.loads(remote(node, ['python3', '-c', NODE_STATE], check=True).stdout)


def own(report, item):
    info = next((row for row in worker.summaries() if row.get('vmid') == item['vmid']), None)
    if info is None: return None
    if any(info.get(key)!=item[key] for key in ('node','vmid','name')):
        raise ValueError('Refusing to touch an unregistered fixture identity')
    marker_code='''import sys,json
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as w
request=w.read_json(w.ROOT/w.project_id(sys.argv[1])/'request.json')
services=json.loads(request['compose'])['services']
print(all(s.get('labels',{}).get('betterdocker.acceptance-run')==sys.argv[2] for s in services.values()))
'''
    marker=remote(info['node'],['python3','-c',marker_code,info['id'],report.data['run']],check=True).stdout.strip()
    if marker!='True':raise ValueError('Fixture does not carry this run ownership marker')
    report.confirm_guest(item, info)
    return info


def base(info):
    return '/nodes/' + info['node'] + '/docker/' + info['id']


def power(info, action):
    task(api('create', base(info) + '/action', action=action, timeout=15))


def rows(info):
    ids = remote(info['node'], ['docker', 'ps', '-aq', '--filter', 'label=betterdocker.id=' + info['id']], check=True).stdout.split()
    if not ids: raise RuntimeError('Fixture has no containers')
    return json.loads(remote(info['node'], ['docker', 'inspect', *ids], check=True).stdout)


def container(info, service):
    return next(row for row in rows(info) if row['Config']['Labels']['com.docker.compose.service'] == service)


def probe(info, address, marker, origin=None, expect=True):
    url = 'http://' + ('[' + address + ']' if ':' in address else address) + ':18087/'
    if origin:
        cid = container(origin, 'app')['Id']
        command = ['docker', 'exec', cid, 'wget', '-q', '-T', '2', '-O', '-', url]
        result = remote(origin['node'], command, timeout=8)
    else:
        result = remote(info['node'], ['curl', '--noproxy', '*', '--connect-timeout', '2', '--max-time', '3', '-fsS', url], timeout=8)
    if expect and not origin and result.returncode:
        for attempt in range(4):
            time.sleep(.5)
            result=remote(info['node'], ['curl','--noproxy','*','--connect-timeout','2','--max-time','3','-fsS',url],timeout=8)
            if not result.returncode:break
    matched = result.returncode == 0 and result.stdout.strip() == marker
    if matched != expect: raise AssertionError('Unexpected published HTTP result: ' + str((info['vmid'], address, expect, result.returncode, result.stdout[:100], result.stderr[-150:])))


def wait_generation(info):
    end = time.monotonic() + 25
    while time.monotonic() < end:
        result = remote(info['node'], ['python3', '/usr/local/lib/betterdocker/firewall.py', 'verify', info['id']])
        if not result.returncode: return
        time.sleep(.5)
    raise RuntimeError('Fixture firewall did not reach its current generation')


def wait_idle(info):
    code = "import sys,json;sys.path.insert(0,'/usr/local/lib/betterdocker');from dctlib import projects as w;print(json.dumps(w.operation_state(sys.argv[1])))"
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        current = json.loads(remote(info['node'], ['python3', '-c', code, info['id']], check=True).stdout)
        if not current.get('active'): return
        time.sleep(1)
    raise TimeoutError('Owned fixture operation is still active; keep the journal')


def cleanup(report):
    for item in report.data['fixtures']:
        if item['removed']: continue
        info = own(report, item)
        if info:
            wait_idle(info)
            power(info, 'shutdown')
            task(api('delete', base(info), **{'destroy-owned-data': 1}))
        if own(report, item) is not None: raise RuntimeError('Owned fixture remains')
        item['removed'] = True; report.save()


def traffic(report, node, address, index):
    runtime = report.data['environments'][node]['mode']
    marker = 'acceptance-' + report.data['run']
    guests = []
    for offset in range(2):
        name = 'acceptance7-' + report.data['run'][:8] + '-' + node + '-' + str(offset)
        vmid = int(api('get', '/cluster/nextid'))
        item = report.plan_guest(node, vmid, name)
        ip = str(ipaddress.IPv4Address(address) + offset)
        ipv6 = 'fd47:bd:7:' + str(index) + '::' + str(offset + 10)
        compose = {'services': {service: dict(image='busybox:1.37', pull_policy='never', init=True,
                    command=['sh', '-c', 'mkdir -p /www; printf %s ' + marker + ' > /www/index.html; exec httpd -f -p 80 -h /www'],
                    cpus=.25, mem_limit='64m', pids_limit=64, read_only=True, tmpfs=['/www:rw,size=1m'],
                    labels={'betterdocker.acceptance-run': report.data['run']},
                    **({'ports': ['18087:80']} if service == 'app' else {})) for service in ('app', 'internal')}}
        deployment = dict(vmid=vmid, name=name, mode='compose', compose=json.dumps(compose),
                          interfaces={'net0':dict(bridge='vmbr0', ipv4mode='static', cidr=ip+'/20', ipv6mode='static', cidr6=ipv6+'/64', firewall=1)})
        item['create_task'] = api('create', '/nodes/'+node+'/docker', deployment=deployment, start=0)
        report.save()
        task(item['create_task'])
        info = own(report, item); assert info
        management = api('get', base(info)+'/management')
        task(api('set', base(info)+'/security-policy', mode='hardened', digest=management['digest']))
        power(info, 'start'); wait_generation(info)
        app = container(info, 'app')
        report.data.setdefault('observations', []).append(dict(node=node, vmid=vmid, apparmor=app['AppArmorProfile'], state=app['State']['Status'], exit_code=app['State']['ExitCode'], networks=app['NetworkSettings']['Networks']))
        report.save()
        probe(info, ip, marker); probe(info, ipv6, marker)
        status = remote(node, ['docker','exec',app['Id'],'cat','/proc/1/status'],check=True).stdout
        fields = dict(line.split(':',1) for line in status.splitlines() if ':' in line)
        assert fields['NoNewPrivs'].strip()=='1' and fields['Seccomp'].strip()=='2'
        assert 'ALL' in app['HostConfig']['CapDrop']
        assert app['AppArmorProfile'] in (('', 'unconfined') if runtime=='rootless' else ('betterdocker-default',))
        internal = remote(node, ['docker','exec',app['Id'],'wget','-q','-T','2','-O','-','http://internal/'],check=True).stdout.strip()
        assert internal == marker
        guests.append((info,ip,ipv6))
    a,ip4,ip6 = guests[0]; b,b4,b6=guests[1]
    probe(b,b4,marker,origin=a)
    report.check(node+' published IPv4/IPv6, same-DCT DNS and cross-DCT published HTTP', 'pass', dict(mode=runtime, guests=[g[0]['vmid'] for g in guests]))
    report.check(node+' enforcing container policy', 'pass', 'NoNewPrivs=1, Seccomp=2, ALL capability drop; mode-specific AppArmor profile inspected')
    private = next(iter(container(b,'internal')['NetworkSettings']['Networks'].values()))['IPAddress']
    app = container(a,'app')
    result = remote(node,['docker','exec',app['Id'],'wget','-q','-T','2','-O','-','http://'+private+'/'],timeout=8)
    report.check(node+' cross-DCT private address isolation', 'pass' if result.returncode else 'fail', dict(target=private, returncode=result.returncode))
    if runtime=='rootless':
        result=remote(node,['curl','--noproxy','*','--connect-timeout','2','--max-time','3','-k','-sS','https://['+ip6+']:8006/'],timeout=8)
        if not result.returncode: raise AssertionError('Rootless dedicated endpoint exposed wildcard PVE HTTPS service')
        report.check(node+' rootless endpoint host-service floor', 'pass', 'Published IPv6 HTTP succeeds; TCP connection to PVE wildcard service through fixture IPv6 endpoint is blocked')
    network=api('get',base(a)+'/network')
    task(api('set',base(a)+'/network',net='net0',link_down=1,digest=network['digest']))
    # Network settings are applied on the next start, matching native pending behavior.
    power(a,'shutdown');power(a,'start');wait_generation(a)
    probe(a,ip4,marker,expect=False);probe(a,ip6,marker,expect=False)
    network=api('get',base(a)+'/network')
    task(api('set',base(a)+'/network',net='net0',link_down=0,digest=network['digest']))
    power(a,'shutdown');power(a,'start');wait_generation(a)
    probe(a,ip4,marker);probe(a,ip6,marker)
    report.check(node+' Disconnect and reconnect both address families', 'pass', 'Only fixture net0 changed; other guests and cluster options untouched')


def verify_baseline(report):
    expected = report.data['baseline']
    if not expected: raise ValueError('Baseline was not recorded; restoration cannot be confirmed')
    after=dict(resources=resources(),cluster_firewall=api('get','/cluster/firewall/options'),nodes={node:state(node) for node in expected['nodes']},node_firewall={node:api('get','/nodes/'+node+'/firewall/options') for node in expected['nodes']})
    for key in ('resources','cluster_firewall','node_firewall'):assert after[key]==expected[key],key+' changed'
    for node in expected['nodes']:
        for key in ('mode','control','configuration','firewall_files','containers','services'):
            assert after['nodes'][node][key]==expected['nodes'][node][key],node+' '+key+' changed'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--source-manifest',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--rootful',default='pve1');parser.add_argument('--rootless',default='pve3')
    parser.add_argument('--rootful-address',default='192.0.2.245');parser.add_argument('--rootless-address',default='192.0.2.247')
    parser.add_argument('--cleanup',action='store_true')
    parser.add_argument('--traffic-mode', choices=('both', 'rootful', 'rootless'), default='both', help='Repeat traffic only for an affected runtime; omitted traffic remains unverified in this report')
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run on the test PVE node as root')
    if args.cleanup:
        report = Report.__new__(Report); report.path = args.report
        report.data = json.loads(args.report.read_text())
        try:
            cleanup(report)
            verify_baseline(report)
            report.check('recovery cleanup and baseline preservation', 'pass', 'Owned fixtures removed; original state compared with saved baseline')
        except BaseException as error:
            report.check('recovery cleanup and baseline preservation', 'fail', str(error)[-2000:])
            raise
        finally:
            report.finish()
        return
    if args.report.exists():raise SystemExit('Report exists; inspect it or use --cleanup')
    manifest=json.loads(args.source_manifest.read_text())
    report=Report(args.report,args.revision,manifest)
    nodes=[row['node'] for row in api('get','/nodes') if row['status']=='online']
    report.data['working_tree']=True
    report.data['baseline']=dict(resources=resources(),cluster_firewall=api('get','/cluster/firewall/options'),nodes={node:state(node) for node in nodes},node_firewall={node:api('get','/nodes/'+node+'/firewall/options') for node in nodes})
    report.save()
    try:
        for node in nodes:
            report.data['environments'][node]=report.data['baseline']['nodes'][node]
            report.save()
            result=remote(node,['python3','-c',NODE_CHECK,str(SOURCE),str(args.source_manifest)],check=True)
            report.check(node+' installed read-only acceptance','pass',result.stdout.strip())
            suite_code='import sys,runpy;sys.path.insert(0,"/usr/local/lib/betterdocker");import dctlib.projects, runtime, guest_state;sys.path.insert(0,'+repr(str(SOURCE/'tests'))+');runpy.run_path('+repr(str(SOURCE/'tests/check_release_ci.py'))+',run_name="__main__")'
            result=remote(node,['python3','-c',suite_code],timeout=120)
            log=args.report.parent/(node+'-contracts.log');log.write_text(result.stdout+result.stderr);log.chmod(0o600)
            if result.returncode:raise RuntimeError(node+' required fixture checks failed; see '+str(log))
            report.check(node+' required fixture suite','pass',dict(log=log.name,sha256=hashlib.sha256(log.read_bytes()).hexdigest(),skips=0))
        for mode,node,address,index in (('rootful',args.rootful,args.rootful_address,1),('rootless',args.rootless,args.rootless_address,2)):
            if args.traffic_mode in ('both', mode):
                traffic(report,node,address,index)
            else:
                report.check(mode+' live traffic', 'not_tested', 'Not repeated in this run; retain the separate runtime report')
        if not report.data['baseline']['cluster_firewall'].get('enable'):
            report.check('native guest ACCEPT/DROP live enforcement','not_tested','Cluster firewall disabled; global settings are outside this test scope')
        report.check('VLAN and SDN live paths','not_tested','No dedicated VLAN/SDN fixture infrastructure; covered by policy fixtures only')
        report.check('both native firewall backends with real traffic','not_tested','Read-only checks and both adapter fixtures; no global backend switch')
        report.check('host outage, quorum loss and reboot','not_tested','Modeled failures only; existing host services remain running')
        report.check('independent manual security review','not_tested','Requires an independent reviewer; this run is not that review')
    except BaseException as error:
        report.check('acceptance execution','fail',str(error)[-2000:])
        raise
    finally:
        try:
            cleanup(report)
            verify_baseline(report)
            report.check('cleanup and baseline preservation','pass','Exact guest IDs/names/nodes/statuses, container IDs/states, saved configurations, runtime mode, control files, native firewall options and service states preserved')
        except BaseException as error:
            report.check('cleanup and baseline preservation','fail',str(error)[-2000:])
        report.finish()
    if report.data['status']=='failed':raise SystemExit(1)


if __name__=='__main__':main()
