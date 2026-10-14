import tempfile
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch
from test_worker import module

firewall = module('firewall')


class FirewallTests(unittest.TestCase):
    def test_boot_cache_lock_is_available_while_waiting_for_daemon(self):
        with tempfile.TemporaryDirectory() as folder:
            def wait_for_daemon():
                code = 'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import firewall;firewall.STATE=Path(sys.argv[2]);\nwith firewall.lock(): print("cache lock available")'
                result = subprocess.run([sys.executable, '-c', code, str(Path(firewall.__file__).parent), folder],
                                        capture_output=True, text=True, check=True, timeout=2)
                self.assertIn('cache lock available', result.stdout)
            with patch.object(firewall, 'STATE', Path(folder)), \
                    patch.object(firewall.runtime, 'load', return_value=SimpleNamespace(rootless=True)), \
                    patch.dict(sys.modules, provision=SimpleNamespace(METADATA=Path(folder) / 'unavailable'),
                               host=SimpleNamespace(ready=wait_for_daemon)):
                with self.assertRaisesRegex(ValueError, 'configuration is unavailable'):
                    firewall.reconcile()

    def test_ipset_exclusions_and_more_specific_inclusions(self):
        entries = ['create test hash:net family inet', 'add test 10.0.0.0/24',
                   'add test 10.0.0.0/25 nomatch', 'add test 10.0.0.7/32']
        self.assertEqual(firewall.set_members(entries), ['10.0.0.7/32', '10.0.0.128/25'])

    def test_original_endpoint_ports_keep_the_transport_protocol(self):
        with patch.object(firewall, 'command', return_value="nft 'add rule ip filter d100in tcp dport 8080 counter accept'"):
            rule = firewall.translate('-A d100in -p tcp --dport 8080 -j ACCEPT', {}, original=True)
        self.assertEqual(rule, 'meta l4proto tcp ct original proto-dst 8080 counter accept')

    def test_sets_and_negation_are_preserved(self):
        with patch.object(firewall, 'command', return_value='nft add rule ip filter d100in ip saddr != 198.18.0.1 counter drop') as run:
            rule = firewall.translate('-A d100in -m set ! --match-set allow src -j DROP', {'allow': 'allowed'})
        self.assertIn('ip saddr != @allowed', rule)
        self.assertIn('!', run.call_args.args[0])

    def test_untranslated_rule_is_an_error(self):
        with patch.object(firewall, 'command', return_value='nft # unsupported rule'):
            with self.assertRaisesRegex(ValueError, 'no supported'):
                firewall.translate('-A d100in -j DROP', {})

    def test_both_policies_run_for_cross_guest_traffic(self):
        infos = [{'id': 'a', 'vmid': 100, 'address': '192.0.2.210', 'firewall': 1},
                 {'id': 'b', 'vmid': 101, 'address': '192.0.2.211', 'firewall': 1}]
        config = {'cluster_enabled': True, 'guests': [
            {'vmid': vmid, 'options': {'enable': 1}, 'sets': {}, 'rules': {'in': [], 'out': []}}
            for vmid in (100, 101)]}
        rules = firewall.render(infos, config, {'a': ['br-a'], 'b': ['br-b']},
                                {'a': [('tcp', 8080, '172.20.0.2', 80)]})
        self.assertIn('hook forward priority -10', rules)
        self.assertIn('hook forward priority 0', rules)
        self.assertIn('iifname "br-a" jump d100out', rules)
        self.assertIn('oifname "br-b" jump d101in', rules)
        self.assertNotIn('iifname { "br-a" } oifname { "br-b" } return', rules)
        self.assertIn('iifname { "br-a" } oifname { "br-a" } return', rules)
        self.assertIn('ct original ip daddr 192.0.2.210 jump d100innat', rules)
        self.assertIn('ct original ip daddr 192.0.2.210 jump d100innat', rules.split('chain output_in')[1])
        self.assertEqual(rules.count('dnat ip to 172.20.0.2:80'), 2)
        self.assertIn('meta nfproto ipv6 drop', rules)
        config['cluster_enabled'] = False
        disabled = firewall.render(infos, config, {'a': ['br-a'], 'b': ['br-b']})
        self.assertNotIn('jump d100', disabled)

    def test_native_policy_log_prefix_and_datacenter_rate(self):
        info = {'id': 'a', 'vmid': 100, 'address': '192.0.2.210', 'firewall': 1}
        config = {'cluster_enabled': True, 'log_ratelimit': {'enable': 1, 'rate': '3/minute', 'burst': 8},
                  'guests': [{'vmid': 100, 'options': {'enable': 1, 'log_level_in': 'warning'},
                              'sets': {}, 'rules': {'in': [], 'out': []}}]}
        rules = firewall.render([info], config, {'a': ['br-a']})
        self.assertIn('limit rate 3/minute burst 8 packets log prefix ":100:4:d100in: DROP: " group 0', rules)
        config['log_ratelimit']['enable'] = 0
        self.assertNotIn('limit rate', firewall.render([info], config, {'a': ['br-a']}))

    def test_unavailable_or_malformed_metadata_never_removes_rules(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(firewall, 'STATE', Path(folder) / 'state'), \
                patch.object(firewall, 'apply_rules') as apply:
            metadata = Path(folder) / 'metadata'
            worker = SimpleNamespace(METADATA=metadata, current_node=lambda: 'pve')
            with patch.dict(sys.modules, provision=worker):
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    firewall.reconcile()
                metadata.mkdir()
                (metadata / 'guest.json').write_text('{invalid')
                with self.assertRaises(ValueError):
                    firewall.reconcile()
            apply.assert_not_called()

    def test_failed_apply_retains_saved_rules(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(firewall, 'STATE', Path(folder)), \
                patch.object(firewall.subprocess, 'run') as subprocess_run, \
                patch.object(firewall, 'command', side_effect=[None, ValueError('Rejected by kernel')]):
            subprocess_run.return_value.returncode = 0
            path = Path(folder) / 'applied.nft'
            path.write_text('previous rules')
            with self.assertRaisesRegex(ValueError, 'Rejected'):
                firewall.apply_rules('table inet betterdocker { }')
            self.assertEqual(path.read_text(), 'previous rules')

    def test_unsupported_options_do_not_look_enabled(self):
        for option in ('ips',):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, 'not supported'):
                firewall.check_options({option: 1})

    def test_rootless_forwarding_restore_keeps_guest_filter_and_cache(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(firewall, 'STATE', Path(folder)), \
                patch.object(firewall.subprocess, 'run') as run, patch.object(firewall, 'command') as command:
            run.return_value.returncode = 0
            guest = Path(folder) / 'applied.nft'
            guest.write_text('guest filtering')
            rules = 'table inet betterdocker_rootless { }'
            firewall.apply_rules(rules, table='betterdocker_rootless', cache='rootless.nft')
            self.assertEqual(guest.read_text(), 'guest filtering')
            self.assertEqual((Path(folder) / 'rootless.nft').read_text(), rules)
            for call in command.call_args_list:
                self.assertEqual(call.kwargs['input'], 'delete table inet betterdocker_rootless\n' + __import__('dctlib.firewall_reload', fromlist=['stamped']).stamped(rules))
