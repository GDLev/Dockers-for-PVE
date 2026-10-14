#!/usr/bin/env python3
"""Disposable native PVE checks for RRD, data ownership and permanent deletion."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from live_project_limits import call, resources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cidr', required=True)
    parser.add_argument('--vmid-start', type=int, default=900)
    args = parser.parse_args()
    fixture_names = {'data-management-test', 'owned-data-delete-test'}
    baseline = {int(row['vmid']) for row in resources() if row.get('name') not in fixture_names}
    identifier = None
    sys.path.insert(0, '/usr/local/lib/betterdocker')
    from dctlib import projects as worker
    occupied = baseline | set(worker.reserved_ids())
    vmid = next(value for value in range(args.vmid_start, min(args.vmid_start + 50, 1000)) if value not in occupied)
    started = int(time.time())
    with tempfile.TemporaryDirectory(prefix='bd-data-test-') as tmp:
        source = Path(tmp) / 'compose.json'
        source.write_text(json.dumps({'services': {'app': {'image': 'busybox:1.37',
            'command': ['sh', '-c', 'while :; do echo sample >> /data/sample; sleep 1; done'],
            'volumes': ['data:/data'], 'cpus': .1, 'mem_limit': '16m', 'pids_limit': 32}}, 'volumes': {'data': {}}}))
        try:
            call('dct', 'create', vmid, source, '--hostname', 'data-management-test', '--net0', 'bridge=vmbr0,ip=' + args.cidr, '--start', 1)
            row = next(row for row in resources() if int(row['vmid']) == vmid)
            identifier = row['id'].split('/')[-1]; node = row['node']
            base = f'/nodes/{node}/docker/{identifier}'
            project = worker.ROOT / identifier
            owned = call('docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier).split()
            assert len(owned) == 1
            call('/usr/local/lib/betterdocker/provision.py', 'refresh')
            info = json.loads(call('pvesh', 'get', base + '/management', '--output-format', 'json'))
            assert info['metrics']['mem'] > 0 and info['metrics']['disk'] > 0
            for range_ in ('hour', 'day', 'week', 'month', 'year', 'decade'):
                for cf in ('AVERAGE', 'MAX'):
                    chart = json.loads(call('pvesh', 'get', base + '/rrddata', '--timeframe', range_, '--cf', cf, '--output-format', 'json'))
                    assert isinstance(chart, list)
            assert (Path('/var/lib/rrdcached/db/pve-vm-9.0') / str(vmid)).exists() or (Path('/var/lib/rrdcached/db/pve2.3-vm') / str(vmid)).exists()
            # A complete minute bucket must contain a real measurement, not only timestamps.
            for _ in range(12):
                chart = json.loads(call('pvesh', 'get', base + '/rrddata', '--timeframe', 'hour', '--cf', 'AVERAGE', '--output-format', 'json'))
                if any(row['time'] >= started and isinstance(row.get('mem'), (int, float)) and row['mem'] > 0 for row in chart):
                    break
                call('/usr/local/lib/betterdocker/provision.py', 'refresh')
                time.sleep(15)
            else:
                raise AssertionError('No numeric native RRD memory sample: ' + str(chart[-3:]))
            print('PASS native RRD measurements and all chart ranges', flush=True)
            call('dct', 'stop', vmid)
            call('dct', 'set', vmid, '--protection', 1)
            error = call('dct', 'destroy', vmid, '--destroy-owned-data', 1, fail=True)
            assert 'protection' in error.lower(), error
            call('dct', 'set', vmid, '--protection', 0)
            call('dct', 'snapshot', vmid, 'before-delete')
            call('dct', 'destroy', vmid, '--destroy-owned-data', 1, fail=True)
            assert project.exists()
            call('dct', 'delsnapshot', vmid, 'before-delete')
            # A local native data disk, then an independently owned unused disk.
            storage_rows = json.loads(call('pvesh', 'get', f'/nodes/{node}/storage', '--output-format', 'json'))
            storage = next(row['storage'] for row in storage_rows if row.get('type') in ('dir', 'lvmthin', 'zfspool') and row.get('active') and 'rootdir' in row.get('content', ''))
            upid = response_json(call('pvesh', 'set', base + '/storage-limit', '--size_mib', 256, '--storage', storage, '--output-format', 'json'))
            wait_task(node, upid)
            settings = worker.storage_config(identifier)
            current = settings['volid']
            extra = worker.pve_storage(identifier, 'allocate', storage=storage, size_mib=256)
            settings['unused'] = [extra['volid']]; worker.write_json(project / 'storage.json', settings)
            (project / 'allocated-volume.json').unlink()
            route = base + '/volumes/' + str(vmid)
            call('pvesh', 'delete', route, '--volume', current, fail=True)
            call('dct', 'set', vmid, '--protection', 1)
            upid = response_json(call('pvesh', 'delete', route, '--volume', extra['volid'], '--output-format', 'json'))
            wait_task(node, upid, fail=True)
            call('dct', 'set', vmid, '--protection', 0)
            upid = response_json(call('pvesh', 'delete', route, '--volume', extra['volid'], '--output-format', 'json'))
            wait_task(node, upid)
            assert extra['volid'] not in worker.storage_config(identifier).get('unused', [])
            content = json.loads(call('pvesh', 'get', f'/nodes/{node}/storage/{storage}/content', '--dct', 1, '--output-format', 'json'))
            entry = next(row for row in content if row['volid'] == current)
            assert entry['dct_id'] == identifier and entry['dct_node'] == node and entry['dct_status'] == 'in-use'
            call('dct', 'destroy', vmid)
            assert project.exists() and worker.storage_config(identifier)['volid'] == current
            upid = response_json(call('pvesh', 'delete', route, '--volume', current, '--output-format', 'json'))
            wait_task(node, upid)
            assert not (worker.METADATA / 'ids' / (str(vmid) + '.json')).exists()
            # Retained deployment files remain. Remove this fixture's private directory only.
            import shutil
            shutil.rmtree(project)
            identifier = None
            call('dct', 'create', vmid, source, '--hostname', 'owned-data-delete-test', '--net0', 'bridge=vmbr0,ip=' + args.cidr, '--start', 1)
            identifier = next(row for row in resources() if int(row['vmid']) == vmid)['id'].split('/')[-1]
            project = worker.ROOT / identifier
            volumes = call('docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=bd-' + identifier).split()
            call('dct', 'stop', vmid)
            call('dct', 'destroy', vmid, '--destroy-owned-data', 1)
            assert not project.exists()
            for volume in volumes: call('docker', 'volume', 'inspect', volume, fail=True)
            identifier = None
            print('PASS native RRD ranges, protection, snapshot guards, attached disk refusal, unused/retained cleanup, storage navigation fields and owned data deletion', flush=True)
        finally:
            if identifier and (worker.METADATA / (identifier + '.json')).exists():
                call('dct', 'stop', vmid)
                call('dct', 'set', vmid, '--protection', 0)
                call('dct', 'destroy', vmid)
    assert {int(row['vmid']) for row in resources() if row.get('name') not in fixture_names} == baseline
    print('PASS original guest inventory preserved', flush=True)


def response_json(output):
    # Local pvesh workers can print progress before returning the JSON task ID.
    for line in reversed(output.splitlines()):
        try: return json.loads(line)
        except json.JSONDecodeError: pass
    raise ValueError('No JSON response: ' + output)


def wait_task(node, upid, fail=False):
    for _ in range(120):
        result = json.loads(call('pvesh', 'get', f'/nodes/{node}/tasks/{upid}/status', '--output-format', 'json'))
        if result['status'] == 'stopped':
            assert (result['exitstatus'] != 'OK') if fail else (result['exitstatus'] == 'OK'), result
            return
        time.sleep(1)
    raise RuntimeError('Native task timeout')


if __name__ == '__main__': main()
