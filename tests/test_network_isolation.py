"""Network isolation contracts. No live packets or host rule changes."""
from contextlib import ExitStack, contextmanager
import copy
import ipaddress
import json
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_worker import worker as w
import firewall as f
import ipv6_proxy
from dctlib import network_security as n, network_identity as identity, firewall_reload as reload
from dctlib import network, ipv6_dynamic as v6, endpoints, endpoint_controls as controls, lifecycle
import guest_state

ID = 'a'*32
MAC = '02:00:00:00:00:20'


def info():
    return {'id':ID,'vmid':100,'node':'pve','bridge':'vmbr0','address':'198.18.0.20','interface':'bd'+ID[:12],'hwaddr':MAC,'ipv4mode':'dhcp','firewall':1}


def config():
    return {'cluster_enabled':True,'guests':[{'vmid':100,'options':{'enable':1,'ipfilter':1},'sets':{},'rules':{'in':[],'out':[]}}]}


class NetworkIsolation(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.base=Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        for obj,key,value in ((f,'STATE',self.base/'firewall'),(w,'ROOT',self.base/'projects'),(w,'METADATA',self.base/'metadata')):
            self.stack.enter_context(patch.object(obj,key,value))
        f.STATE.mkdir();w.METADATA.mkdir()

    def test_acquisition_requires_unicast_correct_family_and_bounded_lifetime(self):
        for value in ('0.0.0.0','127.0.0.1','169.254.1.1','224.0.0.1','255.255.255.255','::1'):
            with self.subTest(value=value),self.assertRaises(ValueError):n.dhcp4({'new_ip_address':value})
        for value in ('0','-1','4294967296','bad',True):
            with self.assertRaises(ValueError):n.lifetime(value)
        self.assertEqual(n.dhcp4({'new_ip_address':'198.18.0.20','new_dhcp_lease_time':'60','new_subnet_mask':'255.255.255.0'}),('198.18.0.20',60))

    def test_dhcp_rejects_subnet_broadcast_and_bad_mask(self):
        for value,mask in (('198.18.0.0','255.255.255.0'),('198.18.0.255','255.255.255.0'),('198.18.0.20','255.0.255.0')):
            with self.assertRaises(ValueError):n.dhcp4({'new_ip_address':value,'new_subnet_mask':mask})

    def test_conflicts_include_other_interfaces_and_same_guest_secondary_endpoint(self):
        endpoint=info()
        with patch.object(w,'capture',return_value='[{"ifname":"management","addr_info":[{"local":"198.18.0.20"}]}]'),self.assertRaises(ValueError):n.acquired(endpoint,endpoint['address'],4)
        with patch.object(w,'capture',return_value='[]'),patch.object(w,'summaries',return_value=[endpoint]),patch.object(endpoints,'resolved',return_value={'net1':dict(endpoint,interface='secondary')}),self.assertRaises(ValueError):n.acquired(endpoint,endpoint['address'],4)
        with patch.object(w,'capture',return_value='[]'),patch.object(w,'summaries',return_value=[]):self.assertEqual(n.acquired(endpoint,'fd00::20',6),'fd00::20')

    def test_bad_dhcp_event_does_not_modify_an_interface(self):
        endpoint=info()
        with patch.object(w,'summaries',return_value=[endpoint]),patch.object(w,'current_node',return_value='pve'),patch.object(w,'add_address') as add,patch.object(w,'write_json') as save:
            with self.assertRaises(ValueError):network.dhcp_event({'interface':endpoint['interface'],'reason':'BOUND','new_ip_address':endpoint['address'],'new_dhcp_lease_time':'0'})
            add.assert_not_called();save.assert_not_called()

    def test_vlan_lease_also_checks_host_and_guest_conflicts(self):
        endpoint=dict(info(),tag=20)
        with patch.object(w,'summaries',return_value=[endpoint]),patch.object(w,'current_node',return_value='pve'),patch.object(w,'capture',return_value='[{"ifname":"management","addr_info":[{"local":"198.18.0.20"}]}]'),patch.object(w,'add_address') as add:
            with self.assertRaises(ValueError):network.dhcp_event({'interface':endpoint['interface'],'reason':'BOUND','new_ip_address':endpoint['address'],'new_dhcp_lease_time':'60'})
            add.assert_not_called()

    def test_dhcp6_prefers_valid_lifetimes_and_conflicts_before_cache_write(self):
        endpoint=dict(info(),net='net0',ipv6mode='dhcp')
        with patch.object(w,'summaries',return_value=[endpoint]),patch.object(w,'current_node',return_value='pve'),patch.object(endpoints,'resolved',return_value={'net0':endpoint}),patch.object(w,'capture',return_value='[]'),patch.object(w,'write_json') as save:
            with self.assertRaises(ValueError):v6.event({'interface':endpoint['interface'],'reason':'BOUND6','new_ip6_address':'fd00::20','new_max_life':'60','new_preferred_life':'61'})
            save.assert_not_called()

    def test_ra_prefix_validation_is_inert_and_keeps_eui64(self):
        packet=bytearray(48);packet[0]=134;packet[16:20]=bytes([3,4,64,64]);packet[20:24]=struct.pack('!I',60);packet[24:28]=struct.pack('!I',30);packet[32:48]=ipaddress.IPv6Address('fd00:1::').packed
        self.assertIsNotNone(v6.router_prefix(bytes(packet),MAC))
        packet[32:48]=ipaddress.IPv6Address('fd00:1::1').packed
        self.assertIsNone(v6.router_prefix(bytes(packet),MAC))

    def test_proxy_listener_has_explicit_owned_pool_and_finite_ports(self):
        valid={'address':'fd00::20','port':8443,'protocol':'tcp','target':'10.240.1.2','internal':443}
        self.assertEqual(n.proxy_mappings([valid]),[valid])
        for changes in ({'address':'::'},{'address':'::1'},{'address':'fe80::1'},{'target':'127.0.0.1'},{'target':'10.240.0.0'},{'port':True},{'port':0},{'internal':65536},{'protocol':'sctp'}):
            with self.assertRaises(ValueError):n.proxy_mappings([dict(valid,**changes)])
        with self.assertRaises(ValueError):n.proxy_mappings([valid,valid])

    def test_rootless_resource_definition_cannot_use_external_driver_mounts(self):
        for kind,definition in (('volumes',{'driver_opts':{'device':'ordinary-host-path'}}),('volumes',{'external':True}),('networks',{'external':True}),('networks',{'driver':'host'})):
            with self.assertRaises(ValueError):n.rootless_resources({kind:{'app':definition}})
        n.rootless_resources({'volumes':{'data':{}},'networks':{'default':{}}})

    def test_rootless_engine_checks_bind_boundaries_and_volume_driver(self):
        body={'Labels':{'betterdocker.id':ID},'HostConfig':{'Binds':['/ordinary/host-path:/data:ro']}}
        with patch.object(w,'safe_bind',side_effect=ValueError('outside guest')):
            with self.assertRaises(ValueError):n.rootless_container(body)
        body['HostConfig']['Binds']=['fixture-volume:/data:rw']
        with patch.object(w,'capture',return_value='[{"Driver":"local","Options":{"device":"ordinary-host-path"}}]'),self.assertRaises(ValueError):n.rootless_container(body)
        with patch.object(w,'capture',return_value='[{"Driver":"local","Options":null}]'):n.rootless_container(body)

    def test_rootless_new_anonymous_volume_uses_only_default_local_storage(self):
        mount = {'Type': 'volume', 'Source': '', 'Target': '/anonymous'}
        body = {'Labels': {'betterdocker.id': ID}, 'HostConfig': {'Mounts': [mount]}}
        with patch.object(w, 'capture') as inspect:
            n.rootless_container(body)
            mount['VolumeOptions'] = {'DriverConfig': {'Name': 'local', 'Options': {}}}
            n.rootless_container(body)
            inspect.assert_not_called()
            for source in ('', 'existing-volume'):
                mount['Source'] = source
                for driver in ({'Name': 'external'}, {'Name': 'local', 'Options': {'device': 'fixture'}}):
                    mount['VolumeOptions'] = {'DriverConfig': driver}
                    with self.assertRaisesRegex(ValueError, 'driver'):
                        n.rootless_container(body)
            mount.pop('VolumeOptions')
            body['HostConfig']['VolumeDriver'] = 'external'
            with self.assertRaisesRegex(ValueError, 'driver'):
                n.rootless_container(body)
            inspect.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'driver'):
            n.rootless_resources({'services': {'app': {'volume_driver': 'external'}}})

    def test_private_source_guards_precede_same_guest_bypass(self):
        guard={ID:{'br-fixture':{'ips':['10.240.1.2'],'macs':[MAC]}}}
        rules=f.render([info()],config(),{ID:['br-fixture']},guards=guard)
        forward=rules.split('chain forward_out')[1]
        self.assertLess(forward.index('ip saddr !='),forward.index('oifname { "br-fixture" } return'))
        self.assertLess(forward.index('ether saddr !='),forward.index('oifname { "br-fixture" } return'))

    def test_rootless_identity_floor_does_not_depend_on_guest_enable(self):
        cfg=config();cfg['cluster_enabled']=False
        rules=f.render([info()],cfg,{ID:['br-fixture']},inner=True,guards={ID:{'br-fixture':{'ips':['10.240.1.2'],'macs':[MAC]}}})
        self.assertIn('chain identity_forward',rules);self.assertIn('hook forward priority -30',rules)
        self.assertIn('ip saddr != { 10.240.1.2 } drop',rules);self.assertNotIn('jump d100',rules)

    def test_cross_guest_policies_keep_separate_forward_hooks(self):
        other=dict(info(),id='b'*32,vmid=101,address='198.18.0.21')
        cfg=config();cfg['guests'].append(dict(cfg['guests'][0],vmid=101))
        rules=f.render([info(),other],cfg,{ID:['br-first'],other['id']:['br-second']})
        self.assertIn('hook forward priority -10',rules);self.assertIn('hook forward priority 0',rules)
        self.assertIn('oifname "br-second" jump d101in',rules)
        self.assertNotIn('iifname { "br-first" } oifname { "br-second" } return',rules)

    def test_secondary_vlan_and_sdn_endpoints_keep_independent_ipv4_ipv6_chains(self):
        endpoint={'bridge':'vmbr0','ipv4mode':'static','cidr':'198.18.0.20/24','cidr6':'fd00::20/64','ipv6mode':'static','hwaddr':MAC,'firewall':1}
        guest=dict(info(),interfaces={'net0':endpoint,'net1':dict(endpoint,bridge='vnettest',tag=20,cidr='198.18.1.20/24',cidr6='fd00:1::20/64')})
        rules=f.render([guest],config(),{ID:['br-fixture']})
        for chain in ('d100in','d100net1in','v6d100net0in','v6d100net1out'):self.assertIn('chain '+chain,rules)
        self.assertIn('ip6 daddr fd00:1::20 jump v6d100net1in',rules)

    def test_native_dhcp_ndp_radv_options_are_supported(self):
        f.check_options({'dhcp':1,'ndp':0,'radv':0})
        cfg=config();cfg['guests'][0]['options'].update(dhcp=1,ndp=0,radv=0)
        endpoint={'bridge':'vmbr0','ipv4mode':'static','cidr':'198.18.0.20/24','cidr6':'fd00::20/64','ipv6mode':'static','hwaddr':MAC,'firewall':1}
        rules=f.render([dict(info(),interfaces={'net0':endpoint})],cfg,{ID:['br-fixture']})
        self.assertIn('udp sport 67 udp dport 68 accept',rules)
        self.assertNotIn('nd-neighbor-solicit, nd-neighbor-advert',rules)
        self.assertIn('icmpv6 type nd-router-advert drop',rules)

    def test_published_ipv6_exceptions_are_scoped_for_both_native_backends(self):
        bindings=[('tcp',8443,'10.240.1.2',443,'fd00::20')]
        with patch.object(f,'command') as command:
            controls.apply_native_host_exceptions(bindings,{'nftables':[]})
            text=command.call_args.kwargs['input']
            self.assertIn('ip6 daddr fd00::20 tcp dport 8443',text);self.assertNotIn('flush table',text)
        with patch.object(subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')),patch.object(f,'command',side_effect=['-P INPUT ACCEPT\n','']) as command:
            controls.apply_host_exceptions(bindings)
            self.assertIn('-F BD6-PUBLISHED',command.call_args.kwargs['input'])
            self.assertNotIn('-F INPUT',command.call_args.kwargs['input'])

    def test_prepare_checks_actual_generation_before_start(self):
        with patch.object(w,'node_runtime') as runtime,patch.object(w,'firewall_check'),patch.object(w,'run') as run:
            runtime.load.return_value=SimpleNamespace(rootless=False)
            network.prepare_firewall(info())
            self.assertEqual(run.call_args.args[0][-2:],['verify',ID])

    def test_post_start_failure_stops_workload(self):
        with patch.object(w,'run',side_effect=[None,ValueError('missing rules'),None]) as run,self.assertRaises(ValueError):network.finish_network_start(info())
        self.assertEqual(run.call_args.args[0][-1],'stop')

    def test_missing_generation_rejects_ready_check(self):
        guest=info();(w.METADATA/(ID+'.json')).write_text(json.dumps(guest))
        (f.STATE/'status.json').write_text('{"error":null}')
        (f.STATE/'applied.nft').write_text('fixture')
        with patch.object(w,'current_node',return_value='pve'),patch.object(f.runtime,'load',return_value=SimpleNamespace(rootless=False)),patch.object(reload,'present',return_value=False),self.assertRaisesRegex(ValueError,'missing'):f.verify_guest(ID)

    def test_failed_generation_from_status_rejects_ready_check(self):
        (w.METADATA/(ID+'.json')).write_text(json.dumps(info()))
        (f.STATE/'status.json').write_text('{"error":"reload failed"}')
        with patch.object(w,'current_node',return_value='pve'),patch.object(f.runtime,'load',return_value=SimpleNamespace(rootless=False)),self.assertRaisesRegex(ValueError,'reload failed'):f.verify_guest(ID)

    def test_resume_from_backup_keeps_journal_when_filtering_fails(self):
        path=w.ROOT/ID/'export-transaction.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'paused':[],'restart':['fixture'],'tags':[]}))
        with patch('dctlib.recovery.require_docker'),patch('dctlib.hardening.check_profile'),patch.object(w,'container_records',return_value=[{'id':'fixture','state':'exited'}]),patch.object(w,'detail',return_value=info()),patch.object(w,'current_node',return_value='pve'),patch.object(w,'prepare_network_start'),patch.object(w,'prepare_firewall',side_effect=ValueError('missing filter')),patch.object(w,'run') as run:
            with self.assertRaises(ValueError):guest_state.cleanup_capture(ID)
            run.assert_not_called();self.assertTrue(path.exists())

    def test_multiple_private_bridges_do_not_skip_the_second_source_guard(self):
        guards={ID:{bridge:{'ips':['10.240.1.2'],'macs':[MAC]} for bridge in ('br-first','br-second')}}
        rules=f.render([info()],config(),{ID:['br-first','br-second']},guards=guards)
        self.assertIn('iifname { "br-first" } oifname { "br-first", "br-second" } return',rules)
        self.assertNotIn('iifname { "br-first", "br-second" } oifname',rules)

    def test_endpoints_do_not_expose_wildcard_host_services(self):
        bindings={ID:[('tcp',8080,'10.240.1.2',80)]}
        rules=controls.render([info()],bindings=bindings)
        self.assertIn('ip daddr 198.18.0.20 tcp dport 8080 socket wildcard 0 ct mark set ct mark | 0x80000000 accept',rules)
        self.assertIn('ip daddr 198.18.0.20 drop',rules)
        self.assertNotIn('dport 8006',rules)
        self.assertNotIn('ip daddr 198.18.0.1 drop',rules)

    def test_stopped_endpoints_keep_host_services_closed(self):
        rules = controls.render([dict(info(), firewall=0)], bindings={})
        self.assertIn('ip daddr 198.18.0.20 drop', rules)
        self.assertNotIn('tcp dport', rules)

    def test_rootful_reconciliation_guards_endpoints_without_guest_firewall(self):
        guest = dict(info(), firewall=0)
        w.write_json(w.METADATA / (ID + '.json'), guest)
        bindings = {ID: [('tcp', 8080, '172.18.0.2', 80)]}
        with patch.object(f.runtime, 'load', return_value=SimpleNamespace(rootless=False)), patch.object(w, 'current_node', return_value='pve'), patch.object(f, 'native_config', return_value=dict(config(), cluster_enabled=False)), patch.object(f, 'networks') as bridges, patch.object(f, 'published_ports', return_value=bindings[ID]) as published, patch.object(f, 'identity_guards', return_value={}), patch.object(reload, 'transaction') as transaction, patch.object(controls, 'activate') as activate, patch.object(n, 'release_ready'):
            f.reconcile()
            rules = next(plan[1] for plan in transaction.call_args.args[0] if plan[0] == 'betterdocker_endpoints')
            transaction.call_args.kwargs['activate']()
        bridges.assert_not_called()
        published.assert_called_once_with(guest)
        activate.assert_called_once_with([guest], bindings)
        self.assertIn('ip daddr 198.18.0.20 drop', rules)
        self.assertIn('tcp dport 8080 socket wildcard 0 ct mark set ct mark | 0x80000000 accept', rules)

    def test_rootless_private_bridges_are_isolated_across_dcts_without_guest_firewall(self):
        guards = {ID: {bridge: {'ips': ['10.240.1.2'], 'macs': [MAC]} for bridge in ('br-first', 'br-second')}, 'b' * 32: {'br-other': {'ips': ['10.240.2.2'], 'macs': [MAC]}}}
        rules = '\n'.join(identity.render(guards))
        self.assertIn('hook forward priority -25', rules)
        self.assertIn('iifname { "br-first", "br-second" } oifname { "br-other" } drop', rules)
        self.assertIn('iifname { "br-other" } oifname { "br-first", "br-second" } drop', rules)
        self.assertNotIn('iifname { "br-first" } oifname { "br-second" } drop', rules)
        self.assertNotIn('bd-uplink', rules)
        self.assertLess(rules.index('ip saddr !='), rules.index('chain private_isolation'))

    def test_rootless_local_nat_does_not_mask_external_client_addresses(self):
        @contextmanager
        def namespace():
            yield None, None, [], ()
        with patch('host.namespace', namespace), patch.object(f, 'render', return_value='fixture'), patch.object(reload, 'transaction') as transaction, patch.object(n, 'release_ready'):
            f.reconcile_rootless([], config(), {}, {})
        plans = transaction.call_args.args[0]
        rules = next(plan[1] for plan in plans if plan[0] == 'betterdocker_rootless')
        self.assertIn('oifname "bd-rootless" ct status dnat fib saddr type local masquerade', rules)
        self.assertNotIn('oifname "bd-rootless" ct status dnat masquerade\n', rules.split('chain outbound', 1)[1].split('iifname "bd-rootless" oifname "bd-rootless"', 1)[0])

    def test_rootless_local_client_replies_are_scoped_to_published_services(self):
        guest = info(); guest['cidr6'] = 'fd00::20/64'
        bindings = {ID: [('tcp', 8080, '10.240.1.2', 80), ('tcp', 8443, '10.240.1.2', 443, 'fd00::20')]}
        rules = controls.render([guest], bindings=bindings)
        for family, address, port in (('ip', '198.18.0.20', 8080), ('ip6', 'fd00::20', 8443)):
            reply = family + ' daddr ' + address + ' ct direction reply ct state established ct original ' + family + ' daddr ' + address + ' ct original proto-dst ' + str(port) + ' meta l4proto tcp accept'
            self.assertIn(reply, rules)
            self.assertLess(rules.index(reply), rules.index(family + ' daddr ' + address + ' drop', rules.index('chain host_endpoint_guard')))
        self.assertNotIn('ct state established,related accept', rules)
        self.assertNotIn('proto-dst 8006', rules)
        self.assertIn('tcp dport 8443 tcp flags & syn == 0 ct direction original ct state established ct mark & 0x80000000 != 0 accept', rules)
        self.assertIn('socket wildcard 0 ct mark set ct mark | 0x80000000 accept', rules)

    def test_new_address_remains_closed_until_its_policy_is_ready(self):
        endpoint=info()
        with patch.object(w.node_runtime,'load',return_value=SimpleNamespace(rootless=True)),patch.object(w,'capture',return_value='[]'),patch.object(n,'apply_quarantine') as apply:
            n.block_address(endpoint,endpoint['address'],4)
            file=w.ROOT.parent/'address-quarantine.json'
            self.assertIn(endpoint['address'],json.loads(file.read_text()))
            n.release_ready([])
            self.assertIn(endpoint['address'],json.loads(file.read_text()))
            n.release_ready([endpoint])
            self.assertEqual(json.loads(file.read_text()),{})
            self.assertEqual(apply.call_args.args[0],{})

    def test_failed_acquisition_guard_prevents_assigning_an_address(self):
        endpoint=info()
        with patch.object(w.node_runtime,'load',return_value=SimpleNamespace(rootless=True)),patch.object(w,'capture',return_value='[]'),patch.object(n,'apply_quarantine',side_effect=ValueError('cannot filter')),patch.object(w,'write_json') as save:
            with self.assertRaises(ValueError):n.block_address(endpoint,endpoint['address'],4)
            save.assert_not_called()

    def test_current_address_is_not_requarantined_on_every_refresh(self):
        endpoint=info()
        with patch.object(w.node_runtime,'load',return_value=SimpleNamespace(rootless=True)),patch.object(w,'capture',return_value='[{"addr_info":[{"local":"198.18.0.20"}]}]'),patch.object(n,'apply_quarantine') as apply:
            n.block_address(endpoint,endpoint['address'],4)
            apply.assert_not_called()

    def test_quarantine_preserves_neighbor_discovery_but_blocks_application_traffic(self):
        with patch.object(f,'apply_rules') as apply:
            n.apply_quarantine({'fd00::20':{'address':'fd00::20'}})
        rules=apply.call_args.args[0]
        self.assertIn('nd-neighbor-solicit, nd-neighbor-advert',rules)
        self.assertIn('ip6 daddr fd00::20 drop',rules)
        self.assertNotIn('tcp dport',rules)

    def test_daemon_resume_filters_before_start_and_keeps_journal_on_failure(self):
        proc=self.base/'proc';boot=proc/'sys/kernel/random/boot_id';boot.parent.mkdir(parents=True);boot.write_text('fixture')
        directory=w.ROOT/ID;directory.mkdir(parents=True)
        path=w.ROOT.parent/'lifecycle.json'
        path.write_text(json.dumps({'boot_id':'fixture','containers':[{'id':'fixture','project':ID,'state':'running','policy':{'Name':'no'}}]}))
        with patch.object(w,'PROC',proc),patch.object(w,'locked'),patch.object(w,'summaries',return_value=[info()]),patch.object(w,'container_records',return_value=[{'id':'fixture'}]),patch('dctlib.hardening.check_profile'),patch.object(w.project_limits,'preflight'),patch.object(w,'prepare_network_start'),patch.object(w,'prepare_firewall',side_effect=ValueError('filter unavailable')),patch.object(w,'run') as run:
            with self.assertRaises(ValueError):lifecycle.daemon_resume('pve')
            run.assert_not_called();self.assertTrue(path.exists())


class FirewallReload(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(f,'STATE',self.root))
        self.stack.enter_context(patch.object(reload.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='table inet betterdocker { chain old { } }')))
        self.real_present = reload.present
        self.stack.enter_context(patch.object(reload,'present',return_value=True))
        self.plan=[('betterdocker','table inet betterdocker { chain new { } }\n','applied.nft',(),())]
        (self.root/'applied.nft').write_text('previous cache')

    def test_single_namespace_changes_are_one_transaction(self):
        plans=self.plan+[('betterdocker_endpoints','table inet betterdocker_endpoints { }\n','endpoints.nft',(),())]
        with patch.object(f,'command') as command:reload.transaction(plans)
        self.assertEqual(len(command.call_args_list),2)
        self.assertIn('betterdocker_endpoints',command.call_args.kwargs['input']);self.assertIn('generation_',command.call_args.kwargs['input'])

    def test_check_failure_keeps_cache_and_never_applies(self):
        with patch.object(f,'command',side_effect=ValueError('syntax failure')) as command,self.assertRaises(ValueError):reload.transaction(self.plan)
        self.assertIn('--check',command.call_args.args[0]);self.assertEqual((self.root/'applied.nft').read_text(),'previous cache')

    def test_failed_commit_keeps_last_cache(self):
        with patch.object(f,'command',side_effect=[None,ValueError('commit failure')]),self.assertRaises(ValueError):reload.transaction(self.plan)
        self.assertEqual((self.root/'applied.nft').read_text(),'previous cache')

    def test_activation_failure_rolls_back_working_tables(self):
        with patch.object(f,'command') as command,self.assertRaises(ValueError):reload.transaction(self.plan,activate=lambda: (_ for _ in ()).throw(ValueError('adapter failure')))
        self.assertIn('chain old',command.call_args.kwargs['input']);self.assertEqual((self.root/'applied.nft').read_text(),'previous cache')

    def test_rootless_two_namespaces_check_before_closing(self):
        plans=[('betterdocker','table inet betterdocker { }','inner.nft',('fixture-ns',),())]+self.plan
        events=[]
        def command(args,**kwargs):events.append('check' if '--check' in args else 'apply')
        with patch.object(f,'command',side_effect=command),patch.object(reload,'closed_rootless',side_effect=lambda:events.append('close')):reload.transaction(plans,rootless=True)
        self.assertEqual(events,['check','check','close','apply','apply'])

    def test_rootless_rollback_failure_keeps_forwarding_closed(self):
        events=[]
        def command(args,**kwargs):
            if '--check' not in args:raise ValueError('failure')
        with patch.object(f,'command',side_effect=command),patch.object(reload,'closed_rootless',side_effect=lambda:events.append('close')),self.assertRaisesRegex(ValueError,'remains closed'):reload.transaction(self.plan,rootless=True)
        self.assertEqual(events,['close','close'])

    def test_unchanged_generation_skips_rule_replacement(self):
        (self.root/'applied.nft').write_text(self.plan[0][1])
        with patch.object(f,'command') as command:reload.transaction(self.plan)
        command.assert_not_called()

    def test_kernel_generation_is_checked_not_only_cache(self):
        rules=self.plan[0][1]
        with patch.object(reload.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'nftables':[{'chain':{'name':reload.generation(rules)}}]}))):self.assertTrue(self.real_present('betterdocker',rules))
        with patch.object(reload.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"nftables":[]}')):self.assertFalse(self.real_present('betterdocker',rules))
