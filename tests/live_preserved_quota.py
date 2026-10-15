#!/usr/bin/env python3
"""Check writable-history quotas using a separate XFS-backed Docker daemon.

The main Docker daemon, its socket, networks and containers are untouched.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, '/usr/local/lib/betterdocker')
import runtime
from dctlib import projects as w, service_recreate


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs).stdout


def main():
    routes = run(['ip', '-j', 'route', 'show', 'default'])
    daemon = None
    mounted = False
    with tempfile.TemporaryDirectory(prefix='bd-quota-') as folder:
        folder = Path(folder); point = folder / 'xfs'; point.mkdir()
        image = folder / 'disk.raw'
        with image.open('wb') as output:
            output.truncate(2 * 1024**3)
        run(['mkfs.xfs', '-f', str(image)])
        try:
            run(['mount', '-o', 'loop,prjquota', str(image), str(point)]); mounted = True
            archive = folder / 'busybox.tar'
            runtime.run(['docker', 'image', 'save', '-o', str(archive), 'busybox:1.37'], check=True)
            socket = folder / 'docker.sock'
            with (folder / 'daemon.log').open('w') as log:
                daemon = subprocess.Popen(['dockerd', '--host=unix://' + str(socket), '--data-root=' + str(point / 'docker'),
                    '--exec-root=' + str(folder / 'run'), '--pidfile=' + str(folder / 'pid'), '--storage-driver=overlay2',
                    '--feature=containerd-snapshotter=false', '--bridge=none', '--iptables=false', '--ip6tables=false',
                    '--ip-forward=false', '--ip-masq=false'], stdout=log, stderr=subprocess.STDOUT)
            selected = runtime.Runtime(socket=str(socket), data=str(point / 'docker'))
            with patch.object(runtime, 'load', return_value=selected), patch.object(w, 'ROOT', folder / 'projects'), patch.object(w, 'METADATA', folder / 'metadata'):
                deadline = time.monotonic() + 30
                while not socket.exists():
                    if daemon.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError((folder / 'daemon.log').read_text()[-3000:])
                    time.sleep(.1)
                w.run(['docker', 'load', '-i', str(archive)], timeout=120, capture_output=True)
                identifier = uuid.uuid4().hex
                directory = w.ROOT / identifier; directory.mkdir(parents=True)
                base = json.loads(w.capture(['docker', 'image', 'inspect', 'busybox:1.37']))[0]['Id']
                config = {'services': {'app': {'image': 'busybox:1.37', 'storage_opt': {'size': '32m'}, 'command': ['sleep', '86400']}}}
                w.write_json(directory / 'compose.runtime.json', config)
                cid = w.capture(['docker', 'run', '-d', '--name', 'quota-app', '--network', 'none', '--storage-opt', 'size=32m',
                    '--label', 'betterdocker.id=' + identifier, '--label', 'com.docker.compose.service=app', '--label', 'com.docker.compose.image=' + base,
                    'busybox:1.37', 'sleep', '86400']).strip()
                try:
                    w.run(['docker', 'exec', cid, 'dd', 'if=/dev/zero', 'of=/retained', 'bs=1M', 'count=12'], capture_output=True)
                    w.run(['docker', 'stop', '--time', '0', cid], capture_output=True)
                    service_recreate.replace({'id': identifier}, 'app', config, {}, {})
                    cid = w.container_records(identifier)[0]['id']
                    inspected = json.loads(w.capture(['docker', 'inspect', cid]))[0]
                    charged = int(inspected['Config']['Labels']['betterdocker.preserved-bytes'])
                    remaining = int(inspected['HostConfig']['StorageOpt']['size'])
                    assert charged >= 12 * 1048576
                    assert charged + remaining == 32 * 1048576
                    w.run(['docker', 'start', cid], capture_output=True)
                    result = w.run(['docker', 'exec', cid, 'dd', 'if=/dev/zero', 'of=/new', 'bs=1M', 'count=22'], check=False, capture_output=True)
                    assert result.returncode and 'No space left' in result.stderr, result.stderr
                    assert int(w.capture(['docker', 'exec', cid, 'stat', '-c', '%s', '/retained']).strip()) == 12 * 1048576
                    print('PASS: preserved 12 MiB remains charged; the new writable layer cannot exceed the 32 MiB total budget', flush=True)
                finally:
                    for row in w.container_records(identifier):
                        w.run(['docker', 'rm', '-f', row['id']], capture_output=True)
        finally:
            if daemon:
                daemon.terminate(); daemon.wait(timeout=30)
            if mounted:
                run(['umount', str(point)])
            assert run(['ip', '-j', 'route', 'show', 'default']) == routes
            print('PASS: isolated daemon removed and original node routes unchanged', flush=True)


if __name__ == '__main__':
    main()
