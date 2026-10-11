"""Binding changes retain committed filesystems and recover after container removal."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from test_worker import worker
from dctlib import rebind

ID = 'ab' * 16


class RebindTests(unittest.TestCase):
    def test_commits_before_removing_and_recreates_preserved_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); directory = root / ID; directory.mkdir()
            worker.write_json(directory / 'network-reconcile.json', {'address': '192.0.2.2'})
            worker.write_json(directory / 'compose.runtime.json', {'services': {'web': {'ports': [{'published': '8080', 'target': 80, 'host_ip': '192.0.2.2'}]}}})
            row = {'Id': 'old', 'Name': '/owned-web', 'Config': {'Image': 'original', 'Labels': {'com.docker.compose.service': 'web'}},
                   'HostConfig': {'Binds': []}, 'Mounts': [{'Type': 'volume', 'Name': 'owned-anonymous', 'Destination': '/data', 'RW': True}],
                   'NetworkSettings': {'Networks': {'owned': {'Aliases': ['old', 'web']}}}}
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return Mock(returncode=0, stdout=json.dumps([row]))
            with patch.object(worker, 'ROOT', root), patch.object(worker, 'require_stopped'), \
                    patch.object(worker, 'container_records', return_value=[]), patch.object(worker, 'snapshot_containers', return_value=[row]), \
                    patch.object(worker, 'capture', side_effect=lambda command: calls.append(command) or 'preserved-image'), \
                    patch.object(worker, 'run', side_effect=run), patch.object(worker.node_runtime, 'load', return_value=Mock(rootless=False)), \
                    patch.object(worker, 'storage_config', return_value={}), patch.object(worker, 'docker_engine', return_value={'Id': 'new'}) as create:
                rebind.reconcile({'id': ID, 'address': '192.0.2.2'})
            self.assertLess(next(i for i,c in enumerate(calls) if c[:2] == ['docker', 'commit']), next(i for i,c in enumerate(calls) if c[:2] == ['docker', 'rm']))
            body = create.call_args.args[2]
            self.assertEqual(body['Image'], 'preserved-image')
            self.assertEqual(body['HostConfig']['PortBindings']['80/tcp'][0]['HostIp'], '192.0.2.2')
            self.assertEqual(body['HostConfig']['Mounts'][0]['Source'], 'owned-anonymous')
            self.assertFalse((directory / 'network-reconcile.json').exists())

    def test_managed_mount_replaces_both_old_mount_forms(self):
        host = {'Binds': ['/old:/data:rw', '/keep:/config:ro'],
                'Mounts': [{'Type': 'volume', 'Source': 'old', 'Target': '/data'}]}
        rebind.storage_bindings(host, {'volumes': [{'type': 'bind', 'source': '/disk/data-0', 'target': '/data'}]},
            {'mapping': {'bind:/old': '/disk/data-0'}})
        self.assertEqual(host['Binds'], ['/keep:/config:ro', '/disk/data-0:/data:rw'])
        self.assertEqual(host['Mounts'], [])
