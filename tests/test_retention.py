"""Retention checks with small files, isolated locks and mocked Docker metadata."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker as w
from dctlib import retention as r, operation_capacity as c

ID = 'a' * 32
CHILD = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from dctlib import retention as r,operation_capacity as c,projects as w
r.ROOT=Path(sys.argv[2]);r.POLICY=Path(sys.argv[3]);c.ROOT=Path(sys.argv[4]);c.POLICY=Path(sys.argv[5]);w.ROOT=Path(sys.argv[6])
try:
 with c.reserve():
  r.register([sys.argv[7]])
  print('admitted',flush=True);sys.stdin.readline()
except ValueError as error:
 print(str(error),flush=True);sys.exit(3)
'''


class Retention(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for mod, key, value in ((r,'ROOT',self.base/'records'),(r,'POLICY',self.base/'retention.json'),(c,'ROOT',self.base/'slots'),(c,'POLICY',self.base/'operations.json'),(w,'ROOT',self.base/'projects')):
            self.stack.enter_context(patch.object(mod,key,value))
        self.stack.enter_context(patch.dict(os.environ));os.environ.pop(c.ENV,None)
        c.POLICY.write_text(json.dumps({'max_concurrent': 4}))
        self.chosen = dict(r.DEFAULTS, transfer_bytes=12, node_transfer_bytes=24)
        self.save()
        self.children=[];self.addCleanup(self.cleanup_children)

    def save(self): r.POLICY.write_text(json.dumps(self.chosen))

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None: child.communicate('\n',timeout=10)

    def child(self,name):
        args=[sys.executable,'-c',CHILD,str(Path(r.__file__).resolve().parents[1]),str(r.ROOT),str(r.POLICY),str(c.ROOT),str(c.POLICY),str(w.ROOT),str(self.base/name)]
        child=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(child);return child

    def test_positive_policy_and_default_values(self):
        self.assertEqual(r.policy()['snapshots'],16)
        for value in (0,-1,True,1.5):
            self.chosen['snapshot_bytes']=value;self.save()
            with self.assertRaises(ValueError):r.policy()

    def test_snapshot_count_and_actual_file_bytes(self):
        root=w.snapshot_root(ID);root.mkdir(parents=True)
        for name in ('first','second'):
            p=root/name;p.mkdir();(p/'data').write_bytes(b'abc')
        self.chosen.update(snapshots=2,snapshot_bytes=100);self.save()
        with self.assertRaisesRegex(ValueError,'count limit'):r.snapshots(ID)
        self.chosen.update(snapshots=3,snapshot_bytes=6);self.save()
        r.snapshots(ID,0)
        with self.assertRaisesRegex(ValueError,'file budget'):r.snapshots(ID,1)
        self.assertEqual((root/'first/data').read_bytes(),b'abc')

    def test_imported_history_is_counted_before_destination_changes(self):
        incoming=self.base/'history';incoming.mkdir();(incoming/'images.tar').write_bytes(b'12345')
        self.chosen['snapshot_bytes']=4;self.save()
        with self.assertRaisesRegex(ValueError,'file budget'):r.snapshots(ID,incoming=[incoming])
        self.assertFalse(w.snapshot_root(ID).exists())

    def test_preservation_charges_unique_complete_images_and_new_files(self):
        self.chosen['preserved_image_bytes']=10;self.save()
        listings='\n'.join(json.dumps({'Repository':'betterdocker-settings/'+ID,'Tag':tag,'ID':'image'}) for tag in ('one','two'))
        result=subprocess.CompletedProcess([],0,listings,'')
        with patch.object(w,'run',return_value=result),patch.object(w,'capture',return_value='[{"Size":8}]') as inspect:
            r.preservation(ID,[{'SizeRootFs':1}])
            self.assertEqual(inspect.call_args.args[0][-1],'image')
            with self.assertRaisesRegex(ValueError,'preserved-image'):r.preservation(ID,[{'SizeRootFs':3}])

    def test_log_defaults_and_explicit_overrides(self):
        r.logs({'max-size':'10m','max-file':'3'})
        for opts in ({'max-size':'11m','max-file':'3'},{'max-size':'10m','max-file':'4'}):
            with self.assertRaisesRegex(ValueError,'Log rotation'):r.logs(opts)
        body={'HostConfig':{}};r.container_logs(body)
        self.assertEqual(body['HostConfig']['LogConfig']['Config']['max-file'],'3')

    def test_stream_rejects_extra_byte_before_writing_it(self):
        output=io.BytesIO()
        self.assertEqual(r.copy_stream(io.BytesIO(b'12345678'),output,8),8)
        output=io.BytesIO()
        with self.assertRaisesRegex(ValueError,'Incoming DCT archive'):r.copy_stream(io.BytesIO(b'123456789'),output,8)
        self.assertLessEqual(len(output.getvalue()),8)

    def test_atomic_node_budget_and_shared_nested_registration(self):
        first=self.child('first');self.assertEqual(first.stdout.readline().strip(),'admitted')
        second=self.child('second');self.assertEqual(second.stdout.readline().strip(),'admitted')
        third=self.child('third');out,err=third.communicate(timeout=10)
        self.assertEqual(third.returncode,3,err);self.assertIn('Node temporary',out)
        first.communicate('\n',timeout=10)
        with c.reserve():
            r.register([self.base/'one']);r.register([self.base/'one/inner'])
        second.communicate('\n',timeout=10)

    def test_retained_files_stay_charged_after_worker_exit(self):
        path=self.base/'retained';path.write_bytes(b'abcd')
        with c.reserve():r.register([path])
        self.chosen['node_transfer_bytes']=14;self.save()
        with c.reserve(),self.assertRaisesRegex(ValueError,'Node temporary'):r.register([self.base/'new'])
        self.assertEqual(path.read_bytes(),b'abcd')

    def test_successive_helpers_reuse_existing_staging_without_double_charge(self):
        path=self.base/'stage';path.mkdir();(path/'archive').write_bytes(b'abcd')
        with c.reserve():r.register([path])
        self.chosen['node_transfer_bytes']=12;self.save()
        with c.reserve():r.register([path]);r.register([path/'unpacked'])
        self.assertEqual((path/'archive').read_bytes(),b'abcd')

    def test_transfer_preflight_and_actual_size_are_bounded(self):
        path=self.base/'stage';path.write_bytes(b'12345678')
        with self.assertRaisesRegex(ValueError,'transfer budget'):r.check_transfer([path],5)
        self.assertEqual(r.check_transfer([path]),8)
        path.write_bytes(b'1234567890123')
        with self.assertRaisesRegex(ValueError,'transfer budget'):r.check_transfer([path])

    def test_saved_filesystems_fail_before_image_load(self):
        self.chosen['preserved_image_bytes']=8;self.save()
        with self.assertRaisesRegex(ValueError,'Saved container'):r.saved_images([{'SizeRootFs':9}])

    def test_loaded_image_sizes_use_engine_metadata(self):
        self.chosen['preserved_image_bytes']=8;self.save()
        with patch.object(w,'capture',return_value='[{"Size":9}]'):
            with self.assertRaisesRegex(ValueError,'Imported images'):
                r.imported_images([{'SnapshotImage':'image','SizeRootFs':1}])

    def test_native_history_disk_is_not_retained_file_payload(self):
        incoming=self.base/'history';incoming.mkdir()
        (incoming/'manifest.json').write_text(json.dumps({'native_storage':{'volid':'fixture'}}))
        (incoming/'disk.raw').write_bytes(b'123456789')
        self.chosen['snapshot_bytes']=r.tree_bytes(incoming)-9;self.save()
        r.snapshots(ID,incoming=[incoming])

    def test_duplicate_staging_records_do_not_double_charge_retained_files(self):
        path=self.base/'stage';path.mkdir();(path/'archive').write_bytes(b'abcd')
        for _ in range(3):
            with c.reserve():r.register([path])
        self.chosen['node_transfer_bytes']=16;self.save()
        with c.reserve():r.register([self.base/'new'])
