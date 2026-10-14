"""Small files and cooperative processes verify cleanup without touching guests."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker as w
from dctlib import operation_capacity as c, retention as r, space as s

ID = 'a' * 32


class AbandonedOperations(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        for obj, attr, value in ((c,'ROOT',self.base/'slots'),(c,'POLICY',self.base/'operations.json'),(r,'ROOT',self.base/'retention'),(r,'POLICY',self.base/'retention.json'),(s,'ROOT',self.base/'space'),(s,'POLICY',self.base/'space.json'),(w,'ROOT',self.base/'projects')):
            self.stack.enter_context(patch.object(obj,attr,value))
        self.stack.enter_context(patch.dict(os.environ)); os.environ.pop(c.ENV,None)
        self.folder = w.ROOT.parent/'transfers'/ID
        self.children = []; self.addCleanup(self.finish_children)

    def finish_children(self):
        for child in self.children:
            if child.poll() is None: child.communicate('\n',timeout=10)

    def partial(self):
        r.register([self.folder])
        self.folder.mkdir(parents=True,exist_ok=True)
        path = self.folder/'state.partial'
        with path.open('xb') as output:
            r.own_partial(path, output.fileno()); output.write(b'ordinary fixture')
        return path

    def child(self):
        child = subprocess.Popen([sys.executable,'-c','import sys; print("ready",flush=True); sys.stdin.readline()'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,pass_fds=(c.descriptor(),))
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(),'ready')
        return child

    def test_dead_operation_reclaims_only_enrolled_partial_and_empty_record(self):
        with c.reserve(): path = self.partial()
        result = r.sweep()
        self.assertEqual(result, {'records_removed':1,'partial_files_removed':1})
        self.assertFalse(path.exists())
        self.assertTrue(self.folder.exists())
        self.assertEqual(r.sweep(), {'records_removed':0,'partial_files_removed':0})

    def test_live_operation_retains_partial(self):
        with c.reserve():
            path = self.partial()
            self.assertEqual(r.sweep()['partial_files_removed'],0)
            self.assertTrue(path.exists())

    def test_inherited_child_keeps_partial_after_parent_closes_slot(self):
        with c.reserve():
            path = self.partial(); child = self.child()
        self.assertEqual(r.sweep()['partial_files_removed'],0)
        self.assertTrue(path.exists())
        child.communicate('\n',timeout=10)
        self.assertEqual(r.sweep()['partial_files_removed'],1)

    def test_recovery_and_completed_archives_are_retained(self):
        for filename in ('state.tar','ready.json','metadata','state','export-transaction.json'):
            with self.subTest(filename=filename):
                if self.folder.exists():
                    for path in self.folder.iterdir(): path.unlink()
                with c.reserve(): path = self.partial()
                recovery = self.folder/filename; recovery.write_bytes(b'recovery data')
                self.assertEqual(r.sweep()['partial_files_removed'],0)
                self.assertTrue(path.exists()); self.assertEqual(recovery.read_bytes(),b'recovery data')

    def test_unregistered_partial_is_never_deleted(self):
        with c.reserve():
            r.register([self.folder]); self.folder.mkdir(parents=True)
            path = self.folder/'state.partial'; path.write_bytes(b'unregistered')
        self.assertEqual(r.sweep()['partial_files_removed'],0)
        self.assertTrue(path.exists())

    def test_replaced_file_identity_is_retained(self):
        with c.reserve(): path = self.partial()
        other = self.folder/'replacement'; other.write_bytes(b'new file')
        other.replace(path)
        self.assertEqual(r.sweep()['partial_files_removed'],0)
        self.assertEqual(path.read_bytes(),b'new file')

    def test_new_live_owner_of_same_paths_protects_old_receipt(self):
        with c.reserve(): path = self.partial()
        # A distinct cooperative operation owns the same staging directory.
        with c.reserve():
            with patch.object(r, 'reap_locked'):
                r.register([self.folder])
            self.assertTrue(path.exists())
            self.assertEqual(r.sweep()['partial_files_removed'],0)

    def test_budget_registration_reaps_abandoned_partial_before_charging(self):
        with c.reserve(): path = self.partial()
        with c.reserve(): r.register([self.base/'other-output'])
        self.assertFalse(path.exists())

    def test_reused_slot_does_not_keep_old_reservations(self):
        with c.reserve():
            r.register([self.base/'empty']); old = c.identity()
        with c.reserve():
            self.assertNotEqual(c.identity(),old)
            r.sweep()
            self.assertFalse((r.ROOT/(old+'.json')).exists())

    def test_guest_peak_reservation_survives_inherited_child(self):
        with c.reserve(), patch.object(w,'operation_state',return_value={'active':True,'pid':123,'process_start':'fixture'}):
            with s.reserve([{'key':'fixture','bytes':1,'free':1024**4,'name':'fixture'}],identifier=ID): child = self.child()
        with patch.object(w,'operation_state',return_value={'active':False}):
            self.assertEqual(s.sweep(),0)
            child.communicate('\n',timeout=10)
            self.assertEqual(s.sweep(),1)
            self.assertEqual(s.sweep(),0)

    def test_remote_lease_retains_borrower_when_controller_ends(self):
        key = 'b'*32; s.ROOT.mkdir()
        with c.reserve():
            record={'owner':{'kind':'process','pid':123,'start':'controller'},'claims':{'fixture':1}}
            (s.ROOT/(key+'.json')).write_text(json.dumps(record))
            with patch.object(s,'process_start',return_value='controller'), s.transfer_loan(key): child = self.child()
        with patch.object(s,'process_start',side_effect=FileNotFoundError):
            self.assertEqual(s.sweep(),0)
            child.communicate('\n',timeout=10)
            self.assertEqual(s.sweep(),1)

    def test_no_ownership_receipt_for_backup_output(self):
        with c.reserve():
            r.register([self.base])
            with (self.base/'backup.tar').open('wb') as output:
                with self.assertRaises(ValueError): r.own_partial(self.base/'backup.tar',output.fileno())

    def test_worker_exit_releases_slot_and_enrolled_partial(self):
        code = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from dctlib import projects as w,operation_capacity as c,retention as r
base=Path(sys.argv[2]);w.ROOT=base/'projects';c.ROOT=base/'slots';c.POLICY=base/'operations.json';r.ROOT=base/'retention';r.POLICY=base/'retention.json'
folder=w.ROOT.parent/'transfers'/('a'*32)
with c.reserve():
 r.register([folder]);folder.mkdir(parents=True)
 with (folder/'state.partial').open('xb') as output:
  r.own_partial(folder/'state.partial',output.fileno());output.write(b'fixture');output.flush()
 os._exit(0)
"""
        child = subprocess.run([sys.executable,'-c',code,str(Path(r.__file__).resolve().parents[1]),str(self.base)],capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertEqual(r.sweep()['partial_files_removed'],1)
        with c.reserve(): pass

    def test_cleanup_command_requires_root_before_sweeping(self):
        with patch.object(sys,'argv',['provision.py','cleanup-temporary']), patch.object(os,'geteuid',return_value=1000), patch.object(r,'sweep') as clean:
            with self.assertRaisesRegex(ValueError,'administrator'): w.main()
            clean.assert_not_called()

    def test_sequential_helpers_share_one_native_guest_reservation(self):
        target={'key':'fixture','bytes':1,'free':1024**4,'name':'fixture'}
        state={'active':True,'pid':123,'process_start':'fixture'}
        with patch.object(w,'operation_state',return_value=state):
            with c.reserve(), s.reserve([target],identifier=ID): first = c.identity()
            with c.reserve(), s.reserve([target],identifier=ID):
                self.assertNotEqual(c.identity(),first)
                self.assertEqual(len(list(s.ROOT.glob('*.json'))),1)
                value = json.loads(next(s.ROOT.glob('*.json')).read_text())
                self.assertEqual(value['owner']['borrowers'][0]['token'],c.identity())
