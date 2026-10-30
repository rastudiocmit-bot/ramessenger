"""Transition, interrupted promotion and rollback; no real Docker/VPS calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import max_runtime_deploy as runtime

SHA = 'a'*40
OLD = 'center-messenger:local-' + 'b'*12
NEW = 'center-messenger:local-' + SHA[:12]


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'live'
        self.source = Path(self.temp.name) / 'source'
        self.root.mkdir(); self.source.mkdir()
        (self.root / 'compose.yaml').write_text('original compose')
        (self.source / 'compose.yaml').write_text('split compose')
        (self.root / '.env').write_text('KEEP_EXISTING=value\n')
        (self.root / 'rollback').mkdir()
        self.backup = self.root / 'rollback/db.dump'
        self.backup.write_bytes(b'x'*2048)
        self.plan = {'version': 1, 'phase': 'prepared', 'sha': SHA, 'old_api': OLD,
                     'old_compose': runtime.digest(self.root / 'compose.yaml'),
                     'new_compose': runtime.digest(self.source / 'compose.yaml')}
        runtime.save(self.root, self.plan)

    def test_compose_change_requires_exact_prepared_sha_and_both_checksums(self):
        self.assertEqual(runtime.plan_for(self.root, self.source, SHA), self.plan)
        with self.assertRaises(RuntimeError): runtime.plan_for(self.root, self.source, 'c'*40)
        (self.root / 'compose.yaml').write_text('changed behind deployment owner')
        with self.assertRaises(RuntimeError): runtime.plan_for(self.root, self.source, SHA)

    def test_without_plan_no_compose_exception_is_granted(self):
        (self.root / runtime.PLAN).unlink()
        with patch.object(runtime, 'run', return_value=SHA):
            with self.assertRaises(RuntimeError): runtime.validate_transition(self.root, self.source)
        with self.assertRaises(RuntimeError): runtime.verify_runtime_layout(self.root, self.source)

    def test_completed_runtime_pin_cannot_be_changed_by_an_api_release(self):
        self.plan.update(phase='complete', worker_image='frozen-worker', worker_id='sha256:pinned')
        runtime.save(self.root, self.plan)
        (self.root / '.env').write_text('CM_MAX_IMAGE=frozen-worker\n')
        worker = {'Image': 'sha256:pinned', 'Config': {'Image': 'frozen-worker'}}
        with patch.object(runtime, 'inspect', return_value=worker):
            runtime.verify_runtime_layout(self.root, self.source)
            (self.root / '.env').write_text('CM_MAX_IMAGE=changed-worker\n')
            with self.assertRaisesRegex(RuntimeError, 'setting changed'):
                runtime.verify_runtime_layout(self.root, self.source)
        worker['Image'] = 'sha256:reassigned'
        with patch.object(runtime, 'inspect', return_value=worker):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                runtime.verify_runtime_layout(self.root, self.source)

    def test_runtime_image_replacement_keeps_unrelated_configuration(self):
        value = runtime.with_image(b'PUBLIC=https://example.com\nCM_MAX_IMAGE=old\nOTHER=secret\n', 'center-messenger:max-new')
        self.assertIn(b'OTHER=secret', value)
        self.assertEqual(value.count(b'CM_MAX_IMAGE='), 1)
        self.assertIn(b'CM_MAX_IMAGE=center-messenger:max-new', value)

    def test_success_starts_only_worker_then_uses_existing_api_promoter(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return '[{"Id":"sha256:new-image"}]' if args[:3] == ('docker', 'image', 'inspect') else ''
        promoter = Mock()
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}}), \
             patch.object(runtime, 'run', side_effect=run), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(runtime, 'wait_worker', return_value=True):
            self.assertTrue(runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, promoter))
        promoter.deploy_api_with_rollback.assert_called_once_with(OLD, NEW, self.backup)
        self.assertTrue(any(c[-1] == 'max-worker' and 'up' in c for c in calls))
        self.assertFalse(any('db' == c[-1] for c in calls))
        self.assertIn('CM_MAX_IMAGE=center-messenger:max-' + SHA[:12], (self.root / '.env').read_text())
        self.assertEqual(json.loads((self.root / runtime.PLAN).read_text())['phase'], 'complete')

    def test_worker_failure_restores_compose_api_and_env_without_restoring_database(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return '[{"Id":"sha256:new-image"}]' if args[:3] == ('docker', 'image', 'inspect') else ''
        promoter = Mock()
        promoter.wait_ready.return_value = True
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}}), \
             patch.object(runtime, 'run', side_effect=run), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(runtime, 'wait_worker', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, promoter)
        self.assertEqual((self.root / 'compose.yaml').read_text(), 'original compose')
        self.assertEqual((self.root / '.env').read_text(), 'KEEP_EXISTING=value\n')
        self.assertEqual(json.loads((self.root / runtime.PLAN).read_text())['phase'], 'failed')
        promoter.deploy_api_with_rollback.assert_not_called()
        self.assertFalse(any('pg_restore' in c or 'migrate' in c for c in calls))

    def test_api_failure_uses_same_recovery_and_retains_new_message_storage(self):
        promoter = Mock()
        promoter.deploy_api_with_rollback.side_effect = RuntimeError('API failed')
        promoter.wait_ready.return_value = True
        def run(*args, **kwargs):
            return '[{"Id":"sha256:new-image"}]' if args[:3] == ('docker', 'image', 'inspect') else ''
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}}), \
             patch.object(runtime, 'run', side_effect=run), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(runtime, 'wait_worker', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'API failed'):
                runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, promoter)
        self.assertEqual((self.root / 'compose.yaml').read_text(), 'original compose')
        self.assertEqual(self.backup.read_bytes(), b'x'*2048)

    def test_worker_death_during_api_promotion_is_not_reported_as_success(self):
        promoter = Mock()
        promoter.wait_ready.return_value = True
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}}), \
             patch.object(runtime, 'run', return_value='[{"Id":"sha256:new-image"}]'), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(runtime, 'wait_worker', side_effect=[True, False]):
            with self.assertRaisesRegex(RuntimeError, 'stopped during API'):
                runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, promoter)
        self.assertEqual(json.loads((self.root / runtime.PLAN).read_text())['phase'], 'failed')

    def test_pinned_image_cannot_be_overwritten_or_live_api_changed(self):
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': 'unexpected'}}):
            with self.assertRaisesRegex(RuntimeError, 'API changed'):
                runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, Mock())
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}}), \
             patch.object(runtime, 'run', return_value='[{"Id":"new"}]'), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=0, stdout='[{"Id":"different"}]')):
            with self.assertRaisesRegex(RuntimeError, 'Refusing to overwrite'):
                runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, Mock())

    def test_completed_transition_never_recreates_the_frozen_worker(self):
        self.plan['phase'] = 'complete'
        runtime.save(self.root, self.plan)
        with patch.object(runtime, 'run') as run:
            self.assertFalse(runtime.promote(self.root, self.source, 'c'*40, OLD, NEW, self.backup, Mock()))
        run.assert_not_called()

    def test_preparation_creates_only_secret_and_plan_without_starting_containers(self):
        (self.source / 'deploy').mkdir()
        policy = self.source / 'deploy/max-protection.json'
        policy.write_text('{}')
        old_read = Path.read_text
        def read(path, *args, **kwargs):
            return 'MemTotal: 4194304 kB\nMemAvailable: 2097152 kB\n' if str(path) == '/proc/meminfo' else old_read(path, *args, **kwargs)
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}, 'State': {'Status': 'running'}}), \
             patch.object(runtime, 'run', return_value=SHA) as run, \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(runtime.os, 'chown') as owner, patch.object(Path, 'read_text', read):
            runtime.prepare(self.root, self.source, SHA, OLD, runtime.digest(policy))
        self.assertEqual(run.call_args.args[:2], ('git', '-C'))
        self.assertEqual((self.root / 'compose.yaml').read_text(), 'original compose')
        self.assertEqual(len((self.root / '.secrets/max_worker_key').read_text().strip()), 64)
        owner.assert_called_once_with(self.root / '.secrets/max_worker_key', 10001, 10001)

    def test_foreign_configuration_change_is_preserved_on_recovery(self):
        promoter = Mock()
        def run(*args, **kwargs):
            return '[{"Id":"sha256:new-image"}]' if args[:3] == ('docker', 'image', 'inspect') else ''
        def fail(*args):
            (self.root / '.env').write_text('OTHER_TASK_CHANGE=keep\n')
            raise RuntimeError('interrupted')
        promoter.deploy_api_with_rollback.side_effect = fail
        with patch.object(runtime, 'inspect', return_value={'Config': {'Image': OLD}}), \
             patch.object(runtime, 'run', side_effect=run), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(runtime, 'wait_worker', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'outside the MAX transition'):
                runtime.promote(self.root, self.source, SHA, OLD, NEW, self.backup, promoter)
        self.assertEqual((self.root / '.env').read_text(), 'OTHER_TASK_CHANGE=keep\n')


class WorkerUpdateTests(unittest.TestCase):
    def setUp(self):
        TransitionTests.setUp(self)
        self.max_image = 'center-messenger:max-' + 'b'*12
        self.worker = {'Id':'old-worker-container', 'Image':'sha256:old-worker',
            'Config':{'Image':self.max_image, 'Env':['CM_RUNTIME_ROLE=max'],
                      'Labels':{'com.docker.compose.project':'center-messenger'}}, 'State':{'Status':'running'}}
        self.api = {'Config':{'Image':OLD,'Env':['CM_RUNTIME_ROLE=api']}, 'State':{'Status':'running'}}
        self.plan.update(phase='complete', worker_id=self.worker['Image'],worker_image=self.max_image)
        runtime.save(self.root,self.plan)
        (self.root/'.env').write_text('KEEP_EXISTING=value\nCM_MAX_IMAGE='+self.max_image+'\n')
        (self.source/'compose.yaml').write_bytes((self.root/'compose.yaml').read_bytes())
        (self.source/'deploy').mkdir()
        (self.source/'deploy/max-protection.json').write_text(json.dumps(
            {'worker_runtime_contract':'native-history-role-v1','schema_revision':'0029_invitation_sections'}))
        self.calls=[]

    def inspect(self,name):
        return self.worker if name==runtime.NAME else self.api

    def run_command(self,*args,**kwargs):
        self.calls.append(args)
        if args[0]=='git': return SHA
        if args[:3]==('docker','image','inspect'): return '[{"Id":"sha256:new-image"}]'
        if args[:2]==('docker','ps'): return runtime.NAME+'\ncenter-messenger-api-1'
        if args[:2]==('docker','exec'): return '0029_invitation_sections'
        return ''

    def prepare(self):
        with patch.object(runtime,'inspect',side_effect=self.inspect), patch.object(runtime,'run',side_effect=self.run_command):
            runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,
                                   runtime.digest(self.source/'deploy/max-protection.json'))

    def test_prepare_records_exact_existing_worker_and_does_not_recreate_it(self):
        before=(self.root/'.env').read_bytes()
        self.prepare()
        self.assertEqual((self.root/'.env').read_bytes(),before)
        plan=json.loads((self.root/runtime.UPDATE).read_text())
        self.assertEqual(plan['old_container'],'old-worker-container')
        self.assertEqual(plan['sha'],SHA)
        self.assertFalse(any('up' in c or 'stop' in c or 'migrate' in c for c in self.calls))

    def test_missing_update_plan_blocks_new_worker_contract(self):
        with patch.object(runtime,'inspect',side_effect=self.inspect):
            with self.assertRaisesRegex(RuntimeError,'explicitly prepared'):
                runtime.verify_runtime_layout(self.root,self.source)
        self.prepare()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            runtime.verify_runtime_layout(self.root,self.source)

    def test_wrong_sha_or_changed_pin_cannot_reuse_worker_update(self):
        self.prepare()
        with patch.object(runtime,'inspect',side_effect=self.inspect):
            with self.assertRaises(RuntimeError): runtime.update_for(self.root,self.source,'c'*40)
            (self.root/'.env').write_text('someone-else=keep\n')
            with self.assertRaisesRegex(RuntimeError,'configuration changed'):
                runtime.update_for(self.root,self.source,SHA)

    def test_success_recreates_only_worker_then_api_and_updates_pin(self):
        self.prepare()
        promoter=Mock()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)),patch.object(runtime,'wait_worker',return_value=True):
            self.assertTrue(runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,promoter))
        promoter.deploy_api_with_rollback.assert_called_once_with(OLD,NEW,self.backup)
        state=json.loads((self.root/runtime.PLAN).read_text())
        self.assertEqual(state['worker_id'],'sha256:new-image')
        self.assertEqual(state['worker_runtime_contract'],'native-history-role-v1')
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'complete')
        self.assertTrue(any(c[-1]=='max-worker' and '--force-recreate' in c for c in self.calls))
        self.assertFalse(any('db'==c[-1] or 'pg_restore' in c for c in self.calls))

    def test_api_failure_restores_both_images_pin_and_state_without_database_restore(self):
        self.prepare()
        original=(self.root/runtime.PLAN).read_bytes()
        promoter=Mock()
        promoter.deploy_api_with_rollback.side_effect=RuntimeError('API failed')
        promoter.wait_ready.return_value=True
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)),patch.object(runtime,'wait_worker',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'API failed'):
                runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,promoter)
        self.assertEqual((self.root/runtime.PLAN).read_bytes(),original)
        self.assertIn('CM_MAX_IMAGE='+self.max_image,(self.root/'.env').read_text())
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'failed')
        self.assertFalse(any('pg_restore' in c or 'migrate' in c for c in self.calls))

    def test_worker_start_failure_recovers_even_if_container_is_temporarily_absent(self):
        self.prepare()
        promoter=Mock()
        promoter.wait_ready.return_value=True
        def run(*args,**kwargs):
            return '' if args[:2]==('docker','ps') else self.run_command(*args,**kwargs)
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=run), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)), \
             patch.object(runtime,'wait_worker',side_effect=[False,True]):
            with self.assertRaisesRegex(RuntimeError,'did not become ready'):
                runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,promoter)
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'failed')

    def test_foreign_config_is_preserved_when_update_fails(self):
        self.prepare()
        promoter=Mock()
        def fail(*args):
            (self.root/'.env').write_text('OTHER_TASK_CHANGE=keep\n')
            raise RuntimeError('API failed')
        promoter.deploy_api_with_rollback.side_effect=fail
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)),patch.object(runtime,'wait_worker',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'outside its update'):
                runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,promoter)
        self.assertEqual((self.root/'.env').read_text(),'OTHER_TASK_CHANGE=keep\n')

    def test_next_api_release_does_not_recreate_updated_worker(self):
        self.prepare()
        plan=json.loads((self.root/runtime.UPDATE).read_text())
        plan['phase']='complete'
        runtime.save_update(self.root,plan)
        self.plan['worker_runtime_contract']='native-history-role-v1'
        runtime.save(self.root,self.plan)
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command) as run:
            runtime.verify_runtime_layout(self.root,self.source)
            self.assertFalse(runtime.promote(self.root,self.source,'c'*40,OLD,NEW,self.backup,Mock()))
        self.assertFalse(any('up' in c.args for c in run.call_args_list))

    def test_interrupted_update_is_recovered_from_durable_intent(self):
        self.prepare()
        original=(self.root/runtime.PLAN).read_bytes()
        promoter=Mock()
        promoter.wait_ready.return_value=True
        promoter.deploy_api_with_rollback.side_effect=KeyboardInterrupt()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)),patch.object(runtime,'wait_worker',return_value=True):
            with self.assertRaises(KeyboardInterrupt):
                runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,promoter)
            self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'promoting')
            runtime.recover(self.root,promoter)
        self.assertEqual((self.root/runtime.PLAN).read_bytes(),original)
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'failed')
        self.assertIn('CM_MAX_IMAGE='+self.max_image,(self.root/'.env').read_text())


class PairedSchemaTests(unittest.TestCase):
    inspect = WorkerUpdateTests.inspect
    run_command = WorkerUpdateTests.run_command
    def setUp(self):
        WorkerUpdateTests.setUp(self)
        self.transition = {'from':'0029_invitation_sections','to':'0030_novofon_settings',
                           'migration':'app/backend/migrations/versions/0030_novofon_settings.py',
                           'rollback_preserves_data':True}
        path = self.source / self.transition['migration']
        path.parent.mkdir(parents=True)
        path.write_text('reviewed additive migration')
        self.paired_policy = {'worker_runtime_contract':'novofon-settings-schema-v1',
            'schema_revision':self.transition['to'], 'paired_schema_update':self.transition,
            'protected':{self.transition['migration']:runtime.digest(path)}}

    def prepare_paired(self):
        path = self.source/'deploy/max-protection.json'
        path.write_text(json.dumps(self.paired_policy))
        with patch.object(runtime,'inspect',side_effect=self.inspect), patch.object(runtime,'run',side_effect=self.run_command):
            runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(path),self.transition['from'])

    def test_unreviewed_schema_does_not_get_worker_only_admission(self):
        (self.source/'deploy/max-protection.json').write_text(json.dumps(self.paired_policy))
        with patch.object(runtime,'inspect',side_effect=self.inspect), patch.object(runtime,'run',side_effect=self.run_command):
            with self.assertRaisesRegex(RuntimeError,'same database schema'):
                runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,
                    runtime.digest(self.source/'deploy/max-protection.json'))

    def test_exact_plan_is_required_and_admits_only_one_reviewed_revision(self):
        self.prepare_paired()
        with patch.object(runtime,'inspect',side_effect=self.inspect):
            self.assertTrue(runtime.allows_schema_update(self.root,self.source,SHA,self.transition['from'],self.transition['to']))
            self.assertFalse(runtime.allows_schema_update(self.root,self.source,SHA,self.transition['from'],'future_revision'))

    def test_modified_migration_cannot_be_prepared(self):
        path=self.source/'deploy/max-protection.json'
        path.write_text(json.dumps(self.paired_policy))
        (self.source/self.transition['migration']).write_text('unreviewed alteration')
        with patch.object(runtime,'inspect',side_effect=self.inspect), patch.object(runtime,'run',side_effect=self.run_command):
            with self.assertRaisesRegex(RuntimeError,'Exact reviewed'):
                runtime.prepare_update(self.root,self.source,SHA,OLD,self.max_image,runtime.digest(path),self.transition['from'])

    def test_intent_is_durable_before_migration_without_restarting_any_container(self):
        self.prepare_paired()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            runtime.begin_schema_update(self.root,self.source,SHA,NEW,self.backup)
        plan=json.loads((self.root/runtime.UPDATE).read_text())
        self.assertEqual(plan['phase'],'migrating')
        self.assertEqual(plan['new_api_id'],'sha256:new-image')
        self.assertFalse(any('up' in command or 'downgrade' in command for command in self.calls))

    def test_recovery_after_migration_commit_downgrades_before_readiness(self):
        self.prepare_paired()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            runtime.begin_schema_update(self.root,self.source,SHA,NEW,self.backup)
        order=[]
        promoter=Mock()
        promoter.wait_ready.side_effect=lambda image: order.append('api-ready') or True
        with patch.object(runtime,'inspect',side_effect=self.inspect), \
             patch.object(runtime,'restore_schema',side_effect=lambda *args: order.append('schema-restored')), \
             patch.object(runtime,'wait_worker',side_effect=lambda *args: order.append('worker-ready') or True):
            runtime.recover_update(self.root,promoter)
        self.assertEqual(order,['schema-restored','worker-ready','api-ready'])
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'failed')

    def test_recovery_never_downgrades_an_unrelated_schema(self):
        plan={'schema_transition':self.transition,'new_api':NEW,'new_api_id':'sha256:new-image'}
        with patch.object(runtime,'schema_revision',return_value='some_other_task_revision'),patch.object(runtime,'run') as command:
            with self.assertRaisesRegex(RuntimeError,'Unrelated schema'):
                runtime.restore_schema(self.root,plan)
        command.assert_not_called()

    def test_paired_promotion_requires_the_same_verified_migration_image(self):
        self.prepare_paired()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            runtime.begin_schema_update(self.root,self.source,SHA,NEW,self.backup)
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'schema_revision',return_value=self.transition['to']), \
             patch.object(runtime,'run',return_value='[{"Id":"sha256:reassigned"}]') as command:
            with self.assertRaisesRegex(RuntimeError,'image identity changed'):
                runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,Mock())
        self.assertFalse(any('up' in call.args or 'tag' in call.args for call in command.call_args_list))

    def test_paired_success_uses_callback_and_keeps_future_api_releases_independent(self):
        self.prepare_paired()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command):
            runtime.begin_schema_update(self.root,self.source,SHA,NEW,self.backup)
        promoter=Mock()
        with patch.object(runtime,'inspect',side_effect=self.inspect),patch.object(runtime,'run',side_effect=self.run_command), \
             patch.object(runtime,'schema_revision',return_value=self.transition['to']), \
             patch.object(runtime.subprocess,'run',return_value=Mock(returncode=1)),patch.object(runtime,'wait_worker',return_value=True):
            self.assertTrue(runtime.promote(self.root,self.source,SHA,OLD,NEW,self.backup,promoter))
        callback=promoter.deploy_api_with_rollback.call_args.kwargs['before_rollback']
        with patch.object(runtime,'restore_schema') as restore:
            callback()
        restore.assert_called_once()
        self.assertEqual(json.loads((self.root/runtime.UPDATE).read_text())['phase'],'complete')
        with patch.object(runtime,'run') as command:
            self.assertFalse(runtime.promote(self.root,self.source,'c'*40,NEW,'future',self.backup,Mock()))
        command.assert_not_called()

    def test_rollback_uses_the_verified_image_without_loading_a_database_dump(self):
        plan={'schema_transition':self.transition,'new_api':NEW,'new_api_id':'sha256:new-image'}
        with patch.object(runtime,'schema_revision',side_effect=[self.transition['to'],self.transition['from']]), \
             patch.object(runtime,'run',return_value='[{"Id":"sha256:new-image"}]') as command:
            runtime.restore_schema(self.root,plan)
        operations=[call.args for call in command.call_args_list]
        self.assertTrue(any('alembic' in op and 'downgrade' in op and self.transition['from'] in op for op in operations))
        self.assertFalse(any('pg_restore' in op for op in operations))


if __name__ == '__main__': unittest.main()
