#!/usr/bin/env python3
"""Verify an unavailable PBS target cannot change the source DCT."""
import live_environment as env
import json
from pathlib import Path
import uuid
from live_service_security import api, call, w


def main():
    node = w.current_node()
    suffix = uuid.uuid4().hex[:8]
    storage = 'bd-unavailable-' + suffix
    identifier = None
    # Storage creation probes PBS, so write a deliberately offline fixture under
    # the native storage lock. No real PBS configuration or credential is changed.
    program = f'use PVE::Storage; use PVE::Tools; use Storable qw(dclone);\nPVE::Storage::lock_storage_config(sub {{\n my $cfg=PVE::Storage::config(); my $id=$ARGV[0]; die "fixture exists" if $cfg->{{ids}}->{{$id}};\n my $copy=dclone($cfg->{{ids}}->{{{env.backup_storage()}}});\n $copy->{{server}}="127.0.0.1"; $copy->{{port}}=1; $copy->{{username}}="fixture\\@pbs";\n $copy->{{nodes}}={{$ARGV[1]=>1}}; delete $copy->{{disable}};\n $cfg->{{ids}}->{{$id}}=$copy;\n PVE::Tools::file_set_contents("/etc/pve/priv/storage/$id.pw", "invalid-test-credential\\n", 0600, 1);\n PVE::Storage::write_config($cfg);\n}});'
    call('perl', '-e', program, storage, node)
    try:
        deployment = {'name': 'pbs-failure-' + suffix, 'mode': 'compose', 'bridge': 'vmbr0', 'ipv4mode': 'none',
            'compose': 'services:\n  app:\n    image: busybox:1.37\n    init: true\n    command: [sleep, "86400"]\n'}
        api('create', '/nodes/' + node + '/docker', deployment=json.dumps(deployment), start=1)
        info = next(row for row in w.summaries() if row['name'] == deployment['name'])
        identifier = info['id']
        cid = w.container_records(identifier)[0]['id']
        call('docker', 'exec', cid, 'sh', '-c', 'echo kept >/marker')
        result = call('vzdump', info['vmid'], '--storage', storage, '--mode', 'snapshot', fail=True)
        assert 'backup failed' in result.lower() or 'unable' in result.lower() or 'connection refused' in result.lower(), result
        assert w.container_records(identifier)[0]['state'] == 'running'
        assert call('docker', 'exec', cid, 'cat', '/marker').strip() == 'kept'
        assert not (w.ROOT / identifier / 'export-transaction.json').exists()
        assert w.detail(identifier, node)['node'] == node
        print('PASS unavailable PBS backup preserves source state, ownership and writable files', flush=True)
    finally:
        if identifier:
            api('create', '/nodes/' + node + '/docker/' + identifier + '/action', action='force-stop')
            api('delete', '/nodes/' + node + '/docker/' + identifier, **{'destroy-owned-data': 1})
        call('pvesh', 'delete', '/storage/' + storage)
        print('PASS disposable DCT and unavailable storage entry removed', flush=True)


if __name__ == '__main__':
    main()
