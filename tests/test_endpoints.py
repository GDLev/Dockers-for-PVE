"""Network model contracts and preservation of independently assigned ports."""
import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dctlib import endpoints
from dctlib.endpoint_controls import render

ID = 'ab' * 16


class EndpointsTests(unittest.TestCase):
    def configuration(self):
        return {'net0': {'bridge': 'vmbr0', 'cidr': '192.0.2.10/24'},
                'net1': {'bridge': 'vmbr1', 'cidr': '198.51.100.10/24', 'cidr6': '2001:db8:1::10/64', 'ipv6mode': 'static', 'ports': 'web:8080/tcp'}}

    def test_preserves_assignment_and_deduplicates_previous_dual_stack(self):
        info = {'id': ID, 'interfaces': endpoints.validate(self.configuration())}
        config = {'services': {'web': {'ports': [{'published': '8080', 'target': 80}, {'published': '8080', 'target': 80}]},
                               'api': {'ports': [{'published': '9000', 'target': 90}]}}}
        endpoints.bind_runtime(config, info)
        self.assertEqual([p['host_ip'] for p in config['services']['web']['ports']], ['198.51.100.10', '2001:db8:1::10'])
        self.assertEqual(config['services']['api']['ports'][0]['host_ip'], '192.0.2.10')
        first = copy.deepcopy(config)
        endpoints.bind_runtime(config, info)
        self.assertEqual(config, first)
        self.assertEqual(len(endpoints.port_bindings(config['services']['web']['ports'])['80/tcp']), 2)

    def test_address_and_gateway_boundaries(self):
        for edit in ({'cidr': '192.0.2.10/24'}, {'gw': '203.0.113.1'}, {'cidr6': '::1/128'}, {'rate': 'nan'}, {'tag': 4095}, {'mtu': 1200}, {'ports': 'web:8080/tcp,web:8080/tcp'}):
            value = self.configuration(); value['net1'].update(edit)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                endpoints.validate(value)

    def test_unassigned_port_rejected(self):
        with self.assertRaisesRegex(ValueError, 'assigned published port'):
            endpoints.bind_runtime({'services': {'app': {'ports': []}}}, {'id': ID, 'interfaces': endpoints.validate(self.configuration())})

    def test_disconnect_and_rate_cover_both_families_and_directions(self):
        value = self.configuration(); value['net0']['rate'] = 2; value['net1']['link_down'] = 1
        rules = render([{'id': ID, 'interfaces': endpoints.validate(value)}])
        self.assertIn('ip6 daddr 2001:db8:1::10 drop', rules)
        self.assertIn('ip6 saddr 2001:db8:1::10 drop', rules)
        self.assertIn('ip daddr 192.0.2.10 jump rate_in_0', rules)
        self.assertIn('chain rate_in_0 { limit rate over 2097152 bytes/second', rules)
        self.assertIn('ip saddr 192.0.2.10 jump rate_out_0', rules)

    def test_ipv6_host_exceptions_are_limited_to_published_sockets(self):
        from dctlib.endpoint_controls import host_exceptions
        bindings = [('tcp', 8080, '10.240.0.2', 80, '2001:db8::10')] * 2
        self.assertEqual(host_exceptions(bindings), ['-A BD6-PUBLISHED -d 2001:db8::10/128 -p tcp -m tcp --dport 8080 -j ACCEPT'])
        self.assertEqual(host_exceptions([]), [])

    def test_legacy_net0_is_retained(self):
        info = {'id': ID, 'bridge': 'vmbr0', 'cidr': '192.0.2.10/24', 'hwaddr': '02:00:00:00:00:99'}
        self.assertEqual(endpoints.resolved(info)['net0']['hwaddr'], info['hwaddr'])
        self.assertEqual(endpoints.resolved(info)['net0']['interface'], 'bd' + ID[:12])
