import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dct_cli', Path(__file__).resolve().parents[1] / 'scripts/dct.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CLITests(unittest.TestCase):
    def test_resolves_current_owner_and_rejects_other_guest_types(self):
        with patch.object(cli, 'api', return_value=[{'type':'docker','vmid':123,'id':'docker/'+'a'*32,'node':'target'}]):
            self.assertEqual(cli.guest(123), '/nodes/target/docker/'+'a'*32)
        with patch.object(cli, 'api', return_value=[{'type':'lxc','vmid':123}]):
            with self.assertRaises(ValueError): cli.guest(123)

    def test_stop_maps_to_immediate_stop_without_shell(self):
        with patch.object(cli, 'guest', return_value='/guest'), patch.object(cli, 'api', return_value=None) as api:
            cli.dispatch('stop', {'vmid':123})
            api.assert_called_once_with('create','/guest/action',action='force-stop',timeout=None)

    def test_failed_async_task_is_not_reported_as_success(self):
        with patch.object(cli,'api',return_value={'status':'stopped','exitstatus':'storage failure'}):
            with self.assertRaisesRegex(RuntimeError,'storage failure'): cli.wait_task('UPID:pve:123:rest')

    def test_native_task_warnings_are_reported_without_failure(self):
        with patch.object(cli,'api',return_value={'status':'stopped','exitstatus':'WARNINGS: 2'}), patch('sys.stderr',new_callable=io.StringIO) as output:
            self.assertEqual(cli.wait_task('UPID:pve:123:rest')['exitstatus'],'WARNINGS: 2')
            self.assertIn('WARNINGS: 2',output.getvalue())

    def test_destroy_uses_positional_id_without_extra_confirmation(self):
        with patch.object(cli,'guest',return_value='/guest'), patch.object(cli,'api',return_value=None) as api:
            cli.dispatch('destroy', {'vmid':123})
            api.assert_called_once_with('delete','/guest', **{'destroy-owned-data': 0})

    def test_api_serializes_parameters_as_separate_arguments(self):
        with patch.object(cli.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout='null'
            cli.api('set','/guest/config',description='$(secret); newline\nvalue')
            self.assertEqual(run.call_args.args[0][-1],'$(secret); newline\nvalue')
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_local_task_logs_do_not_break_json_response(self):
        with patch.object(cli.subprocess,'run') as run, patch('sys.stdout',new_callable=io.StringIO) as output:
            run.return_value.returncode=0
            run.return_value.stdout='Creating snapshot\n"UPID:pve:123:rest"\n'
            self.assertEqual(cli.api('create','/snapshot'),'UPID:pve:123:rest')
            self.assertIn('Creating snapshot',output.getvalue())

    def test_status_uses_native_text_and_explicit_json(self):
        with patch.object(cli,'guest',return_value='/guest'), patch.object(cli,'api',return_value={'status':'running','vmid':123}), patch('sys.stdout',new_callable=io.StringIO) as out:
            cli.dispatch('status',{'vmid':123})
            self.assertEqual(out.getvalue(),'status: running\n')
            out.truncate(0);out.seek(0)
            cli.dispatch('status',{'vmid':123,'output-format':'json'})
            self.assertEqual(json.loads(out.getvalue())['status'],'running')

    def test_create_reads_positional_compose_source_and_defaults_to_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'compose.yaml';source.write_text('services:\n  web:\n    image: nginx:stable\n')
            with patch.object(cli,'api',return_value=None) as api:
                cli.create({'vmid':123,'source':str(source),'hostname':'website','net0':'bridge=vmbr0,ip=192.0.2.123/24','node':'pve'})
                data=api.call_args.kwargs['deployment']
                self.assertEqual(data['name'],'website')
                self.assertEqual(data['cidr'],'192.0.2.123/24')
                self.assertEqual(api.call_args.kwargs['start'],0)

    def test_create_still_accepts_deployment_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'deployment.json';source.write_text(json.dumps({'name':'web','mode':'image','image':'nginx:stable','bridge':'vmbr0','cidr':'192.0.2.123/24'}))
            with patch.object(cli,'api',return_value=None) as api:
                cli.create({'vmid':123,'source':str(source)})
                self.assertEqual(api.call_args.kwargs['deployment']['image'],'nginx:stable')

    def test_missing_compose_file_does_not_turn_into_image(self):
        with patch.object(cli,'api') as api:
            with self.assertRaisesRegex(ValueError,'file does not exist'):
                cli.create({'vmid':123,'source':'missing.yaml'})
            api.assert_not_called()

    def test_unsupported_network_options_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'unknown'):
            cli.net0('bridge=vmbr0,ip=dhcp,unknown=192.0.2.1')
        with self.assertRaisesRegex(ValueError,'duplicate'):
            cli.net0('bridge=vmbr0,bridge=vmbr1')

    def test_resource_update_preserves_other_effective_and_pending_values(self):
        info={'digest':'before','services':[{'name':'web','settings':{'memory':128,'swap':128,'cpus':0.5,'dns':['1.1.1.1'],'restart':'always'},'pending':{'swap':256}}]}
        with patch.object(cli,'api',return_value={'digest':'new'}) as api, patch.object(cli,'finish_task'):
            cli.set_options('/guest',info,{'vmid':123,'memory':512})
            call=api.call_args
            self.assertEqual(call.kwargs['settings']['memory'],512)
            self.assertEqual(call.kwargs['settings']['swap'],256)
            self.assertEqual(call.kwargs['settings']['dns'],['1.1.1.1'])
            self.assertEqual(call.kwargs['settings']['restart'],'always')
            self.assertEqual(call.kwargs['digest'],'new')

    def test_multiservice_selection_is_validated_before_any_write(self):
        info={'digest':'before','services':[{'name':'web'},{'name':'cache'}]}
        with patch.object(cli,'api') as api:
            with self.assertRaisesRegex(ValueError,'--service'):
                cli.set_options('/guest',info,{'onboot':1,'memory':256})
            api.assert_not_called()

    def test_native_perl_numeric_strings_resolve_guest_ids(self):
        with patch.object(cli,'guest',return_value='/guest') as guest, patch.object(cli,'api',return_value=None):
            cli.dispatch('stop',{'vmid':'123'})
            guest.assert_called_once_with(123)

    def test_snapshot_commands_use_native_names(self):
        for command, action in [('snapshot','create'),('delsnapshot','delete'),('rollback','restore')]:
            with patch.object(cli,'guest',return_value='/guest'), patch.object(cli,'api',return_value=None) as api:
                cli.dispatch(command,{'vmid':123,'snapname':'baseline'})
                self.assertEqual(api.call_args.kwargs['action'],action)
                self.assertEqual(api.call_args.kwargs['snapname'],'baseline')

    def test_additional_ipv6_only_endpoint_and_port_assignments_round_trip(self):
        value = cli.net0('bridge=vmbr0,ip=none,ip6=2001:db8::2/64,ports=web:8080/tcp;dns:53/udp')
        self.assertEqual(value['ipv4mode'], 'none')
        self.assertEqual(value['ports'], 'web:8080/tcp,dns:53/udp')
        self.assertEqual(cli.net0(cli.format_net0(value)), value)

    def test_clone_requires_an_independent_endpoint_before_allocation(self):
        with patch.object(cli,'guest',return_value='/guest'), patch.object(cli,'api',return_value={'digest':'x','name':'web'}) as api:
            with self.assertRaisesRegex(ValueError,'independent'):
                cli.dispatch('clone',{'vmid':123,'newid':124,'net0':'bridge=vmbr0'})
            self.assertFalse(any(call.args[0]=='create' for call in api.call_args_list))

    def test_json_list_does_not_include_other_guest_types(self):
        rows=[{'type':'docker','vmid':123,'node':'pve'},{'type':'lxc','vmid':124,'node':'pve'}]
        with patch.object(cli,'api',return_value=rows), patch('sys.stdout',new_callable=io.StringIO) as out:
            cli.dispatch('list',{'cluster':1,'output-format':'json'})
            self.assertEqual([r['vmid'] for r in json.loads(out.getvalue())],[123])


if __name__ == '__main__':
    unittest.main()
