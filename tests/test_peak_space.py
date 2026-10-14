"""Peak-space planning with tiny processes/files and mocked capacity metadata."""
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
from dctlib import space as s, operation_capacity as c
import guest_state
import transfer

ID='a'*32
CHILD='''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from dctlib import space as s,operation_capacity as c
s.ROOT=Path(sys.argv[2]);s.POLICY=Path(sys.argv[3]);c.ROOT=Path(sys.argv[4]);c.POLICY=Path(sys.argv[5])
try:
 with c.reserve(),s.reserve([{'key':'fixture','free':30,'bytes':10,'name':'fixture'}]):
  print('admitted',flush=True);sys.stdin.readline()
except ValueError as error:
 print(str(error),flush=True);sys.exit(3)
'''


def target(amount,free=100): return {'key':'fixture','free':free,'bytes':amount,'name':'fixture'}


class PeakSpace(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.base=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for mod,key,value in ((s,'ROOT',self.base/'records'),(s,'POLICY',self.base/'space.json'),(c,'ROOT',self.base/'slots'),(c,'POLICY',self.base/'operations.json'),(w,'ROOT',self.base/'projects')):
            self.stack.enter_context(patch.object(mod,key,value))
        s.POLICY.write_text(json.dumps({'reserve_bytes':5,'overhead_percent':10}))
        c.POLICY.write_text(json.dumps({'max_concurrent':4}))
        self.stack.enter_context(patch.dict(os.environ));os.environ.pop(c.ENV,None)
        self.children=[];self.addCleanup(self.close_children)

    def close_children(self):
        for child in self.children:
            if child.poll() is None:child.communicate('\n',timeout=10)

    def child(self):
        child=subprocess.Popen([sys.executable,'-c',CHILD,str(Path(s.__file__).resolve().parents[1]),str(s.ROOT),str(s.POLICY),str(c.ROOT),str(c.POLICY)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(child);return child

    def test_shared_filesystem_sums_all_simultaneous_outputs(self):
        plan=s.group([target(7,20),target(8,21)])
        self.assertEqual(plan['fixture']['bytes'],15)
        self.assertEqual(plan['fixture']['free'],20)
        with c.reserve(),self.assertRaisesRegex(ValueError,'Insufficient peak'):
            with s.reserve([target(7,20),target(8,21)]):pass

    def test_independent_filesystems_have_independent_limits(self):
        other=dict(target(7,20),key='other')
        with c.reserve(),s.reserve([target(7,20),other]):pass

    def test_atomic_parallel_reservations_refuse_overcommit(self):
        first=self.child();self.assertEqual(first.stdout.readline().strip(),'admitted')
        second=self.child();out,err=second.communicate(timeout=10)
        self.assertEqual(second.returncode,3,err);self.assertIn('Insufficient peak',out)
        first.communicate('\n',timeout=10)
        with c.reserve(),s.reserve([target(10,30)]):pass

    def test_nested_work_does_not_double_reserve(self):
        with c.reserve(),s.reserve([target(10,16)]):
            with s.reserve([target(5,8)]):pass
        self.assertEqual(len(list(s.ROOT.glob('*.json'))),1)

    def test_larger_nested_plan_is_checked(self):
        with c.reserve(),s.reserve([target(10,30)]):
            with self.assertRaisesRegex(ValueError,'Insufficient peak'):
                with s.reserve([target(30,30)]):pass

    def test_fail_closed_on_unknown_measurement_and_bad_policy(self):
        for value in (None,True,-1):
            with self.assertRaises(ValueError):s.number(value)
        s.POLICY.write_text(json.dumps({'reserve_bytes':-1,'overhead_percent':10}))
        with self.assertRaises(ValueError):s.policy()

    def test_paths_on_same_real_filesystem_share_one_group(self):
        first=self.base/'one';first.mkdir()
        second=self.base/'two';second.mkdir()
        self.assertEqual(len(s.group([s.filesystem(first,7),s.filesystem(second,8)])),1)

    def test_native_pool_aliases_are_grouped(self):
        response=json.dumps({'type':'lvmthin','vgname':'fixture-vg','thinpool':'fixture-pool','pool':None,'avail':100,'active':1})
        with patch.object(w,'capture',return_value=response):
            plan=s.group([s.storage('first',7),s.storage('second',8)])
        self.assertEqual(len(plan),1);self.assertEqual(next(iter(plan.values()))['bytes'],15)

    def test_file_storage_and_docker_paths_share_filesystem_budget(self):
        response=json.dumps({'type':'dir','path':str(self.base),'avail':100,'active':1})
        with patch.object(w,'capture',return_value=response):
            plan=s.group([s.storage('local',7),s.filesystem(self.base/'docker',8)])
        self.assertEqual(len(plan),1);self.assertEqual(next(iter(plan.values()))['bytes'],15)

    def test_backup_rejects_before_pause_or_journal(self):
        with patch.object(guest_state,'operation'),patch.object(w,'locked'),patch.object(w,'detail'),patch.object(s,'source_targets',return_value=[]),patch.object(s,'claim',side_effect=ValueError('Insufficient peak')),patch.object(w,'write_json') as write,patch.object(w,'run') as run:
            with self.assertRaisesRegex(ValueError,'Insufficient peak'):guest_state.quiesce(ID,'pve','stop',str(self.base/'state'))
            write.assert_not_called();run.assert_not_called()

    def test_migration_checks_destination_before_source_changes(self):
        spec={'state_bytes':10,'image_bytes':5,'disks':[]}
        with patch.object(w,'operation'),patch.object(w,'locked'),patch.object(w,'detail',return_value={}),patch.object(w,'assert_configuration_digest'),patch.object(w,'load_pending_network',return_value={}),patch.object(transfer,'check_runtime'),patch.object(w,'recover_guest_hook'),patch.object(w,'active_network_instances',return_value=[{'service':'web','number':'1'}]),patch.object(w.project_limits,'preflight'),patch.object(s,'export_size',return_value=spec),patch.object(s,'source_targets',return_value=[]),patch.object(s,'claim'),patch.object(transfer,'destination_capacity',side_effect=ValueError('Insufficient peak on target')),patch.object(w,'action') as action,patch.object(w,'write_json') as write,patch.object(w,'run') as run:
            with self.assertRaisesRegex(ValueError,'Insufficient peak'):transfer.migrate(ID,'pve','target','',restart=1)
            action.assert_not_called();write.assert_not_called();run.assert_not_called()

    def test_resize_charges_only_growth(self):
        disk={'volid':'local:disk','size_mib':8,'additional':{}}
        with patch.object(w,'locked'),patch.object(w,'storage_config',return_value=disk),patch.object(s,'storage',side_effect=lambda sid,required:target(required)) as storage,patch.object(s,'claim'),patch.object(w,'run',return_value=subprocess.CompletedProcess([],0,'{}','')):
            w.pve_storage(ID,'resize',volid='local:disk',size_mib=9)
        self.assertEqual(storage.call_args.args[1],1048576)

    def test_transfer_loan_borrows_live_destination_reservation(self):
        key='b'*32
        with c.reserve(),s.reserve([target(10,16)],key=key):
            with s.transfer_loan(key),s.reserve([target(8,8)]):pass
        self.assertEqual(len(list(s.ROOT.glob('*.json'))),1)

    def test_destination_handshake_and_size_update_use_bounded_readiness(self):
        code="import sys; print('READY',flush=True)\nfor line in sys.stdin: print('READY',flush=True)"
        child=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(child)
        lease=transfer.DestinationCapacity(child)
        lease.ready();lease.update({'state_bytes':12,'image_bytes':7,'disks':[]})

    def test_export_counts_additional_saved_images(self):
        directory=w.ROOT/ID;directory.mkdir(parents=True)
        (directory/'images.json').write_text(json.dumps({'app':{'id':'sha256:'+'b'*64}}))
        with patch.object(w,'snapshot_containers',return_value=[{'SizeRootFs':8,'Image':'current'}]),patch.object(guest_state,'owned_mounts',return_value=[]),patch.object(w,'capture',return_value='[{"Size":12}]'):
            self.assertEqual(s.export_size(ID)['image_bytes'],20)

    def test_restore_uses_archive_size_when_larger_than_container_metadata(self):
        folder=self.base/'state';folder.mkdir()
        (folder/'containers.json').write_text(json.dumps([{'SizeRootFs':1}]))
        (folder/'images.tar').write_bytes(b'123456789012')
        with patch.object(s,'docker_root',return_value=self.base/'docker'),patch.object(s,'filesystem',side_effect=lambda path,required:target(required)) as paths,patch.object(s,'claim'):
            s.restore(ID,folder)
        self.assertEqual(paths.call_args_list[0].args[1],24)
