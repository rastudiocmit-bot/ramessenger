"""Exact third-container transition and fail-closed pin checks; no real Docker."""
import json
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
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


class TelephonyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'live';self.root.mkdir();(self.root/'rollback').mkdir()
        self.source=Path(self.temp.name)/'source';(self.source/'deploy').mkdir(parents=True)
        self.sha='a'*40;self.old_sha='b'*40
        self.old_api='center-messenger:local-'+self.old_sha[:12]
        self.new_api='center-messenger:local-'+self.sha[:12]
        self.old_phone='center-messenger:telephony-'+self.old_sha[:12]
        self.new_phone='center-messenger:telephony-'+self.sha[:12]
        self.max_image='center-messenger:max-pinned'
        self.old_env=('CM_MAX_IMAGE='+self.max_image+'\nCM_TELEPHONY_IMAGE='+self.old_phone+'\n').encode()
        (self.root/'.env').write_bytes(self.old_env)
        for path in (self.root/'compose.yaml',self.source/'compose.yaml'):path.write_text('unchanged compose')
        self.policy={'schema_revision':'0031_telephony_calls','telephony_runtime_contract':'new-phone-contract',
                     'protected':{'compose.yaml':runtime.digest(self.source/'compose.yaml')}}
        (self.source/'deploy/max-protection.json').write_text(json.dumps(self.policy))
        self.old_state={'phase':'complete','sha':self.old_sha,'worker_image':self.old_phone,'worker_id':'old-phone-id',
                        'contract':'old-phone-contract','schema_revision':'0031_telephony_calls'}
        (self.root/phone.STATE).write_text(json.dumps(self.old_state))
        def info(tag,identity,role):
            return {'Id':identity+'-container','Image':identity,'Config':{'Image':tag,'Env':['CM_RUNTIME_ROLE='+role]},
                    'State':{'Status':'running','Health':{'Status':'healthy'}}}
        self.processes={'center-messenger-api-1':info(self.old_api,'api-id','api'),
                        phone.NAME:info(self.old_phone,'old-phone-id','telephony'),
                        'center-messenger-max-worker-1':info(self.max_image,'fixed-max-id','max')}
        self.max_before=copy.deepcopy(self.processes['center-messenger-max-worker-1'])
        self.calls=[];self.revision='0031_telephony_calls'
        self.phone_present=True
        self.runtime=SimpleNamespace(write=runtime.write,digest=runtime.digest,run=self.run_command,
            inspect=lambda name:copy.deepcopy(self.processes[name]),schema_revision=lambda:self.revision)
        self.backup=self.root/'rollback/database.dump';self.backup.write_bytes(b'd'*2048)
        def deploy(old,new,backup):self.processes['center-messenger-api-1']['Config']['Image']=new
        self.promoter=Mock();self.promoter.deploy_api_with_rollback.side_effect=deploy
        self.promoter.wait_ready.return_value=True

    def run_command(self,*args,**kwargs):
        self.calls.append(args)
        if args[:1]==('git',):return self.sha
        if args[:2]==('docker','ps'):return phone.NAME if self.phone_present else ''
        if args[:3]==('docker','image','inspect'):return json.dumps([{'Id':'new-phone-id'}])
        if args[:2]==('docker','compose'):
            if args[-1]==phone.NAME or args[-1]=='telephony-worker':
                image=kwargs['env']['CM_TELEPHONY_IMAGE']
                self.processes[phone.NAME]['Config']['Image']=image
                self.processes[phone.NAME]['Image']='old-phone-id' if image==self.old_phone else 'new-phone-id'
                self.phone_present=True
            elif args[-1]=='api':self.processes['center-messenger-api-1']['Config']['Image']=kwargs['env']['CM_IMAGE']
        return ''

    def prepare(self):
        phone.prepare_update(self.root,self.source,self.sha,self.old_api,self.old_phone,self.max_image,
                             runtime.digest(self.source/'deploy/max-protection.json'),self.runtime)

    def promote(self):
        with patch.object(phone.subprocess,'run',return_value=Mock(returncode=1)):
            return phone.promote_update(self.root,self.source,self.sha,self.old_api,self.new_api,self.backup,self.promoter,self.runtime)

    def test_preparation_changes_no_running_processes_environment_or_max_pin(self):
        self.prepare()
        self.assertEqual((self.root/'.env').read_bytes(),self.old_env)
        self.assertEqual(json.loads((self.root/phone.STATE).read_text()),self.old_state)
        self.assertFalse(any('up' in call or 'stop' in call for call in self.calls))
        self.assertEqual(phone.prepared(self.root,self.source,self.sha,self.runtime)['phase'],'prepared')

    def test_phone_and_api_promote_with_the_max_container_untouched(self):
        self.prepare();self.assertTrue(self.promote())
        self.assertEqual(self.processes['center-messenger-max-worker-1'],self.max_before)
        self.assertEqual(json.loads((self.root/phone.STATE).read_text())['worker_image'],self.new_phone)
        self.assertEqual(json.loads((self.root/phone.UPDATE).read_text())['phase'],'complete')
        self.assertEqual([c[-1] for c in self.calls if c[:2]==('docker','compose')],['telephony-worker'])
        self.promoter.deploy_api_with_rollback.assert_called_once_with(self.old_api,self.new_api,self.backup)
        before=len(self.calls)
        self.assertFalse(phone.promote_update(self.root,self.source,'c'*40,self.new_api,'other-api',self.backup,self.promoter,self.runtime))
        self.assertEqual(len(self.calls),before)
        phone.verify(self.root,self.source,self.runtime)

    def test_a_failed_api_promotion_restores_phone_configuration_without_database_or_max_changes(self):
        self.prepare();self.promoter.deploy_api_with_rollback.side_effect=RuntimeError('synthetic API failure')
        with self.assertRaisesRegex(RuntimeError,'synthetic API failure'):self.promote()
        self.assertEqual((self.root/'.env').read_bytes(),self.old_env)
        self.assertEqual(json.loads((self.root/phone.STATE).read_text()),self.old_state)
        self.assertEqual(self.processes[phone.NAME]['Config']['Image'],self.old_phone)
        self.assertEqual(self.processes['center-messenger-max-worker-1'],self.max_before)
        self.assertEqual(json.loads((self.root/phone.UPDATE).read_text())['phase'],'failed')
        self.assertFalse(any('psql' in call or 'alembic' in call or 'rm' in call for call in self.calls))
        self.assertEqual(self.backup.read_bytes(),b'd'*2048)

    def test_worker_readiness_failure_rolls_back_before_api_promotion(self):
        self.prepare()
        with patch.object(phone,'wait',side_effect=[False,True]):
            with self.assertRaisesRegex(RuntimeError,'not ready'):self.promote()
        self.promoter.deploy_api_with_rollback.assert_not_called()
        self.assertEqual((self.root/'.env').read_bytes(),self.old_env)

    def test_a_crash_leaves_a_recoverable_exact_plan(self):
        self.prepare();self.promoter.deploy_api_with_rollback.side_effect=RuntimeError('synthetic crash')
        with patch.object(phone,'recover_update'):
            with self.assertRaises(RuntimeError):self.promote()
        self.assertEqual(json.loads((self.root/phone.UPDATE).read_text())['phase'],'promoting')
        phone.recover_update(self.root,self.promoter,self.runtime)
        self.assertEqual((self.root/'.env').read_bytes(),self.old_env)
        self.assertEqual(json.loads((self.root/phone.UPDATE).read_text())['phase'],'failed')

    def test_a_foreign_max_restart_blocks_without_recreating_it(self):
        self.prepare();self.processes['center-messenger-max-worker-1']['Id']='someone-else'
        with self.assertRaisesRegex(RuntimeError,'MAX changed'):self.promote()
        self.assertFalse(any('up' in call for call in self.calls))

    def test_recovery_recreates_a_phone_container_missing_after_a_crash(self):
        self.prepare();self.promoter.deploy_api_with_rollback.side_effect=RuntimeError('synthetic crash')
        with patch.object(phone,'recover_update'):
            with self.assertRaises(RuntimeError):self.promote()
        self.phone_present=False
        phone.recover_update(self.root,self.promoter,self.runtime)
        self.assertTrue(self.phone_present)
        self.assertEqual(self.processes[phone.NAME]['Config']['Image'],self.old_phone)
        self.assertEqual(self.processes['center-messenger-max-worker-1'],self.max_before)

    def test_missing_backup_or_changed_schema_blocks_before_promotion(self):
        self.prepare();self.backup.unlink()
        with self.assertRaisesRegex(RuntimeError,'backup required'):self.promote()
        self.revision='0032_unrelated'
        with self.assertRaisesRegex(RuntimeError,'schema'):self.promote()
        self.assertEqual((self.root/'.env').read_bytes(),self.old_env)


