"""Hold and restore native HA configuration during explicit installer acceptance."""
import live_environment as env
from live_firewall import remote
from live_pools_hooks import api

NODES = (env.node('primary'), env.node('secondary'), env.node('rootless'))
DROPIN = '/etc/systemd/system/{unit}.service.d/betterdocker-installer-test.conf'
CONDITION = '/run/betterdocker-installer-test-allow'


def hold(state):
    for node in NODES:
        for unit in ('pve-ha-crm', 'pve-ha-lrm'):
            script = 'from pathlib import Path;p=Path(' + repr(DROPIN.format(unit=unit)) + ');assert not p.exists();p.parent.mkdir(parents=True,exist_ok=True);p.write_text(' + repr('[Unit]\nConditionPathExists=' + CONDITION + '\n') + ')'
            remote(node, ['python3', '-c', script], check=True)
        remote(node, ['systemctl', 'daemon-reload'], check=True)
        remote(node, ['systemctl', 'stop', 'pve-ha-crm', 'pve-ha-lrm'], check=True, timeout=180)
    for resource in state['ha']:
        api('delete', '/cluster/ha/resources/' + resource['sid'], purge=0)
    for job in state['replication']:
        api('delete', '/cluster/replication/' + job['id'], force=1)


def restore(state):
    jobs = api('get', '/cluster/replication')
    for job in state['replication']:
        if not any(i['id'] == job['id'] for i in jobs):
            api('create', '/cluster/replication', **{k:v for k,v in job.items() if k in ('id','type','target','schedule','rate','disable')})
    resources = api('get', '/cluster/ha/resources')
    for resource in state['ha']:
        if not any(i['sid'] == resource['sid'] for i in resources):
            api('create', '/cluster/ha/resources', **{k:v for k,v in resource.items() if k in ('sid','state','group','comment','failback','max_restart','max_relocate')})
    for node in NODES:
        for unit in ('pve-ha-crm', 'pve-ha-lrm'):
            remote(node, ['rm', '-f', DROPIN.format(unit=unit)], check=True)
        remote(node, ['systemctl', 'daemon-reload'], check=True)
        remote(node, ['systemctl', 'start', 'pve-ha-crm', 'pve-ha-lrm'], check=True)
    print('PASS original HA/replication configuration and HA managers restored', flush=True)

