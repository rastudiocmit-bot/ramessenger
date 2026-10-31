"""Exact third-container transition and fail-closed pin checks; no real Docker."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import max_runtime_deploy as runtime
import telephony_runtime as phone
import test_max_runtime_deploy as fixture
SHA,OLD,NEW=fixture.SHA,fixture.OLD,fixture.NEW


class TelephonyRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture.WorkerUpdateTests.setUp(self)
        self.source.joinpath('compose.yaml').write_text('reviewed compose with a telephony container')
        migration='app/backend/migrations/versions/0031_telephony_calls.py'
        path=self.source/migration;path.parent.mkdir(parents=True);path.write_text('additive; rollback preserves calls')
        self.policy={'schema_revision':'0031_telephony_calls','worker_runtime_contract':'schema-0031',
            'telephony_runtime_contract':'telephony-calls-v1',
            'telephony_install':{'from_compose':runtime.digest(self.root/'compose.yaml')},
            'protected':{'compose.yaml':runtime.digest(self.source/'compose.yaml'),migration:runtime.digest(path)},
            'paired_schema_update':{'from':'0029_invitation_sections','to':'0031_telephony_calls',
                                   'migration':migration,'rollback_preserves_data':True}}
        self.source.joinpath('deploy/max-protection.json').write_text(json.dumps(self.policy))

    inspect=fixture.WorkerUpdateTests.inspect
    run_command=fixture.WorkerUpdateTests.run_command

    def prepare(self):
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command),patch.object(phone,'install_key'):
            runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(self.source/'deploy/max-protection.json'),schema_from='0029_invitation_sections',install_telephony=True)

    def test_compose_change_requires_an_explicit_telephony_and_schema_plan(self):
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            with self.assertRaises(RuntimeError):runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(self.source/'deploy/max-protection.json'))
        self.prepare()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            runtime.validate_transition(self.root,self.source)
            self.assertTrue(runtime.allows_schema_update(self.root,self.source,SHA,'0029_invitation_sections','0031_telephony_calls'))
        self.assertEqual(self.root.joinpath('compose.yaml').read_text(),'original compose')
        self.assertFalse(any('up' in call or 'stop' in call for call in self.calls))

    def test_worker_is_installed_only_inside_the_existing_promoter_after_migration(self):
        self.prepare()
        plan=json.loads(self.root.joinpath(runtime.UPDATE).read_text());plan.update(phase='migrating',new_api=NEW,new_api_id='sha256:new-image');runtime.save_update(self.root,plan)
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command), \
             patch.object(runtime,'schema_revision',return_value='0031_telephony_calls'), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)), \
             patch.object(runtime,'wait_worker',return_value=True),patch.object(phone,'wait',return_value=True):
            runtime.promote_update(self.root,self.source,SHA,OLD,NEW,self.backup,Mock())
        self.assertTrue(any(c[-1]=='telephony-worker' and 'up' in c for c in self.calls))
        state=json.loads(self.root.joinpath(phone.STATE).read_text())
        self.assertEqual(state['worker_image'],'center-messenger:telephony-'+SHA[:12])
        self.assertEqual(self.root.joinpath('compose.yaml').read_bytes(),self.source.joinpath('compose.yaml').read_bytes())
        before=len(self.calls)
        with patch.object(runtime,'run',side_effect=self.run_command):
            self.assertFalse(runtime.promote(self.root,self.source,'c'*40,OLD,NEW,self.backup,Mock()))
        self.assertEqual(before,len(self.calls),'An ordinary API release cannot recreate either pinned worker')

    def test_ordinary_release_rejects_a_replaced_telephony_pin(self):
        self.root.joinpath(phone.STATE).write_text(json.dumps({'phase':'complete','worker_id':'image-id','worker_image':'frozen-phone','contract':'telephony-calls-v1'}))
        self.root.joinpath('.env').write_text('CM_TELEPHONY_IMAGE=someone-else\n')
        fake=Mock(inspect=Mock(return_value={'Image':'image-id','Config':{'Image':'frozen-phone'}}))
        with self.assertRaisesRegex(RuntimeError,'Pinned telephony'):phone.verify(self.root,self.source,fake)

    def test_rollback_never_removes_a_foreign_container(self):
        fake=Mock(run=Mock(return_value=phone.NAME),inspect=Mock(return_value={'Image':'other'}))
        with self.assertRaisesRegex(RuntimeError,'identity changed'):phone.remove_new(self.root,{'worker_id':'ours','telephony_image':'frozen'},fake)
        self.assertEqual(fake.run.call_count,1)

    def test_failed_promotion_restores_original_configuration_and_removes_only_its_container(self):
        self.prepare()
        plan=json.loads(self.root.joinpath(runtime.UPDATE).read_text());plan.update(phase='migrating',new_api=NEW,new_api_id='sha256:new-image');runtime.save_update(self.root,plan)
        original_env=self.root.joinpath('.env').read_bytes()
        def inspect(name):
            if name==phone.NAME:return {'Id':'created-phone','Image':'sha256:new-image','Config':{'Image':'center-messenger:telephony-'+SHA[:12],'Labels':{'com.docker.compose.project':'center-messenger'}}}
            return self.inspect(name)
        def run(*args,**kwargs):
            if args[:2]==('docker','ps'):return runtime.NAME+'\ncenter-messenger-api-1\n'+phone.NAME
            return self.run_command(*args,**kwargs)
        promoter=Mock();promoter.deploy_api_with_rollback.side_effect=RuntimeError('API did not become ready')
        with patch.object(runtime,'inspect',side_effect=inspect),patch.object(runtime,'run',side_effect=run), \
             patch.object(runtime,'schema_revision',return_value='0031_telephony_calls'), \
             patch.object(runtime,'restore_schema') as restore, \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)), \
             patch.object(runtime,'wait_worker',return_value=True),patch.object(phone,'wait',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'API did not'):runtime.promote_update(self.root,self.source,SHA,OLD,NEW,self.backup,promoter)
        restore.assert_called_once()
        self.assertEqual(self.root.joinpath('.env').read_bytes(),original_env)
        self.assertEqual(self.root.joinpath('compose.yaml').read_text(),'original compose')
        self.assertIn(('docker','rm','-f','created-phone'),self.calls)
        self.assertFalse(self.root.joinpath(phone.STATE).exists())
        self.assertEqual(self.backup.read_bytes(),b'x'*2048)


if __name__=='__main__':unittest.main()
