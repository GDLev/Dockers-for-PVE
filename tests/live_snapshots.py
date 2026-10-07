"""Run on the approved test PVE host only; creates an isolated disposable DCT."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid


def main():
    spec = importlib.util.spec_from_file_location('worker', '/usr/local/lib/betterdocker/provision.py')
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    node = w.current_node()
    identifier = uuid.uuid4().hex
    name = 'snapshot-test-' + identifier[:8]
    folder = w.ROOT / identifier
    payload = {'name': name, 'mode': 'compose', 'bridge': 'vmbr0', 'ipv4mode': 'static', 'address': '192.0.2.240', 'cidr': '192.0.2.240/20', 'environment': 'VERSION=original\n',
               'compose': json.dumps({'services': {
                   'app': {'image': 'busybox:1.37', 'command': ['sh', '-c', "trap 'exit 0' TERM; while true; do sleep 1; done"],
                           'restart': 'no', 'volumes': ['./data:/data', 'records:/records'], 'deploy': {'replicas': 2}},
                   'side': {'image': 'busybox:1.37', 'command': ['sh', '-c', "trap 'exit 0' TERM; while true; do sleep 1; done"], 'restart': 'no'}},
                   'volumes': {'records': {}}})}
    state_file = Path('/root/betterdocker-ui18/snapshot-live-state.json')
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    existing = next((item for item in w.summaries() if item['id'] == previous.get('id') and item['name'] == previous.get('name') and item['name'].startswith('snapshot-test-')), None)
    if existing:
        w.action(existing['id'], node, 'delete')
    preferred = w.capture(['pvesh', 'get', '/cluster/nextid']).strip()
    try:
        w.capture(['perl', '-I/usr/share/perl5', '-MPVE::API2::BetterDocker', '-e', 'PVE::Cluster::cfs_update(); PVE::API2::BetterDocker::reserve_guest_id($ARGV[0], $ARGV[1], $ARGV[2]);', identifier, node, preferred])
    except subprocess.CalledProcessError as error:
        print(error.stderr, flush=True)
        raise
    state_file.write_text(json.dumps({'id': identifier, 'name': name, 'node': node}))
    try:
        with w.operation(identifier, 'create'):
            w.create(w.validate(payload), node, identifier)
    except Exception:
        if not (w.METADATA / (identifier + '.json')).exists():
            w.release_id(identifier)
        raise
    info = w.detail(identifier, node)
    info.pop('deployment', None)
    Path('/root/betterdocker-ui18/snapshot-live-state.json').write_text(json.dumps(info))
    print('TEST_DCT', info['vmid'], identifier, flush=True)


    def containers():
        return w.container_records(identifier)


    def command(verb, *arguments):
        with w.operation(identifier, verb):
            if verb == 'snapshot':
                w.project_snapshot(identifier, node, *arguments)
            elif verb == 'storage':
                w.project_storage_limit(identifier, node, *arguments)
            else:
                w.action(identifier, node, verb)


    original = {}
    for row in containers():
        content = 'original-' + row['name']
        original[row['name']] = content
        w.run(['docker', 'exec', row['id'], 'sh', '-c', 'printf "%s" "$1" > /root-state', 'sh', content])
        if row['service'] == 'app':
            w.run(['docker', 'exec', row['id'], 'sh', '-c', 'echo original-bind > /data/value; echo original-volume > /records/value'])
    identities = {row['name']: row['id'] for row in containers()}
    command('snapshot', 'create', 'live')
    assert all(row['state'] == 'running' and row['id'] == identities[row['name']] for row in containers())
    command('snapshot', 'delete', 'live')
    print('PASS live snapshot freezes/resumes without recreating services', flush=True)
    command('stop')
    (folder / 'settings.json').write_text(json.dumps({'app': dict(cpus=0.5, memory=0, swap=0, cpuunits=0, pids=0, dns=[], dns_search=[], restart='no')}))
    command('snapshot', 'create', 'before', 'Before test changes')
    # Every replica gets a different filesystem state, and must recover its own one.
    command('start')
    for row in containers():
        w.run(['docker', 'exec', row['id'], 'sh', '-c', 'echo changed > /root-state; if [ -d /data ]; then echo changed > /data/value; echo changed > /records/value; touch /data/new-file; fi'])
    (folder / '.env').write_text('VERSION=changed\n')
    (folder / 'compose.source.yaml').write_text((folder / 'compose.source.yaml').read_text() + '# changed\n')
    (folder / 'settings.json').write_text('{}')
    command('snapshot', 'restore', 'before')
    assert (folder / '.env').read_text() == 'VERSION=original\n'
    assert '# changed' not in (folder / 'compose.source.yaml').read_text()
    assert json.loads((folder / 'settings.json').read_text())['app']['cpus'] == 0.5
    assert all(row['state'] == 'created' for row in containers())
    assert {row['name'] for row in w.snapshot_list(identifier, node)} == {'before', 'current'}
    command('start')
    for row in containers():
        assert w.capture(['docker', 'exec', row['id'], 'cat', '/root-state']).strip() == original[row['name']]
        if row['service'] == 'app':
            assert w.capture(['docker', 'exec', row['id'], 'cat', '/data/value']).strip() == 'original-bind'
            assert w.capture(['docker', 'exec', row['id'], 'cat', '/records/value']).strip() == 'original-volume'
            w.run(['docker', 'exec', row['id'], 'test', '!', '-e', '/data/new-file'])
    command('restart')
    for row in containers():
        assert w.capture(['docker', 'exec', row['id'], 'cat', '/root-state']).strip() == original[row['name']]
    command('stop')
    print('PASS configuration, settings, per-replica rootfs, bind, volume, no automatic snapshot, start/restart', flush=True)
    command('storage', '256')
    command('snapshot', 'create', 'bounded')
    config = w.storage_config(identifier)
    for path in config['mapping'].values():
        (Path(path) / 'value').write_text('changed-on-data-disk')
    command('storage', '512')
    command('snapshot', 'restore', 'bounded')
    assert w.storage_config(identifier)['size_mib'] == 256
    for path in w.storage_config(identifier)['mapping'].values():
        assert (Path(path) / 'value').read_text().strip().startswith('original-')
    print('PASS bounded data disk rollback and restored snapshot capacity', flush=True)
    command('snapshot', 'restore', 'before')
    assert not w.storage_config(identifier)
    assert not list(folder.glob('storage-rollback-*.ext4'))
    command('start')
    for row in containers():
        if row['service'] == 'app':
            assert w.capture(['docker', 'exec', row['id'], 'cat', '/data/value']).strip() == 'original-bind'
    command('stop')
    print('PASS rollback across data disk migration', flush=True)
    # Integrity failure must not destroy current state or create a recovery snapshot.
    archive = w.snapshot_root(identifier) / 'bounded' / 'configuration' / '.env'
    archive.write_text('corrupted')
    before_ids = [row['id'] for row in containers()]
    try:
        command('snapshot', 'restore', 'bounded')
        raise AssertionError('Corrupt snapshot accepted')
    except ValueError as error:
        assert 'integrity' in str(error)
    assert before_ids == [row['id'] for row in containers()]
    command('snapshot', 'delete', 'bounded')
    assert not (w.snapshot_root(identifier) / 'bounded').exists()
    print('PASS corruption rejection and snapshot deletion', flush=True)
    print('SNAPSHOT_LIVE_TESTS_OK; DCT stopped for browser verification', flush=True)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Destructive snapshot verification on an explicitly approved test PVE host only.')
    parser.add_argument('--run-on-test-host', action='store_true', required=True)
    parser.parse_args()
    main()
