#!/usr/bin/env python3
"""Exercise cluster metadata replacement on exactly owned disposable DCTs."""
import argparse
import hashlib
import json
from pathlib import Path
import selectors
import subprocess
import sys

import live_environment as env
import live_guest_lifecycle as lifecycle
import live_security_acceptance as live
from acceptance_report import Report

sys.path.insert(0, '/usr/local/lib/betterdocker')
import installer

READER = r'''import json,subprocess,sys,time
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib.secure_io import read_json
identifier,node,vmid=sys.argv[1:];vmid=int(vmid)
program=r"""use PVE::API2::BetterDocker;use Time::HiRes qw(usleep);
my($id,$node,$vmid)=@ARGV;
for(1..100){my $v=PVE::API2::BetterDocker::read_guest($id,$node);
die "Identity changed\n" unless $v && $v->{id} eq $id && $v->{node} eq $node && $v->{vmid} == $vmid;usleep(50000);}
print "100\n";"""
print('READY',flush=True);sys.stdin.readline()
child=subprocess.Popen(['perl','-e',program,identifier,node,str(vmid)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
try:
 for _ in range(100):
  value=read_json('/etc/pve/betterdocker/'+identifier+'.json')
  assert (value['id'],value['node'],value['vmid'])==(identifier,node,vmid)
  time.sleep(.03)
 out,err=child.communicate(timeout=30)
 assert child.returncode==0,(out,err)
 assert out.strip()=='100',out
 print(json.dumps(dict(python_reads=100,perl_reads=100)),flush=True)
finally:
 if child.poll() is None:child.terminate();child.communicate(timeout=10)
'''

WRITER = r'''import json,sys,time
sys.path.insert(0,'/usr/local/lib/betterdocker')
from dctlib import projects as worker
from dctlib.secure_io import atomic_text
identifier,node,vmid=sys.argv[1:];vmid=int(vmid)
with worker.locked(identifier):
 path=worker.METADATA/(identifier+'.json');value=worker.read_json(path)
 assert (value['id'],value['node'],value['vmid'])==(identifier,node,vmid)
 payload=json.dumps(value)+'\n'
 for _ in range(100):atomic_text(path,payload,public=True);time.sleep(.05)
print('100')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    assert not Path(args.report).exists(), 'Refuse to overwrite acceptance evidence'
    report = Report(args.report, args.revision,
                    {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    cluster = installer.Cluster()
    children = []
    try:
        for owner in (env.node('primary'), env.node('rootless')):
            item = report.plan_guest(owner, int(live.api('get', '/cluster/nextid')),
                                     'metadata-' + report.data['run'][:8] + '-' + str(len(report.data['fixtures'])))
            compose = {'services': {'app': {'image': 'busybox:1.37', 'pull_policy': 'never',
                       'command': ['sleep', '86400'], 'cpus': .1, 'mem_limit': '32m', 'pids_limit': 16,
                       'labels': {'dev.gdlev.acceptance-run': report.data['run']}}}}
            live.task(live.api('create', '/nodes/' + owner + '/docker', start=0,
                      deployment=dict(vmid=item['vmid'], name=item['name'], mode='compose',
                                      compose=json.dumps(compose), bridge='vmbr0', ipv4mode='none')))
            info = lifecycle.own(report, item)
            assert info
            identity = [info['id'], owner, str(info['vmid'])]
            for node in dict.fromkeys(env.config()['nodes'].values()):
                child = subprocess.Popen(cluster.command(node, ['python3', '-u', '-c', READER, *identity]),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append((node, child))
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    assert selector.select(30), 'Reader did not become ready'
                assert child.stdout.readline().strip() == 'READY'
            for _, child in children:
                child.stdin.write('\n'); child.stdin.flush()
            result = cluster.run(owner, ['python3', '-c', WRITER, *identity], text=True, check=True, timeout=60)
            assert result.stdout.strip() == '100'
            for node, child in children:
                out, err = child.communicate(timeout=90)
                assert child.returncode == 0, (node, out, err)
                assert json.loads(out) == dict(python_reads=100, perl_reads=100)
                report.check('concurrent metadata reads ' + owner + ' from ' + node, 'pass',
                             '100 replacements, 100 Python IPC reads and 100 actual Perl read_guest calls; identity unchanged')
            children.clear()
    except BaseException as error:
        report.check('cluster metadata replacement', 'fail', str(error)[-1800:])
        raise
    finally:
        for _, child in children:
            if child.poll() is None:
                child.terminate(); child.communicate(timeout=15)
        lifecycle.cleanup(report)
        report.finish()
    assert report.data['status'] == 'passed'


if __name__ == '__main__':
    main()
