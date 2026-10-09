import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('dct_cli', Path(__file__).resolve().parents[1] / 'scripts/dct.py')
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)

class CLITests(unittest.TestCase):
    def test_resolves_current_owner_and_rejects_other_guest_types(self):
        with patch.object(cli, 'api', return_value=[{'type':'docker','vmid':123,'id':'docker/'+'a'*32,'node':'target'}]):
            self.assertEqual(cli.guest(123), '/nodes/target/docker/'+'a'*32)
        with patch.object(cli, 'api', return_value=[{'type':'lxc','vmid':123}]):
            with self.assertRaises(ValueError): cli.guest(123)

    def test_stop_maps_to_immediate_stop_without_shell(self):
        with patch.object(cli, 'guest', return_value='/guest'), patch.object(cli, 'api', return_value=None) as api, patch('sys.stdout',new_callable=io.StringIO):
            cli.main(['stop','123'])
            api.assert_called_once_with('create','/guest/action',action='force-stop',timeout=None)

    def test_failed_async_task_is_not_reported_as_success(self):
        with patch.object(cli,'api',return_value={'status':'stopped','exitstatus':'storage failure'}):
            with self.assertRaisesRegex(RuntimeError,'storage failure'): cli.wait_task('UPID:pve:123:rest')

    def test_native_task_warnings_are_reported_without_failure(self):
        with patch.object(cli,'api',return_value={'status':'stopped','exitstatus':'WARNINGS: 2'}), patch('sys.stderr',new_callable=io.StringIO) as output:
            self.assertEqual(cli.wait_task('UPID:pve:123:rest')['exitstatus'],'WARNINGS: 2')
            self.assertIn('WARNINGS: 2',output.getvalue())

    def test_destroy_requires_matching_id(self):
        with patch.object(cli,'guest',return_value='/guest'), patch.object(cli,'api',return_value={'digest':'x'}) as api:
            with self.assertRaisesRegex(ValueError,'match'): cli.main(['destroy','123','--confirm','124'])
            self.assertFalse(any(call.args[0]=='delete' for call in api.call_args_list))

    def test_api_serializes_parameters_as_separate_arguments(self):
        with patch.object(cli.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout='null'
            cli.api('set','/guest/config',description='$(secret); newline\nvalue')
            self.assertEqual(run.call_args.args[0][-1],'$(secret); newline\nvalue')
            self.assertNotIn('shell',run.call_args.kwargs)
