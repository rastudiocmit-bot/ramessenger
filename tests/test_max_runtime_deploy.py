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


if __name__ == '__main__': unittest.main()
