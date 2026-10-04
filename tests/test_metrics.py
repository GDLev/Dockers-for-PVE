"""Native chart measurements, rate calculation, retention and private notes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker

ID = 'a' * 32


class MetricsTests(unittest.TestCase):
    def test_snapshot_uses_real_usage_and_combined_service_capacity(self):
        stats = [{'CPUPerc': '50.00%', 'MemUsage': '64MiB / 512MiB',
                  'NetIO': '1MB / 2kB', 'BlockIO': '3MiB / 0B'}]
        containers = [{'state': 'running', 'started': '2026-10-04T00:00:00.123456789Z', 'id': 'b' * 64}]
        with patch.object(worker.os, 'cpu_count', return_value=8):
            snapshot = worker.metric_snapshot(containers, stats, [{'settings': {'cpus': 2, 'memory': 512}, 'replicas': 2}])
        self.assertEqual(snapshot['cpu'], 0.125)
        self.assertEqual(snapshot['mem'], 64 * 1048576)
        self.assertEqual(snapshot['maxmem'], 1024 * 1048576)
        self.assertEqual(snapshot['netin_total'], 1000000)
        self.assertEqual(snapshot['diskread_total'], 3 * 1048576)
        self.assertGreater(snapshot['uptime'], 0)

    def test_rates_do_not_turn_container_recreation_into_a_spike(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)):
            directory = Path(folder) / ID
            directory.mkdir()
            sample = {'time': 1000, 'cpu': 0.1, 'mem': 5, 'maxmem': 10, 'instances': 'container-a',
                      **{key + '_total': 100 for key in ('netin', 'netout', 'diskread', 'diskwrite')}}
            worker.record_metrics({'id': ID, 'metrics': sample})
            worker.record_metrics({'id': ID, 'metrics': dict(sample, time=1060, netin_total=700)})
            history = json.loads((directory / 'metrics.json').read_text())
            self.assertEqual(history[-1]['netin'], 10)
            worker.record_metrics({'id': ID, 'metrics': dict(sample, time=1120, instances='container-b', netin_total=50000)})
            history = json.loads((directory / 'metrics.json').read_text())
            self.assertEqual(history[-1]['netin'], 0)
            self.assertEqual((directory / 'metrics.json').stat().st_mode & 0o777, 0o600)

    def test_native_chart_average_and_max_are_distinct_and_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'detail'), patch.object(worker.time, 'time', return_value=3000000):
            directory = Path(folder) / ID
            directory.mkdir()
            sample = {'time': 3000000 - 60, 'cpu': 0.1, 'mem': 5, 'maxmem': 10, 'instances': '',
                      **{key + '_total': 0 for key in ('netin', 'netout', 'diskread', 'diskwrite')}}
            worker.record_metrics({'id': ID, 'metrics': sample})
            worker.record_metrics({'id': ID, 'metrics': dict(sample, time=3000000, cpu=0.9)})
            average = worker.rrddata(ID, 'pve', 'month', 'AVERAGE')
            maximum = worker.rrddata(ID, 'pve', 'month', 'MAX')
            self.assertAlmostEqual(average[0]['cpu'], 0.5)
            self.assertEqual(maximum[0]['cpu'], 0.9)
            worker.record_metrics({'id': ID, 'metrics': dict(sample, time=3000000 + 8 * 86400)})
            self.assertEqual(len(json.loads((directory / 'metrics.json').read_text())), 1)

    def test_notes_are_saved_privately_without_redeployment(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), patch.object(worker, 'detail'):
            directory = Path(folder) / ID
            directory.mkdir()
            self.assertEqual(worker.notes(ID, 'pve')['description'], '')
            worker.notes(ID, 'pve', '**Application notes**')
            self.assertEqual(worker.notes(ID, 'pve')['description'], '**Application notes**')
            self.assertEqual((directory / 'notes.txt').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                worker.notes(ID, 'pve', 'x' * 8193)
