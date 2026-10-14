"""Data-preserving edits, quotas, dependency readiness and response limits."""
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_worker import worker as w
from dctlib import capacity, disks, engine_config, ipv6_dynamic, log_output, service_start
from dctlib.deployment import escape_literals
from dctlib.endpoints import port_bindings

ID = 'ab' * 16
IMAGE = 'sha256:' + 'a' * 64


class ReleaseFeatures(unittest.TestCase):
    def test_edit_rejects_unmapped_hooks_instead_of_silently_dropping_them(self):
        image = {'Id': IMAGE, 'Config': {}}
        for key in ('post_start', 'pre_stop'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                engine_config.create_body({key: [{'command': ['true']}]}, image, ID, 'app', 1, {'default': 'owned'}, {})

    def test_literal_dollars_survive_runtime_serialization(self):
        self.assertEqual(escape_literals({'command': ['sh', '-c', 'echo $HOME ${SECRET}']}),
                         {'command': ['sh', '-c', 'echo $$HOME $${SECRET}']})

    def test_restore_accepts_ipv6_only_and_regenerates_clone_macs(self):
        import guest_state
        request = {'name': 'source', 'mode': 'compose', 'compose': 'services: {}', 'bridge': 'vmbr0',
            'address': '192.0.2.1', 'cidr': '192.0.2.1/24', 'ipv4mode': 'static', 'hwaddr': '02:11:22:33:44:55',
            'interfaces': {'net0': {'bridge': 'vmbr0', 'cidr': '192.0.2.1/24', 'ipv4mode': 'static',
                'gw': '192.0.2.254', 'hwaddr': '02:11:22:33:44:55', 'ipv6mode': 'static', 'cidr6': 'fd00::1/64', 'gw6': 'fd00::ffff'}}}
        info = dict(request, id=ID)
        clone = guest_state.restore_request(request, info, {'ipv4mode': 'none', 'ipv6mode': 'auto'}, 'cd' * 16, ID, 941)
        self.assertNotEqual(clone['interfaces']['net0']['hwaddr'], request['interfaces']['net0']['hwaddr'])
        self.assertEqual(clone['interfaces']['net0']['gw'], '')
        self.assertEqual(clone['interfaces']['net0']['gw6'], '')
        validated = w.validate(clone)
        self.assertEqual(validated['interfaces']['net0']['ipv6mode'], 'auto')
        self.assertEqual(request['interfaces']['net0']['ipv6mode'], 'static')
        moved = guest_state.restore_request(request, info, {'cidr': '192.0.2.1/24'}, ID, ID, 940)
        self.assertEqual(moved['interfaces']['net0']['hwaddr'], '02:11:22:33:44:55')

    def test_restore_accepts_addressless_guests_without_published_ports(self):
        import guest_state
        request = {'name': 'source', 'bridge': 'vmbr0', 'ipv4mode': 'static'}
        result = guest_state.restore_request(request, dict(request, id=ID), {'ipv4mode': 'none', 'ipv6mode': 'none'}, 'cd' * 16, ID, 941)
        self.assertEqual(result['cidr'], '')
        self.assertEqual(result['interfaces']['net0']['ipv6mode'], 'none')

    def test_port_range_is_selected_per_replica_by_docker(self):
        self.assertEqual(port_bindings([{'published': '8000-8010', 'target': 80, 'host_ip': '192.0.2.1'}]),
                         {'80/tcp': [{'HostIp': '192.0.2.1', 'HostPort': '8000-8010'}]})
        with self.assertRaises(ValueError):
            port_bindings([{'published': '8000-8010', 'target': '80-81', 'host_ip': '192.0.2.1'}])

    def test_static_ipv6_accepts_a_link_local_router(self):
        from dctlib.endpoints import validate
        values = validate({'net0': {'bridge': 'vmbr0', 'ipv4mode': 'none', 'ipv6mode': 'static',
            'cidr6': 'fd00::2/64', 'gw6': 'fe80::1'}})
        self.assertEqual(values['net0']['gw6'], 'fe80::1')

    def test_edit_body_keeps_image_defaults_and_applies_compose_options(self):
        service = {'image': 'busybox', 'environment': {'NEW': 'value'}, 'command': ['sleep', '5'],
            'stop_grace_period': '2m3s', 'networks': {'private': {'aliases': ['app'], 'ipv6_address': 'fd00::2', 'driver_opts': {'key': 'value'}}},
            'healthcheck': {'test': ['CMD', 'true'], 'interval': '1.5s'}, 'expose': ['9090/udp'],
            'volumes': [{'type': 'volume', 'source': 'state', 'target': '/data'}]}
        config = {'volumes': {'state': {'name': 'bd-' + ID + '_state'}}}
        image = {'Id': IMAGE, 'Config': {'Env': ['OLD=kept'], 'User': '1000', 'Entrypoint': ['entry'], 'Labels': {'base': 'label'}}}
        with patch.object(w.node_runtime, 'load', return_value=Mock(rootless=False)):
            body = engine_config.create_body(service, image, ID, 'web', 2, {'private': 'bd-private'}, config)
        self.assertEqual(body['User'], '1000')
        self.assertEqual(body['Entrypoint'], ['entry'])
        self.assertEqual(body['Env'], ['OLD=kept', 'NEW=value'])
        self.assertEqual(body['StopTimeout'], 123)
        self.assertEqual(body['Healthcheck']['Interval'], 1500000000)
        self.assertIn('9090/udp', body['ExposedPorts'])
        self.assertEqual(body['Labels']['com.docker.compose.container-number'], '2')
        self.assertEqual(len(body['Labels']['com.docker.compose.config-hash']), 64)
        self.assertEqual(body['HostConfig']['Mounts'][0]['Source'], 'bd-' + ID + '_state')
        self.assertEqual(body['NetworkingConfig']['EndpointsConfig']['bd-private']['IPAMConfig'], {'IPv6Address': 'fd00::2'})

    def test_local_secrets_remain_read_only(self):
        config = {'secrets': {'password': {'file': '/owned/password'}}}
        mounts = engine_config.mounts({'secrets': ['password']}, ID, config)
        self.assertEqual(mounts, [{'Type': 'bind', 'Source': '/owned/password', 'Target': '/run/secrets/password', 'ReadOnly': True}])
        with self.assertRaises(ValueError):
            engine_config.mounts({'secrets': ['password']}, ID, {'secrets': {'password': {'external': True}}})

    def test_image_index_creation_keeps_the_selected_platform_identity(self):
        index = 'sha256:' + 'b' * 64
        image = {'Id': IMAGE, '_create_image': index, 'Config': {'User': '1000'}}
        with patch.object(w.node_runtime, 'load', return_value=Mock(rootless=False)):
            body = engine_config.create_body({}, image, ID, 'app', 1, {'default': 'owned'}, {})
        self.assertEqual(body['Image'], index)
        self.assertEqual(body['Labels']['com.docker.compose.image'], IMAGE)
        self.assertEqual(body['User'], '1000')

    def test_preserved_history_reduces_next_writable_budget(self):
        row = {'Image': IMAGE, 'SizeRw': 10 * 1048576,
               'Config': {'Labels': {'betterdocker.preserved-bytes': str(20 * 1048576)}}}
        body = {'Labels': {}, 'HostConfig': {'StorageOpt': {'size': '100m'}}}
        capacity.preserve_quota(row, body)
        self.assertEqual(body['Labels']['betterdocker.preserved-bytes'], str(30 * 1048576))
        self.assertEqual(w.disk_mib(body['HostConfig']['StorageOpt']['size']), 70)
        row['SizeRw'] = 80 * 1048576
        with self.assertRaisesRegex(ValueError, 'Increase'):
            capacity.preserve_quota(row, {'Labels': {}, 'HostConfig': {'StorageOpt': {'size': '100m'}}})

    def test_unmeasurable_writable_layer_cannot_bypass_quota(self):
        row = {'Image': IMAGE, 'Config': {'Labels': {}}}
        with self.assertRaisesRegex(ValueError, 'measure'):
            capacity.preserve_quota(row, {'Labels': {}, 'HostConfig': {'StorageOpt': {'size': '100m'}}})

    def test_free_space_keeps_operation_reserve(self):
        with patch.object(capacity.shutil, 'disk_usage', return_value=Mock(free=capacity.RESERVE + 100)):
            capacity.require_space(Path.cwd(), 100)
            with self.assertRaisesRegex(ValueError, 'Insufficient'):
                capacity.require_space(Path.cwd(), 101)

    def test_all_owned_native_disks_are_included_once(self):
        self.assertEqual(disks.volume_ids({'volid': 'local:root', 'additional': {'mp1': {'volid': 'local:data'}},
                                         'unused': ['local:old', 'local:data']}), ['local:root', 'local:data', 'local:old'])
        for target in ('/', '/proc/foo', '/sys', '/dev/x', '/../etc', 'relative'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                disks.validate({'target': target, 'size_mib': 256})

    def test_dependency_cycles_and_missing_required_services_fail(self):
        with self.assertRaisesRegex(ValueError, 'Circular'):
            service_start.order({'a': {'depends_on': {'b': {}}}, 'b': {'depends_on': {'a': {}}}})
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            service_start.order({'a': {'depends_on': {'missing': {}}}})
        self.assertEqual(service_start.order({'a': {'depends_on': {'missing': {'required': False}}}}), ['a'])
        self.assertEqual(service_start.order({'app': {'volumes_from': ['data:ro']}, 'data': {}}), ['data', 'app'])

    def test_start_waits_for_dependency_health_before_application(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(w, 'ROOT', Path(folder)):
            directory = Path(folder) / ID; directory.mkdir()
            w.write_json(directory / 'compose.runtime.json', {'services': {
                'app': {'depends_on': {'db': {'condition': 'service_healthy'}}}, 'db': {}}})
            rows = [{'id': 'app', 'service': 'app', 'state': 'exited'}, {'id': 'db', 'service': 'db', 'state': 'exited'}]
            ready = [{'State': {'Running': True, 'Health': {'Status': 'starting'}}}]
            healthy = [{'State': {'Running': True, 'Health': {'Status': 'healthy'}}}]
            calls = []
            with patch.object(w, 'container_records', return_value=rows), patch.object(w, 'capture', side_effect=[json.dumps(ready), json.dumps(healthy)]), \
                    patch.object(w, 'run', side_effect=lambda args, **_: calls.append(args)), patch.object(service_start.time, 'sleep'):
                service_start.start(ID)
            self.assertEqual(calls, [['docker', 'start', 'db'], ['docker', 'start', 'app']])

    def test_ra_prefix_uses_guest_mac_and_bounds_lifetimes(self):
        header = b'\x86\x00' + bytes(14)
        prefix = bytes.fromhex('fd000000000000000000000000000000')
        option = bytes([3, 4, 64, 0x40]) + struct.pack('!II', 600, 300) + bytes(4) + prefix
        value = ipv6_dynamic.router_prefix(header + option, '02:11:22:33:44:55')
        self.assertEqual(value['cidr6'], 'fd00::11:22ff:fe33:4455/64')
        self.assertLessEqual(value['preferred'], value['expires'])
        self.assertIsNone(ipv6_dynamic.router_prefix(header + bytes([3, 0]), '02:11:22:33:44:55'))
        self.assertIsNone(ipv6_dynamic.router_prefix(header + option[:-1], '02:11:22:33:44:55'))

    def test_real_log_writer_is_bounded_before_reading(self):
        with patch.object(w, 'run', side_effect=subprocess.run):
            output = log_output.bounded([sys.executable, '-c', 'import os; os.write(1,b"x"*3000000)'])
        self.assertLessEqual(len(output.encode()), log_output.MAX_BYTES)
        self.assertIn('truncated', output)

    def test_invalid_utf8_cannot_expand_log_response_past_limit(self):
        def write(command, **kwargs):
            kwargs['stdout'].write(b'\xff' * log_output.MAX_BYTES)
            return Mock(returncode=0)
        with patch.object(w, 'run', side_effect=write):
            output = log_output.bounded(['fake'])
        self.assertLessEqual(len(output.encode()), log_output.MAX_BYTES)
        self.assertLessEqual(len(json.dumps({'data': output})), log_output.MAX_BYTES)

    def test_unrelated_log_reader_failure_is_reported(self):
        with patch.object(w, 'run', return_value=Mock(returncode=-15)):
            with self.assertRaisesRegex(ValueError, 'Cannot read'):
                log_output.bounded(['fake'])

    def test_native_host_ipv6_controls_are_scoped_and_checked_before_apply(self):
        from dctlib import ipv6_controls
        import firewall
        state = {'nftables': [{'chain': {'name': 'input'}}, {'chain': {'name': 'output'}}]}
        with patch.object(ipv6_controls.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(state))), \
                patch.object(firewall, 'command') as commands:
            ipv6_controls.ensure({'interface': 'bdowned'})
        self.assertEqual(commands.call_args_list[0].args[0], ['nft', '--check', '-f', '-'])
        self.assertEqual(commands.call_args_list[1].args[0], ['nft', '-f', '-'])
        content = commands.call_args_list[0].kwargs['input']
        self.assertIn('iifname "bdowned"', content)
        self.assertIn('ip6 hoplimit 255', content)
        self.assertIn('udp sport 547 udp dport 546', content)

    def test_native_host_ipv6_controls_refuse_foreign_chain_contents(self):
        from dctlib import ipv6_controls
        state = {'nftables': [{'rule': {'chain': ipv6_controls.TAG + '-input', 'comment': 'administrator', 'handle': 1}}]}
        with patch.object(ipv6_controls.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(state))):
            with self.assertRaisesRegex(ValueError, 'administrator'):
                ipv6_controls.ensure({'interface': 'bdowned'})