class ThreeProcessSchemaTests(unittest.TestCase):
    def setUp(self):
        fixture.WorkerUpdateTests.setUp(self)
        self.old_phone='center-messenger:telephony-old'
        self.phone={'Id':'old-phone-container','Image':'old-phone-id','Config':{'Image':self.old_phone,
            'Env':['CM_RUNTIME_ROLE=telephony'],'Labels':{'com.docker.compose.project':'center-messenger'}},'State':{'Status':'running','Health':{'Status':'healthy'}}}
        (self.root/'.env').write_text((self.root/'.env').read_text()+'CM_TELEPHONY_IMAGE='+self.old_phone+'\n')
        (self.root/phone.STATE).write_text(json.dumps({'phase':'complete','worker_image':self.old_phone,'worker_id':'old-phone-id','schema_revision':'0031_telephony_calls','contract':'old-phone-contract'}))
        migration='app/backend/migrations/versions/0032_telephony_ai.py'
        path=self.source/migration;path.parent.mkdir(parents=True);path.write_text('retained additive AI tables')
        self.policy={'schema_revision':'0032_telephony_ai','worker_runtime_contract':'schema-32','telephony_runtime_contract':'phone-ai-v1',
            'protected':{migration:runtime.digest(path),'compose.yaml':runtime.digest(self.source/'compose.yaml')},
            'paired_schema_update':{'from':'0031_telephony_calls','to':'0032_telephony_ai','migration':migration,'rollback_preserves_data':True}}
        (self.source/'deploy/max-protection.json').write_text(json.dumps(self.policy))
        self.revision='0031_telephony_calls';self.original_env=(self.root/'.env').read_bytes()
        self.original_phone=(self.root/phone.STATE).read_bytes()
        self.promoter=Mock();self.promoter.wait_ready.return_value=True

    def inspect(self,name):
        if name==runtime.NAME:return self.worker
        if name==phone.NAME:return self.phone
        return self.api

    def command(self,*args,**kwargs):
        self.calls.append(args)
        if args[:1]==('git',):return SHA
        if args[:2]==('docker','exec'):return self.revision
        if args[:3]==('docker','image','inspect'):return '[{"Id":"sha256:new-image"}]'
        if args[:2]==('docker','ps'):return phone.NAME if '--filter' in args else runtime.NAME+'\ncenter-messenger-api-1\n'+phone.NAME
        if args[:2]==('docker','compose') and 'up' in args:
            target=args[-1];env=kwargs['env']
            if target=='max-worker':self.worker['Config']['Image']=env['CM_MAX_IMAGE'];self.worker['Image']='sha256:old-worker' if env['CM_MAX_IMAGE']==self.max_image else 'sha256:new-image'
            if target=='telephony-worker':self.phone['Config']['Image']=env['CM_TELEPHONY_IMAGE'];self.phone['Image']='old-phone-id' if env['CM_TELEPHONY_IMAGE']==self.old_phone else 'sha256:new-image'
            if target=='api':self.api['Config']['Image']=env['CM_IMAGE']
        return ''

    def context(self):
        from contextlib import ExitStack
        stack=ExitStack()
        for target,name,value in [(runtime,'run',self.command),(runtime,'inspect',self.inspect),
                                  (runtime,'schema_revision',lambda:self.revision)]:
            stack.enter_context(patch.object(target,name,side_effect=value))
        stack.enter_context(patch.object(runtime,'wait_worker',return_value=True))
        stack.enter_context(patch.object(phone,'wait',return_value=True))
        stack.enter_context(patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)))
        return stack

    def prepare(self):
        with self.context():runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(self.source/'deploy/max-protection.json'),schema_from='0031_telephony_calls',update_telephony=True,expected_telephony=self.old_phone)

    def migrating(self):
        with self.context():runtime.begin_schema_update(self.root,self.source,SHA,NEW,self.backup)
        self.revision='0032_telephony_ai'

    def test_schema_change_is_blocked_without_all_three_exact_pins(self):
        with self.context():
            with self.assertRaisesRegex(RuntimeError,'three-process'):
                runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(self.source/'deploy/max-protection.json'),schema_from='0031_telephony_calls')
            with self.assertRaises(RuntimeError):
                runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(self.source/'deploy/max-protection.json'),schema_from='0031_telephony_calls',update_telephony=True,expected_telephony='wrong')
        self.assertEqual((self.root/'.env').read_bytes(),self.original_env)

    def test_preparation_changes_no_container_and_admits_only_the_reviewed_schema(self):
        self.prepare()
        with self.context():
            self.assertTrue(runtime.allows_schema_update(self.root,self.source,SHA,'0031_telephony_calls','0032_telephony_ai'))
            runtime.verify_runtime_layout(self.root,self.source)
        self.assertFalse(any('up' in command for command in self.calls))

    def test_promotion_updates_phone_and_max_for_the_shared_readiness_revision(self):
        self.prepare();self.migrating()
        with self.context():runtime.promote_update(self.root,self.source,SHA,OLD,NEW,self.backup,self.promoter)
        self.assertEqual(json.loads((self.root/phone.STATE).read_text())['schema_revision'],'0032_telephony_ai')
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'complete')
        self.assertEqual([command[-1] for command in self.calls if command[:2]==('docker','compose') and 'up' in command],['max-worker','telephony-worker'])
        before=len(self.calls)
        with self.context():runtime.verify_runtime_layout(self.root,self.source)
        self.assertFalse(any('up' in command for command in self.calls[before:]))

    def test_failed_api_restores_both_original_worker_pins_without_restoring_a_dump(self):
        self.prepare();self.migrating();self.promoter.deploy_api_with_rollback.side_effect=RuntimeError('synthetic API failure')
        def restore(*_):self.revision='0031_telephony_calls'
        with self.context(),patch.object(runtime,'restore_schema',side_effect=restore):
            with self.assertRaisesRegex(RuntimeError,'synthetic API failure'):
                runtime.promote_update(self.root,self.source,SHA,OLD,NEW,self.backup,self.promoter)
        self.assertEqual((self.root/'.env').read_bytes(),self.original_env)
        self.assertEqual((self.root/phone.STATE).read_bytes(),self.original_phone)
        self.assertFalse(any('pg_restore' in command for command in self.calls))

    def test_migration_interruption_checks_phone_identity_before_recovery(self):
        self.prepare();self.migrating();self.phone['Image']='someone-else'
        with self.context(),patch.object(runtime,'restore_schema') as restore:
            with self.assertRaises(RuntimeError):runtime.recover_update(self.root,self.promoter)
            restore.assert_not_called()

    def test_crash_after_phone_metadata_commit_recovers_both_original_states(self):
        self.prepare();self.migrating();original_write=runtime.write;failed=False
        def write(path,body,**kwargs):
            nonlocal failed
            if path==self.root/runtime.PLAN and not failed:
                failed=True;raise RuntimeError('synthetic metadata crash')
            return original_write(path,body,**kwargs)
        def restore(*_):self.revision='0031_telephony_calls'
        with self.context(),patch.object(runtime,'write',side_effect=write),patch.object(runtime,'restore_schema',side_effect=restore):
            with self.assertRaisesRegex(RuntimeError,'metadata crash'):
                runtime.promote_update(self.root,self.source,SHA,OLD,NEW,self.backup,self.promoter)
        self.assertEqual((self.root/phone.STATE).read_bytes(),self.original_phone)
        self.assertEqual((self.root/'.env').read_bytes(),self.original_env)


if __name__=='__main__':unittest.main()
