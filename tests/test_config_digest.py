"""Optimistic configuration checks must precede Docker and filesystem mutations."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_worker import worker

ID='e'*32
class ConfigurationDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.root=self.base/'projects'
        self.metadata=self.base/'metadata'
        self.directory=self.root/ID
        self.directory.mkdir(parents=True)
        self.metadata.mkdir()
        self.source={'name':'digest-test','mode':'compose','compose':'services: {}','environment':'SECRET=private','bridge':'vmbr0','address':'192.0.2.250'}
        worker.write_json(self.directory/'request.json',self.source)
        worker.write_json(self.metadata/(ID+'.json'),{'id':ID,'vmid':105,'node':'pve','status':'stopped'})
        self.patches=[patch.object(worker,'ROOT',self.root),patch.object(worker,'METADATA',self.metadata),patch.object(worker,'current_node',return_value='pve')]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def digest(self):return worker.detail(ID,'pve')['digest']
    def test_stable_across_key_order_metrics_and_runtime_changes(self):
        original=self.digest()
        worker.write_json(self.directory/'request.json',dict(reversed(list(self.source.items()))))
        worker.write_json(self.directory/'compose.runtime.json',{'services':{},'address':'changed'})
        worker.write_json(self.metadata/(ID+'.json'),{'id':ID,'vmid':105,'node':'pve','status':'running','metrics':{'cpu':.8}})
        self.assertEqual(original,self.digest())
    def test_all_persistent_configuration_sources_change_digest(self):
        original=self.digest()
        for name,content in [('pending.json',{'app':{'cpus':0}}),('settings.json',{'app':{'memory':128}}),('storage.json',{'size_mib':512}),('request.json',dict(self.source,environment='SECRET=other'))]:
            with self.subTest(name=name):
                worker.write_json(self.directory/name,content)
                self.assertNotEqual(original,self.digest())
                if name!='request.json':(self.directory/name).unlink()
    def test_legacy_client_and_matching_digest_are_accepted(self):
        worker.assert_configuration_digest(ID,'pve',None)
        worker.assert_configuration_digest(ID,'pve','')
        worker.assert_configuration_digest(ID,'pve',self.digest())
    def test_invalid_and_stale_digest_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'Invalid configuration digest'):
            worker.assert_configuration_digest(ID,'pve','../unsafe')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            worker.assert_configuration_digest(ID,'pve','0'*64)
    def test_every_mutation_checks_before_side_effects(self):
        calls=[lambda:worker.update(ID,'pve',{},'0'*64),lambda:worker.service_settings(ID,'pve','app',{'memory':32},'0'*64),lambda:worker.reset_settings(ID,'pve','app','0'*64),lambda:worker.project_storage_limit(ID,'pve',256,'0'*64)]
        with patch.object(worker,'locked'),patch.object(worker,'run') as run,patch.object(worker,'prepare') as prepare:
            for call in calls:
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):call()
            run.assert_not_called();prepare.assert_not_called()
        self.assertEqual(json.loads((self.directory/'request.json').read_text()),self.source)
        self.assertFalse((self.directory/'settings.json').exists())
        self.assertFalse((self.directory/'storage.ext4').exists())
    def test_worker_rechecks_after_preflight_before_locked_write(self):
        old=self.digest()
        worker.assert_configuration_digest(ID,'pve',old)
        @contextlib.contextmanager
        def changed_before_lock(*args):
            worker.write_json(self.directory/'settings.json',{'app':{'memory':64}})
            yield
        with patch.object(worker,'locked',changed_before_lock),patch.object(worker,'run') as run:
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                worker.service_settings(ID,'pve','app',{'memory':128},old)
            run.assert_not_called()
        self.assertEqual(json.loads((self.directory/'settings.json').read_text()),{'app':{'memory':64}})
    def test_digest_is_not_persisted_in_public_metadata(self):
        info=worker.detail(ID,'pve')
        info.pop('deployment')
        worker.write_json(self.metadata/(ID+'.json'),info)
        self.assertNotIn('digest',json.loads((self.metadata/(ID+'.json')).read_text()))

    def test_empty_pending_queue_preserves_legacy_digest(self):
        original=self.digest()
        worker.save_pending(self.directory, {'app': {}})
        self.assertEqual(original,self.digest())
        self.assertFalse((self.directory/'pending.json').exists())

    def test_stale_revert_fails_before_queue_mutation(self):
        worker.write_json(self.directory/'compose.runtime.json', {'services': {'app': {}}})
        worker.save_pending(self.directory, {'app': {'cpus': 1}})
        original=(self.directory/'pending.json').read_bytes()
        with patch.object(worker, 'locked'), self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            worker.revert_settings(ID, 'pve', 'app', 'cpus', '0'*64)
        self.assertEqual(original,(self.directory/'pending.json').read_bytes())
