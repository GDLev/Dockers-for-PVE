"""Migrated snapshots use destination networks without old daemon IP assignments."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker
import migration_history as history

ID = 'ac' * 16


class HistoryTests(unittest.TestCase):
    def test_anonymous_history_volume_requires_an_actual_guest_claim(self):
        name = 'ab' * 32
        manifest = {'mounts': [{'type': 'volume', 'volume': name}]}
        rows = [{'Config': {'Labels': {'betterdocker.id': ID}}, 'Mounts': [{'Type': 'volume', 'Name': name}]}]
        mapped = history.volume_names(ID, manifest, rows)[name]
        self.assertTrue(mapped.startswith('bd-' + ID + '_history_'))
        self.assertEqual(history.volume_names(ID, manifest, rows)[name], mapped)
        for rows in ([], [{'Config': {'Labels': {'betterdocker.id': 'ac' * 15 + 'aa'}}, 'Mounts': [{'Type': 'volume', 'Name': name}]}]):
            with self.assertRaisesRegex(ValueError, 'Unowned'): history.volume_names(ID, manifest, rows)

    def test_rehomes_container_and_compose_network_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary); (folder / 'configuration').mkdir()
            old, new = 'bd-' + ID + '_default', 'bd-' + ID + '_restored_0'
            worker.write_json(folder / 'networks.json', [{'Name': old, 'Id': 'old-id', 'IPAM': {'Config': [{'Subnet': '172.18.0.0/16'}]}, 'Labels': {'com.docker.compose.network': 'default'}}])
            worker.write_json(folder / 'containers.json', [{'HostConfig': {'NetworkMode': old}, 'NetworkSettings': {'Networks': {old: {'Aliases': ['app'], 'IPAMConfig': {'IPv4Address': '172.18.0.2'}, 'NetworkID': 'old-id'}}}}])
            worker.write_json(folder / 'configuration/compose.runtime.json', {'networks': {'default': {'name': old}}})
            worker.write_json(folder / 'configuration/request.json', {'mode': 'compose', 'compose': 'old'})
            current = [{'Name': new, 'Labels': {'com.docker.compose.network': 'default'}}]
            with patch.object(worker, 'capture', side_effect=['new-id', json.dumps(current)]):
                history.rehome_networks(ID, folder)
            network = json.loads((folder / 'networks.json').read_text())[0]
            self.assertEqual(network['Name'], new)
            self.assertNotIn('IPAM', network)
            container = json.loads((folder / 'containers.json').read_text())[0]
            self.assertEqual(container['HostConfig']['NetworkMode'], new)
            self.assertEqual(container['NetworkSettings']['Networks'], {new: {'Aliases': ['app']}})
            request = json.loads((folder / 'configuration/request.json').read_text())
            self.assertEqual(json.loads(request['compose'])['networks']['default']['name'], new)
            self.assertEqual(json.loads((folder / 'checksums.json').read_text()), worker.snapshot_digest(folder))
