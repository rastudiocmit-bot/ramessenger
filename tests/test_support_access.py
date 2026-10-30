"""Isolated access, persistence and deployment-race tests; no Docker/VPS calls."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import support_access as access
import support_ssh_entry as entry
import install_support_access as install
import local_build_vps as build

A = 'a' * 40
B = 'b' * 40
OLD = 'center-messenger:local-' + 'c' * 12


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'actors.json').write_text(json.dumps({'one': {'role': 'maintainer'},
                                                         'read': {'role': 'diagnostic'}}))
        self.scope = patch.object(access, 'SUPPORT', self.root)
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.queue = access.Queue(self.root / 'requests.sqlite')

    def test_long_audit_emits_heartbeat_without_resending_stdin_or_restarting(self):
        process = Mock(returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired('docker', 10),
                                           ('{"stage":"complete"}', None)]
        context = Mock()
        context.__enter__ = Mock(return_value=process)
        context.__exit__ = Mock(return_value=False)
        with patch.object(access.subprocess, 'Popen', return_value=context) as spawn, \
             patch.object(access, 'print') as output:
            access.run_audit('GET-only script')
        spawn.assert_called_once()
        self.assertEqual(process.communicate.call_args_list[0].kwargs['input'], 'GET-only script')
        self.assertIsNone(process.communicate.call_args_list[1].kwargs['input'])
        self.assertTrue(any('проверка выполняется' in str(call) for call in output.call_args_list))
        process.kill.assert_not_called()

    def test_audit_has_a_hard_process_deadline(self):
        process = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=process)
        context.__exit__ = Mock(return_value=False)
        with patch.object(access.subprocess, 'Popen', return_value=context), \
             patch.object(access.time, 'monotonic', side_effect=[0, 361]):
            with self.assertRaises(subprocess.TimeoutExpired):
                access.run_audit('GET-only script')
        process.kill.assert_called_once()
        process.communicate.assert_called_once_with()

    def test_duplicate_audit_does_not_start_a_second_browser(self):
        (self.root / 'diagnose_max_recipients.py').write_text('GET-only script')
        with access.worker_lock(self.root / 'audit.lock'), \
             patch.object(access, 'run_audit') as run, patch.object(access, 'print') as output:
            access.audit()
        run.assert_not_called()
        self.assertIn('audit_running', output.call_args.args[0])

    def test_only_exact_commands_and_full_sha_are_allowed(self):
        for command in ['status', 'logs', 'deploy-logs', 'audit', 'deploy ' + A]:
            self.assertTrue(access.parse_command(command))
        for command in ['bash', 'deploy main', 'deploy ' + A[:12], 'logs other-container',
                        'status; id', 'status\nid', 'deploy ' + A + ' && id', '$(id)',
                        'docker exec center-messenger-db-1 sh', 'deploy ' + A.upper()]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                access.parse_command(command)

    def test_read_role_and_revoked_actor_cannot_queue_a_deploy(self):
        with patch.object(access, 'execute') as command:
            for actor in ['read', 'removed']:
                with self.assertRaises(ValueError):
                    access.dispatch(actor, 'deploy ' + A)
            command.assert_not_called()
        self.assertFalse(self.queue.path.exists())

    def test_parallel_callers_have_durable_distinct_requests(self):
        self.queue.latest()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda n: self.queue.enqueue('one', A), range(20)))
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(len(access.Queue(self.queue.path).latest()), 20)

    def test_restart_recovers_claim_but_does_not_reexecute_installed_release(self):
        identifier = self.queue.enqueue('one', A)
        self.queue.claim()
        recreated = access.Queue(self.queue.path)
        recreated.recover()
        request = recreated.claim()
        self.assertEqual(request['id'], identifier)
        with patch.object(access, 'installed_sha', return_value=A), \
             patch.object(access, 'execute') as command:
            access.process_request(recreated, request)
        command.assert_not_called()
        self.assertEqual(recreated.latest()[0]['state'], 'deployed')

    def test_old_request_is_superseded_without_starting_a_deploy(self):
        self.queue.enqueue('one', A)
        with patch.object(access, 'installed_sha', return_value=None), \
             patch.object(access, 'current_main', return_value=B), \
             patch.object(access, 'execute') as command:
            access.process_request(self.queue, self.queue.claim())
        command.assert_not_called()
        self.assertEqual(self.queue.latest()[0]['state'], 'superseded')

    def test_new_request_waiting_on_old_systemd_job_gets_its_own_second_run(self):
        self.queue.enqueue('one', B)
        with patch.object(access, 'installed_sha', side_effect=[None, A, B]), \
             patch.object(access, 'current_main', return_value=B), \
             patch.object(access, 'execute') as command:
            access.process_request(self.queue, self.queue.claim())
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.args[0], ['systemctl', 'start', access.BUILD_UNIT])
        self.assertEqual(self.queue.latest()[0]['state'], 'deployed')

    def test_build_failure_is_not_reported_as_success(self):
        self.queue.enqueue('one', A)
        with patch.object(access, 'installed_sha', return_value=None), \
             patch.object(access, 'current_main', return_value=A), \
             patch.object(access, 'execute', side_effect=subprocess.CalledProcessError(1, 'hidden')):
            access.process_request(self.queue, self.queue.claim())
        self.assertEqual(self.queue.latest()[0]['state'], 'failed')

    def test_revoked_queued_actor_is_checked_again_before_any_server_call(self):
        self.queue.enqueue('removed', A)
        with patch.object(access, 'installed_sha') as read, patch.object(access, 'execute') as command:
            access.process_request(self.queue, self.queue.claim())
        read.assert_not_called()
        command.assert_not_called()
        self.assertEqual(self.queue.latest()[0]['state'], 'failed')

    def test_missed_systemd_wakeup_keeps_request_for_timer(self):
        with patch.object(access, 'current_main', return_value=A), \
             patch.object(access, 'execute', side_effect=subprocess.CalledProcessError(1, 'systemctl')):
            access.dispatch('one', 'deploy ' + A)
        self.assertEqual(self.queue.latest()[0]['state'], 'queued')

    def test_worker_lock_excludes_another_process_then_releases(self):
        code = ('import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                'from support_access import worker_lock\n'
                'try:\n with worker_lock(Path(sys.argv[2])): pass\n'
                'except BlockingIOError: sys.exit(23)\n')
        path = self.root / 'queue.lock'
        with access.worker_lock(path):
            result = subprocess.run([sys.executable, '-c', code, str(SCRIPTS), str(path)])
            self.assertEqual(result.returncode, 23)
        self.assertEqual(subprocess.run([sys.executable, '-c', code, str(SCRIPTS), str(path)]).returncode, 0)

    def test_ssh_metacharacters_are_passed_as_data_without_shell_evaluation(self):
        command = 'status; touch /tmp/should-never-exist'
        with patch.object(sys, 'argv', ['entry', 'one']), \
             patch.dict(entry.os.environ, {'SSH_ORIGINAL_COMMAND': command}), \
             patch.object(entry.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(SystemExit):
                entry.main()
        self.assertEqual(run.call_args.args[0][-1], command)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_public_key_options_cannot_inject_a_forced_command(self):
        self.assertEqual(install.key_line('ssh-ed25519 AAAA comment'), 'ssh-ed25519 AAAA')
        for key in ['command="sh" ssh-ed25519 AAAA', 'ssh-ed25519 AAAA\nssh-ed25519 BBBB',
                    'ssh-ed25519 AAAA"', 'ssh-rsa AAAA comment']:
            with self.assertRaises(ValueError):
                install.key_line(key)

    def test_new_actor_preserves_other_keys_and_rotation_replaces_only_its_own(self):
        one = install.authorized_lines([], 'one', 'ssh-ed25519 AAAA')
        two = install.authorized_lines(one, 'two', 'ssh-ed25519 BBBB')
        self.assertIn(one[0], two)
        rotated = install.authorized_lines(two, 'one', 'ssh-ed25519 CCCC')
        self.assertEqual(len(rotated), 2)
        self.assertIn(two[1], rotated)
        self.assertFalse(any(' AAAA ' in line for line in rotated))
        with self.assertRaises(ValueError):
            install.authorized_lines(rotated, 'three', 'ssh-ed25519 BBBB')

    def test_revocation_does_not_wait_for_a_running_queue_or_deploy(self):
        (self.root / 'local_build_vps.py').write_text('existing runner')
        with access.worker_lock(self.root / 'queue.lock'), \
             patch.object(install, 'ROOT', self.root), patch.object(install, 'SUPPORT', self.root), \
             patch.object(install.os, 'geteuid', return_value=0), \
             patch.object(install.os, 'chown'), \
             patch.object(sys, 'argv', ['installer', '--actor', 'one', '--revoke']), \
             patch.object(install, 'run') as command:
            install.main()
        command.assert_not_called()
        self.assertNotIn('one', json.loads((self.root / 'actors.json').read_text()))
        self.assertIn('read', json.loads((self.root / 'actors.json').read_text()))


class BuildFreshnessTests(unittest.TestCase):
    def test_backward_main_is_rejected_before_runner_self_update(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps({'enabled': True, 'last_deployed_sha': 'c' * 40}))
            with patch.object(build, 'STATE', state), patch.object(build.os, 'geteuid', return_value=0), \
                 patch.object(build, 'main_sha', return_value=A), \
                 patch.object(build, 'inspect', return_value={'Config': {'Image': OLD}}), \
                 patch.object(build, 'run', return_value='behind'), \
                 patch.object(build, 'self_update') as update:
                with self.assertRaises(RuntimeError):
                    build.main(False)
            update.assert_not_called()

    def test_reset_or_divergent_main_cannot_downgrade_the_live_release(self):
        state = {'last_deployed_sha': 'c' * 40}
        for relation in ['behind', 'diverged']:
            with patch.object(build, 'run', return_value=relation), self.assertRaises(RuntimeError):
                build.require_forward(state, OLD, A)
        with patch.object(build, 'run', return_value='ahead'):
            build.require_forward(state, OLD, A)

    def test_main_advanced_or_external_image_change_never_passes_fence(self):
        with patch.object(build, 'main_sha', return_value=B), patch.object(build, 'inspect') as image:
            self.assertFalse(build.promotion_fresh(A, OLD))
            image.assert_not_called()
        with patch.object(build, 'main_sha', return_value=A), \
             patch.object(build, 'inspect', return_value={'Config': {'Image': 'unexpected'}}):
            with self.assertRaises(RuntimeError):
                build.promotion_fresh(A, OLD)

    def test_main_advance_during_build_or_backup_does_not_migrate_or_promote(self):
        for heads, backups in [([A, B], 0), ([A, A, B], 1)]:
            with self.subTest(heads=heads), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'local-build-state.json'
                state.write_text('{"enabled": true}')
                def run(*argv, **kwargs):
                    if argv[0] == 'git':
                        return A
                    if argv[:3] == ('docker', 'image', 'inspect'):
                        return '[{"Os":"linux","Architecture":"amd64"}]'
                    if argv[0] == 'docker':
                        return 'revision'
                    return ''
                with patch.object(build, 'ROOT', root), patch.object(build, 'STATE', state), \
                     patch.object(build.os, 'geteuid', return_value=0), \
                     patch.object(build, 'main_sha', side_effect=heads[1:]), \
                     patch.object(build, 'inspect', return_value={'Config': {'Image': OLD}}), \
                     patch.object(build, 'guard', return_value=(OLD, 'revision')), \
                     patch.object(build, 'run', side_effect=run), \
                     patch.object(build, 'self_update'), \
                     patch.object(build, 'require_forward'), \
                     patch.object(build, 'alembic', return_value='revision (head)'), \
                     patch.object(build.subprocess, 'run'), \
                     patch.object(build, 'backup', return_value=root / 'backup.dump') as backup, \
                     patch.object(build, 'migrate') as migrate:
                    build.main(False, verified_source=(root, A))
                self.assertEqual(backup.call_count, backups)
                migrate.assert_not_called()
                self.assertNotIn('last_deployed_sha', json.loads(state.read_text()))


if __name__ == '__main__':
    unittest.main()
