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
            average = worker.rrddata(ID, 'pve', 'week', 'AVERAGE')
            maximum = worker.rrddata(ID, 'pve', 'week', 'MAX')
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

    def test_chart_ranges_cannot_claim_more_than_the_retained_week(self):
        with patch.object(worker, 'detail'):
            for timeframe in ('month', 'year'):
                with self.assertRaisesRegex(ValueError, 'Invalid chart range'):
                    worker.rrddata(ID, 'pve', timeframe, 'AVERAGE')

    def test_swap_is_read_from_owned_cgroup_and_capped_by_host_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc, cgroup = root / 'proc', root / 'cgroup'
            proc.mkdir(); cgroup.mkdir()
            (proc / 'meminfo').write_text('SwapTotal:       1048576 kB\n')
            (proc / '123').mkdir()
            cid = 'b' * 64
            group = cgroup / ('docker-' + cid + '.scope')
            group.mkdir()
            (proc / '123' / 'cgroup').write_text('0::/docker-' + cid + '.scope\n')
            (group / 'memory.swap.current').write_text(str(32 * 1048576))
            (group / 'memory.swap.max').write_text(str(256 * 1048576))
            containers = [{'id': cid, 'pid': 123, 'state': 'running'}]
            with patch.object(worker, 'PROC', proc), patch.object(worker, 'CGROUP', cgroup):
                self.assertEqual(worker.swap_usage(containers, []), {'swap': 32 * 1048576, 'maxswap': 256 * 1048576})
                (group / 'memory.swap.max').write_text('max')
                self.assertEqual(worker.swap_usage(containers, [])['maxswap'], 1048576 * 1024)
                (proc / '123' / 'cgroup').write_text('0::/another-workload\n')
                self.assertIsNone(worker.swap_usage(containers, [])['swap'])

    def test_bootdisk_reports_logical_root_sizes_and_shared_capacity_with_cache(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)):
            directory = Path(folder) / ID
            directory.mkdir()
            containers = [{'id': 'b' * 64}]
            inspected = json.dumps([{'Id': 'b' * 64, 'SizeRootFs': 12345, 'SizeRw': 123}])
            with patch.object(worker, 'capture', side_effect=['/var/lib/docker', inspected]) as capture, \
                 patch.object(worker.os, 'statvfs', return_value=SimpleNamespace(f_blocks=10000, f_frsize=4096)):
                result = worker.disk_usage(ID, containers)
                self.assertEqual(result, {'disk': 12345, 'maxdisk': 40960000})
                self.assertIn('--size', capture.call_args.args[0])
                self.assertEqual(worker.disk_usage(ID, containers), result)
                self.assertEqual(capture.call_count, 2, 'size scans must be cached between refreshes')
                self.assertEqual((directory / 'disk-usage.json').stat().st_mode & 0o777, 0o600)

    def test_missing_storage_measurements_are_unavailable_instead_of_fake_zero(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(worker, 'capture', side_effect=OSError('unavailable')):
            self.assertEqual(worker.disk_usage(ID, []), {'disk': None, 'maxdisk': None})
