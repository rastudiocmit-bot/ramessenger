"""Release fence regressions. Only temporary files and mocked Docker/systemd."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import max_release_guard as guard
import local_build_vps as build

SHA = 'a' * 40


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.trusted = root / 'trusted'
        self.source = root / 'candidate'
        self.trusted.mkdir()
        files = {'core/max.py': b'original MAX', 'migrations/001.py': b'original schema',
                 'scripts/guard.py': b'pinned guard', 'tests/behaviour.py': b'pinned tests'}
        for name, data in files.items():
            path = self.trusted / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        self.policy = {'version': 1, 'application_baseline': SHA,
                       'protected': {p: guard.digest(b) for p, b in files.items()},
                       'protected_prefixes': ['migrations'],
                       'protected_patterns': ['core/max*.py'],
                       'bundle': ['scripts/guard.py'], 'tests': ['tests/behaviour.py']}
        path = self.trusted / guard.POLICY
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(guard.encode(self.policy))
        shutil.copytree(self.trusted, self.source)

    def report(self):
        return guard.report(self.source, self.trusted, SHA)

    def test_ordinary_ui_edit_keeps_protected_max_unchanged(self):
        (self.source / 'design.css').write_text('new design')
        result = self.report()
        self.assertEqual(result['changes'], [])
        self.assertTrue(guard.approved(result, None))

    def test_changed_deleted_or_symlink_core_is_blocked(self):
        path = self.source / 'core/max.py'
        for mode in ['edit', 'delete', 'symlink']:
            with self.subTest(mode=mode):
                path.unlink(missing_ok=True)
                if mode == 'edit':
                    path.write_text('changed MAX')
                if mode == 'symlink':
                    path.symlink_to(self.trusted / 'core/max.py')
                self.assertFalse(guard.approved(self.report(), None))

    def test_policy_and_verifier_cannot_be_replaced_in_the_same_commit(self):
        (self.source / 'scripts/guard.py').write_text('always pass')
        self.policy['protected']['scripts/guard.py'] = guard.digest(b'always pass')
        (self.source / guard.POLICY).write_bytes(guard.encode(self.policy))
        result = self.report()
        self.assertEqual({x['path'] for x in result['changes']}, {'scripts/guard.py', guard.POLICY})
        self.assertFalse(guard.approved(result, None))

    def test_removing_or_rewriting_compatibility_tests_requires_approval(self):
        (self.source / 'tests/behaviour.py').unlink()
        self.assertFalse(guard.approved(self.report(), None))

    def test_new_migration_or_new_native_module_is_reviewed(self):
        for name in ['migrations/002.py', 'core/max_extra.py']:
            path = self.source / name
            path.write_text('new')
            self.assertIn(name, {x['path'] for x in self.report()['changes']})

    def test_nested_symlink_and_escaping_path_rejected(self):
        shutil.rmtree(self.source / 'core')
        (self.source / 'core').symlink_to(self.trusted / 'core', target_is_directory=True)
        self.assertFalse(guard.approved(self.report(), None))
        for name in ['/etc/passwd', '../outside', 'core/../../outside']:
            with self.subTest(name=name), self.assertRaises(guard.GuardError):
                guard.regular(self.source, name)

    def test_approval_is_for_exact_commit_diff_policy_and_expiry(self):
        (self.source / 'core/max.py').write_text('reviewed MAX fix')
        result = self.report()
        now = datetime.now(timezone.utc)
        approval = dict(result, reason='Reviewed recipient fix', expires_at=(now + timedelta(hours=1)).isoformat())
        self.assertTrue(guard.approved(result, approval, now))
        for key in ['sha', 'diff_sha256', 'policy_sha256']:
            other = dict(approval, **{key: 'wrong'})
            self.assertFalse(guard.approved(result, other, now))
        self.assertFalse(guard.approved(result, approval, now + timedelta(hours=2)))
        (self.source / 'core/max.py').write_text('different fix')
        self.assertFalse(guard.approved(self.report(), approval, now))

    def test_malformed_approval_cannot_disable_guard(self):
        (self.source / 'core/max.py').write_text('changed')
        for approval in [{}, dict(self.report(), expires_at='wrong'), dict(self.report(), expires_at='2099-01-01', reason='')]:
            self.assertFalse(guard.approved(self.report(), approval))

    def test_compatibility_uses_installed_tests_and_isolated_image(self):
        with patch.object(guard.subprocess, 'run') as run, patch.object(guard, 'database_compatibility') as db, \
             patch.object(guard, 'remove_test_container') as remove:
            guard.compatibility('center-messenger:local-a', self.trusted)
        db.assert_called_once_with('center-messenger:local-a', self.trusted)
        args = run.call_args.args[0]
        self.assertIn('--read-only', args)
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn(f'type=bind,src={self.trusted}/tests,dst=/opt/max-tests,readonly', args)
        self.assertNotIn('--env-file', args)
        self.assertNotIn('--publish', args)
        self.assertFalse(any('/run/secrets' in x or 'center-messenger_database' in x for x in args))
        remove.assert_called_once()

    def test_compatibility_failure_is_not_ignored(self):
        with patch.object(guard.subprocess, 'run', side_effect=RuntimeError('test failed')), \
             patch.object(guard, 'remove_test_container') as remove:
            with self.assertRaises(RuntimeError):
                guard.compatibility('image', self.trusted)
        remove.assert_called_once()

    def test_database_compatibility_never_uses_live_database_network_or_volume(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            return '[{"Image":"sha256:test-postgres"}]' if args[:2] == ('docker', 'inspect') else ''
        with patch.object(guard, 'command', side_effect=command), patch.object(guard.subprocess, 'run') as run, \
             patch.object(guard, 'remove_test_container') as remove:
            run.return_value.returncode = 0
            guard.database_compatibility('candidate-image', self.trusted)
        create = next(c for c in calls if c[:3] == ('docker', 'network', 'create'))
        self.assertIn('--internal', create)
        start = next(c for c in calls if c[:3] == ('docker', 'run', '-d'))
        self.assertIn('--tmpfs', start)
        self.assertNotIn('--publish', start)
        self.assertFalse(any('center-messenger_database' in str(c) for c in calls))
        self.assertEqual(remove.call_count, 2)
        self.assertTrue(any(c[:3] == ('docker', 'network', 'rm') for c in calls))
        test = run.call_args.args[0]
        self.assertIn('--database', test)
        self.assertFalse(any('secret' in x or 'center-messenger-db-1' in x for x in test))

    def test_database_test_failure_still_removes_only_its_own_resources(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            return '[{"Image":"sha256:test-postgres"}]' if args[:2] == ('docker', 'inspect') else ''
        with patch.object(guard, 'command', side_effect=command), patch.object(guard.subprocess, 'run') as run, \
             patch.object(guard, 'remove_test_container') as remove:
            run.side_effect = [Mock(returncode=0), RuntimeError('synthetic test failure')]
            with self.assertRaisesRegex(RuntimeError, 'synthetic test failure'):
                guard.database_compatibility('candidate', self.trusted)
        self.assertEqual(remove.call_count, 2)
        self.assertTrue(any(c[:3] == ('docker', 'network', 'rm') for c in calls))

    def test_changed_protected_file_blocks_before_loading_runner(self):
        from contextlib import nullcontext
        (self.source / 'core/max.py').write_text('unsafe')
        with patch.object(guard, 'trusted_bundle'), patch.object(guard, 'project_lock', return_value=nullcontext()), \
             patch.object(guard, 'ROOT', self.source), patch.object(guard, 'TRUST', self.trusted), \
             patch.object(guard, 'main_sha', return_value=SHA), \
             patch.object(guard, 'checkout', return_value=nullcontext(self.source)), \
             patch.object(guard.importlib.util, 'spec_from_file_location') as load:
            (self.source / 'local-build-state.json').write_text('{"enabled":true}')
            with self.assertRaises(guard.GuardError):
                guard.run(self.trusted)
        load.assert_not_called()

    def test_trust_checksum_detects_local_bundle_damage(self):
        integrity = {p: guard.digest(guard.regular(self.trusted, p))
                     for p in [guard.POLICY, *self.policy['bundle'], *self.policy['tests']]}
        (self.trusted / 'integrity.json').write_bytes(guard.encode(integrity))
        with patch.object(guard, 'TRUST', self.trusted), patch.object(guard, 'root_owned'):
            guard.trusted_bundle(self.trusted)
            (self.trusted / 'tests/behaviour.py').write_text('damaged')
            with self.assertRaises(guard.GuardError):
                guard.trusted_bundle(self.trusted)

    def test_install_checks_policy_and_live_build_and_does_not_restart_api(self):
        external = self.source.parent / 'external'
        dropin = self.source.parent / 'units/50-max-guard.conf'
        policy_hash = guard.digest(guard.regular(self.source, guard.POLICY))
        live = [{'Config': {'Image': 'center-messenger:local-' + SHA[:12], 'Env': ['CM_BUILD_SHA=' + SHA]},
                 'State': {'Status': 'running'}}]
        with patch.object(guard, 'TRUST', external), patch.object(guard, 'DROP_IN', dropin), \
             patch.object(guard, 'project_lock'), patch.object(guard, 'root_owned'), \
             patch.object(guard, 'command', side_effect=[json.dumps(live), '']) as cmd:
            guard.install(self.source, policy_hash, SHA)
        self.assertTrue((external / 'active').is_symlink())
        self.assertIn('ExecStart=\nExecStart=/usr/bin/python3 /etc/ramessenger-max-guard/', dropin.read_text())
        self.assertEqual(cmd.call_args_list[-1].args, ('systemctl', 'daemon-reload'))
        self.assertFalse(any('restart' in c.args or 'stop' in c.args for c in cmd.call_args_list))

    def test_install_rejects_unreviewed_policy_before_any_system_command(self):
        with patch.object(guard, 'command') as command:
            with self.assertRaises(guard.GuardError):
                guard.install(self.source, 'wrong-checksum', SHA)
        command.assert_not_called()


class RunnerTests(unittest.TestCase):
    def test_support_suite_is_isolated_from_an_installed_host_guard(self):
        import test_support_access
        # Reproduce the deployment host, where /etc/.../active exists. The
        # nested suite must override this host path and must never exec it.
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(build, 'MAX_GUARD', Path(directory)), \
                 patch.object(build.os, 'execv', side_effect=AssertionError('Host guard executed')) as execute:
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_support_access.BuildFreshnessTests)
                result = unittest.TestResult()
                suite.run(result)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            self.assertEqual(result.testsRun, 4)
            execute.assert_not_called()

    def test_existing_guard_is_entered_before_self_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            state.write_text('{"enabled":true}')
            with patch.object(build, 'MAX_GUARD', root), patch.object(build, 'STATE', state), \
                 patch.object(build.os, 'geteuid', return_value=0), patch.object(build, 'main_sha', return_value=SHA), \
                 patch.object(build, 'inspect', return_value={'Config': {'Image': 'center-messenger:local-' + 'b'*12}}), \
                 patch.object(build, 'enter_max_guard') as enter, patch.object(build, 'self_update') as update, \
                 patch.object(build, 'bootstrap_max_guard') as bootstrap:
                build.main(False)
            enter.assert_called_once()
            update.assert_not_called()
            bootstrap.assert_not_called()

    def test_first_release_bootstraps_before_any_build_or_database_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            state.write_text('{"enabled":true}')
            with patch.object(build, 'MAX_GUARD', root / 'not-installed'), patch.object(build, 'STATE', state), \
                 patch.object(build.os, 'geteuid', return_value=0), patch.object(build, 'main_sha', return_value=SHA), \
                 patch.object(build, 'inspect', return_value={'Config': {'Image': 'center-messenger:local-' + 'b'*12}}), \
                 patch.object(build, 'require_forward'), patch.object(build, 'self_update') as update, \
                 patch.object(build, 'bootstrap_max_guard') as bootstrap, patch.object(build, 'guard') as db_guard:
                build.main(False)
            update.assert_called_once_with(SHA)
            bootstrap.assert_called_once_with(SHA)
            db_guard.assert_not_called()

    def test_bootstrap_reuses_lock_and_cleans_checkout_before_entering_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = Mock()
            def run(*args, **kwargs):
                if args[0] == 'gh':
                    source = Path(args[4])
                    (source / 'deploy').mkdir(parents=True)
                    (source / 'deploy/max-protection.json').write_text(json.dumps({'application_baseline': SHA}))
                return SHA
            def enter():
                self.assertEqual(list(root.glob('max-guard-bootstrap-*')), [])
            with patch.object(build, 'ROOT', root), patch.object(build, 'MAX_GUARD', root / 'absent'), \
                 patch.object(build, 'run', side_effect=run), patch.object(build, 'enter_max_guard', side_effect=enter), \
                 patch.object(build.importlib.util, 'spec_from_file_location'), \
                 patch.object(build.importlib.util, 'module_from_spec', return_value=module):
                build.bootstrap_max_guard(SHA)
            self.assertTrue(module.install.call_args.kwargs['lock_held'])
            self.assertEqual(module.install.call_args.args[2], SHA)

    def test_bootstrap_does_not_install_from_a_different_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(build, 'ROOT', root), patch.object(build, 'MAX_GUARD', root / 'absent'), \
                 patch.object(build, 'run', return_value='b'*40), patch.object(build, 'enter_max_guard') as enter, \
                 patch.object(build.importlib.util, 'spec_from_file_location') as load:
                with self.assertRaisesRegex(RuntimeError, 'Main advanced'):
                    build.bootstrap_max_guard(SHA)
            load.assert_not_called()
            enter.assert_not_called()

    def test_verified_source_never_self_updates_or_reloads_main_as_target(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{"enabled":true}')
            with patch.object(build, 'STATE', state), patch.object(build.os, 'geteuid', return_value=0), \
                 patch.object(build, 'inspect', return_value={'Config': {'Image': 'center-messenger:local-' + SHA[:12]}}), \
                 patch.object(build, 'main_sha') as main_sha, patch.object(build, 'self_update') as update:
                build.main(False, verified_source=(Path(directory), SHA))
            main_sha.assert_not_called()
            update.assert_not_called()

    def test_compatibility_failure_happens_before_any_database_backup_or_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            state.write_text('{"enabled":true}')
            old = 'center-messenger:local-' + 'b' * 12
            def run(*args, **kwargs):
                if args[0] == 'git':
                    return SHA
                if args[:3] == ('docker', 'image', 'inspect'):
                    return '[{"Os":"linux","Architecture":"amd64"}]'
                if args[:2] == ('docker', 'run'):
                    return 'revision'
                return ''
            with patch.object(build, 'STATE', state), patch.object(build.os, 'geteuid', return_value=0), \
                 patch.object(build, 'inspect', return_value={'Config': {'Image': old}}), \
                 patch.object(build, 'require_forward'), patch.object(build, 'run', side_effect=run), \
                 patch.object(build, 'guard', return_value=(old, 'revision')), \
                 patch.object(build, 'alembic', return_value='revision (head)'), \
                 patch.object(build.subprocess, 'run'), patch.object(build, 'backup') as backup, \
                 patch.object(build, 'migrate') as migrate:
                compatibility = Mock(side_effect=RuntimeError('regression failure'))
                with self.assertRaisesRegex(RuntimeError, 'regression failure'):
                    build.main(False, verified_source=(root, SHA), compatibility=compatibility)
            compatibility.assert_called_once_with('center-messenger:local-' + SHA[:12])
            backup.assert_not_called()
            migrate.assert_not_called()


class PolicyTests(unittest.TestCase):
    def test_repository_policy_matches_all_frozen_files(self):
        root = SCRIPTS.parent
        self.assertEqual(guard.changes(root, root), [])

    def test_database_gate_targets_real_existing_regressions(self):
        # Check the selected names without importing application dependencies.
        import ast
        root = SCRIPTS.parent
        source = (root / 'tests/run_max_compatibility.py').read_text()
        tree = ast.parse(source)
        case = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'DATABASE_CASES' for t in n.targets))
        scope = {}
        exec(compile(ast.Module(body=[case], type_ignores=[]), '<cases>', 'exec'), scope)
        for name in scope['DATABASE_CASES']:
            module, cls, *method = name.split('.')
            parsed = ast.parse((root / 'tests' / (module + '.py')).read_text())
            found = next(n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == cls)
            if method:
                self.assertTrue(any(isinstance(n, ast.FunctionDef) and n.name == method[0] for n in found.body), name)


if __name__ == '__main__':
    unittest.main()
