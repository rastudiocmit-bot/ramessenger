"""Offline backup safety checks. No production Docker/SSH/database is used."""

from contextlib import ExitStack, contextmanager
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'full_backup_vps.py'
spec = importlib.util.spec_from_file_location('full_backup_vps', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@unittest.skipUnless(shutil.which('openssl') and shutil.which('tar'), 'OpenSSL/GNU tar required')
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / '20261030T120000Z-0123456789ab'
        self.directory.mkdir(mode=0o700)
        self.key = self.root / 'separate.key'
        self.key.write_bytes(b'offline-test-secret-with-sufficient-entropy\n')
        self.key.chmod(0o600)
        self.backup = m.FullBackup(self.directory, self.key)

    def manifest(self):
        value = {'format': 1, 'state': 'complete', 'parts': self.backup.parts}
        m.json_write(self.directory / 'manifest.json', value)
        raw = (self.directory / 'manifest.json').read_bytes()
        digest = hmac.new(m.authentication_key(self.key.read_bytes().strip()), raw, hashlib.sha256).hexdigest()
        (self.directory / 'manifest.hmac').write_text(digest)
        return value

    def test_encryption_roundtrip_and_authenticated_manifest(self):
        data = self.root / 'input'
        data.write_bytes((b'Customer data\0' * 10000) + bytes(range(256)))
        self.backup.part('test.enc', [['cat', str(data)]])
        result = subprocess.run(self.backup.decrypt('test.enc'), capture_output=True, check=True)
        self.assertEqual(result.stdout, data.read_bytes())
        self.assertNotIn(b'Customer data', (self.directory / 'test.enc').read_bytes())
        self.assertEqual((self.directory / 'test.enc').stat().st_mode & 0o777, 0o600)
        expected = self.manifest()
        self.assertEqual(m.verify_integrity(self.directory, self.key), expected)
        with self.assertRaises(m.BackupError):
            other = self.root / 'wrong.key'
            other.write_bytes(b'wrong')
            m.verify_integrity(self.directory, other)

    def test_ciphertext_tampering_is_rejected_before_decryption(self):
        self.backup.part('test.enc', [[sys.executable, '-c', 'print("protected")']])
        self.manifest()
        file = self.directory / 'test.enc'
        value = bytearray(file.read_bytes())
        value[-1] ^= 1
        file.write_bytes(value)
        with self.assertRaisesRegex(m.BackupError, 'authentication failed'):
            m.verify_integrity(self.directory, self.key)
        with self.assertRaises(m.BackupError):
            self.backup.decrypt('test.enc')

    def test_manifest_cannot_drop_parts_or_change_paths(self):
        self.backup.part('test.enc', [[sys.executable, '-c', 'print("protected")']])
        self.manifest()
        path = self.directory / 'manifest.json'
        value = json.loads(path.read_text())
        value['parts'] = []
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(m.BackupError, 'Manifest authentication'):
            m.verify_integrity(self.directory, self.key)

    def test_failed_producer_never_finalizes_a_part(self):
        with self.assertRaises(m.BackupError):
            self.backup.part('failed.enc', [[sys.executable, '-c',
                'import sys;print("partial");print("hidden-secret",file=sys.stderr);sys.exit(3)']])
        self.assertFalse((self.directory / 'failed.enc').exists())
        self.assertEqual(self.backup.parts, [])

    def test_tar_preserves_files_symlinks_modes_and_literal_names(self):
        files = self.root / 'source files'
        files.mkdir()
        file = files / 'strange\n--checkpoint-action=exec=false'
        file.write_bytes(b'bytes to preserve')
        file.chmod(0o640)
        (files / 'link').symlink_to(file.name)
        listing = self.backup.paths_file('paths.nul', [files])
        archive = self.backup.part('files.tar.gz.enc', [m.archive_command(listing)])
        plain = self.root / 'plain.tar.gz'
        with plain.open('wb') as output:
            subprocess.run(m.encryption_command(self.key, decrypt=True, source=archive),
                           stdout=output, check=True)
        with tarfile.open(plain, 'r:gz') as saved:
            members = {item.name: item for item in saved.getmembers()}
            name = str(file).lstrip('/')
            self.assertEqual(saved.extractfile(name).read(), file.read_bytes())
            self.assertEqual(members[name].mode, 0o640)
            self.assertTrue(members[str(files / 'link').lstrip('/')].issym())

    def test_sqlite_online_copy_captures_committed_wal(self):
        source = self.root / 'queue.sqlite'
        connection = sqlite3.connect(source)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE requests(id integer)')
        connection.execute('INSERT INTO requests VALUES (42)')
        connection.commit()
        target = self.root / 'copy.sqlite'
        with sqlite3.connect(target) as output:
            connection.backup(output)
            self.assertEqual(output.execute('SELECT id FROM requests').fetchone(), (42,))
            self.assertEqual(output.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_source_path_dedup_does_not_hide_external_symlink_target(self):
        parent = self.root / 'tree'
        parent.mkdir()
        child = parent / 'child'
        child.write_text('x')
        external = self.root / 'external'
        external.mkdir()
        link = self.root / 'symlink'
        link.symlink_to(external, target_is_directory=True)
        self.assertEqual(set(m.minimal_paths([child, parent, link, link / 'future'])), {parent, link})

    def test_each_failed_stage_recovers_api_and_only_original_timers(self):
        project = self.root / 'deployment'
        (project / 'support').mkdir(parents=True)
        phases = ('inventory', 'capture_metadata', 'source_bundle', 'host_files',
                  'image_files', 'database_files', 'verify_database')
        for failed in phases:
            with self.subTest(stage=failed), ExitStack() as stack:
                stack.enter_context(patch.object(m, 'ROOT', project))
                stack.enter_context(patch.object(self.backup, 'prepare'))
                stack.enter_context(patch.object(self.backup, 'active', side_effect=lambda unit: unit == m.TIMERS[0]))
                self.backup.runner = Mock()
                resume = stack.enter_context(patch.object(self.backup, 'resume_api'))
                for phase in phases:
                    stack.enter_context(patch.object(self.backup, phase,
                        side_effect=m.BackupError('planned failure') if phase == failed else None))
                finish = stack.enter_context(patch.object(self.backup, 'finish'))
                with self.assertRaisesRegex(m.BackupError, 'planned failure'):
                    self.backup.execute()
                resume.assert_called_once()
                calls = [call.args[0] for call in self.backup.runner.run.call_args_list]
                self.assertIn(['systemctl', 'start', m.TIMERS[0]], calls)
                self.assertNotIn(['systemctl', 'start', m.TIMERS[1]], calls)
                self.assertEqual(self.backup.timers, [])
                finish.assert_not_called()

    def test_remove_refuses_a_foreign_container(self):
        self.backup.runner = Mock()
        self.backup.runner.run.return_value = json.dumps([{'Id': 'foreign', 'Config': {'Labels': {}}}])
        with self.assertRaises(m.BackupError):
            self.backup.remove_owned_container('not-ours')
        self.assertEqual(self.backup.runner.run.call_count, 1)

    def test_recovery_refuses_to_start_replaced_production_container(self):
        m.json_write(self.directory / 'recovery.json', {
            'format': 1, 'run_id': self.directory.name, 'api_stop_intent': True,
            'api_id': 'original', 'api_image_id': 'sha256:original', 'timers': []})
        project = self.root / 'deployment'
        (project / 'support').mkdir(parents=True)
        runner = Mock()
        runner.run.return_value = json.dumps([{'Id': 'replacement', 'Image': 'sha256:new'}])
        with patch.object(m, 'ROOT', project), patch.object(m, 'Runner', return_value=runner):
            with self.assertRaisesRegex(m.BackupError, 'different API'):
                m.recover_run(self.root, self.directory.name)
        self.assertEqual(runner.run.call_count, 1)

    def test_api_resume_failure_keeps_recovery_intent(self):
        self.backup.api_stopped = True
        self.backup.runner = Mock()
        self.backup.runner.run.side_effect = m.BackupError('start failed')
        with self.assertRaises(m.BackupError):
            self.backup.clean_resources()
        state = json.loads((self.directory / 'recovery.json').read_text())
        self.assertTrue(state['api_stop_intent'])

    def test_background_unit_has_independent_recovery_and_no_deploy(self):
        args = Mock(output_root=self.root / 'backups', key_directory=self.root / 'keys')
        fake_log = self.root / 'log'
        original_path = m.Path
        def path(value):
            return fake_log if value == '/var/log/ramessenger-full-backup.log' else original_path(value)
        @contextmanager
        def credential():
            yield self.root / 'protected-token'
        with patch.object(m, 'Path', side_effect=path), patch.object(m.subprocess, 'run') as run, \
             patch.object(m, 'github_credential', credential):
            m.start_background(args)
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'systemd-run')
        self.assertTrue(any(item.startswith('--property=ExecStopPost=') for item in command))
        self.assertTrue(any(item.startswith('--property=LoadCredential=github-token:') for item in command))
        self.assertIn('--property=RuntimeMaxSec=3h', command)
        self.assertFalse(any(item in {'deploy', 'up', 'migrate', 'prune'} for item in command))

    def test_unverified_backup_cannot_be_marked_complete(self):
        with self.assertRaisesRegex(m.BackupError, 'incomplete'):
            self.backup.finish()
        self.assertFalse((self.directory / 'manifest.json').exists())

    def test_final_metadata_archive_is_stable_and_all_parts_reverify(self):
        fields = ('source_bundle_verified', 'deployment_archive_verified', 'image_archive_verified',
                  'logical_dumps_toc_verified', 'postgres_manifest_verified',
                  'physical_restore_verified', 'all_table_counts_verified')
        self.backup.metadata.update({field: True for field in fields})
        self.backup.metadata['logical_databases'] = []
        for name in ('source.bundle.enc', 'deployment-files.tar.gz.enc',
                     'container-images.tar.gz.enc', 'postgres-globals.sql.enc', 'postgres-base.tar.gz.enc'):
            self.backup.part(name, [[sys.executable, '-c', 'print("offline fixture")']])
        (self.backup.work / 'closed-log.txt').write_text('private diagnostic')
        self.backup.finish()
        self.assertFalse(self.backup.work.exists())
        saved = m.verify_integrity(self.directory, self.key)
        self.assertEqual(saved['state'], 'complete')
        self.assertTrue(saved['physical_restore_verified'])
        self.assertTrue((self.directory / 'RESTORE.txt').exists())
        self.assertNotIn(self.key.read_text().strip(), (self.directory / 'RESTORE.txt').read_text())
        self.assertEqual(json.loads((self.directory / 'status.json').read_text())['phase'], 'complete')

    def test_restore_container_has_no_network_ports_or_live_data_mount(self):
        self.backup.metadata = {}
        self.backup.db = {'Image': 'sha256:saved-postgres'}
        self.backup.databases = ['center_messenger']
        self.backup.db_bytes = 100
        self.backup.source_counts = {'center_messenger': {'messages': 42}}
        self.backup.schema_revision = 'schema-test'
        self.backup.recovery_settings = {'max_connections': '30', 'max_worker_processes': '8'}
        self.backup.runner = Mock()
        with patch.object(self.backup, 'decrypt', return_value=['fake-decrypt']), \
             patch.object(self.backup, 'counts', return_value=self.backup.source_counts), \
             patch.object(self.backup, 'sql', return_value='schema-test'), \
             patch.object(self.backup, 'wait_database'), \
             patch.object(self.backup, 'remove_owned_container'):
            self.backup.verify_database()
        commands = [call.args[0] for call in self.backup.runner.run.call_args_list]
        create = next(command for command in commands if command[:2] == ['docker', 'create'])
        self.assertEqual(create[create.index('--network') + 1], 'none')
        self.assertNotIn('-p', create)
        self.assertNotIn('--publish', create)
        mount = create[create.index('--mount') + 1]
        self.assertIn(str(self.directory / 'private-work' / 'restore-data'), mount)
        self.assertNotIn('/var/lib/docker/volumes/', mount)
        self.assertIn('max_connections=30', create)

    def test_disk_failure_happens_before_any_stream_or_api_stop(self):
        self.backup.runner = Mock()
        with patch.object(m.shutil, 'disk_usage', return_value=Mock(free=1)):
            with self.assertRaisesRegex(m.BackupError, 'Insufficient'):
                self.backup.part('no-space.enc', [['never-run']])
        self.backup.runner.pipeline.assert_not_called()
        self.assertFalse(self.backup.api_stopped)

    def test_own_transient_unit_cannot_pull_backup_key_into_sources(self):
        env = self.root / 'environment'
        env.write_text('SAFE_FILE=1')
        self.backup.runner = Mock()
        self.backup.runner.run.return_value = (
            '# /etc/systemd/system/ramessenger-local-build.service\n'
            '[Service]\nEnvironmentFile=' + str(env) + '\n'
            '# /run/systemd/transient/ramessenger-full-backup-0123456789ab.service\n'
            '[Service]\nExecStart=/usr/bin/python3 /opt/script.py --key-directory /root/ramessenger-backup-keys\n')
        paths = self.backup.extra_unit_paths()
        self.assertIn(env, paths)
        self.assertNotIn(Path('/root/ramessenger-backup-keys'), paths)
        self.assertFalse(any('transient/ramessenger-full-backup-' in str(p) for p in paths))

    def test_running_stream_preserves_disk_reserve(self):
        output = self.root / 'stream.incomplete'
        with patch.object(m.shutil, 'disk_usage', return_value=Mock(free=1)):
            with self.assertRaisesRegex(m.BackupError, 'preserve 2 GiB'):
                self.backup.runner.pipeline([[sys.executable, '-c', 'import time;time.sleep(5)']], output=output)

    def test_shell_credential_is_private_temporary_and_never_in_argv(self):
        secret = b'offline-fake-token'
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['gh'], 0, secret + b'\n', b'')) as run:
            with m.github_credential(self.root) as path:
                self.assertEqual(path.read_bytes(), secret + b'\n')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertNotIn(secret.decode(), repr(run.call_args))
            self.assertFalse(path.exists())

    def test_missing_shell_authentication_prevents_service_start(self):
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['gh'], 4, b'', b'authentication required')):
            with self.assertRaisesRegex(m.BackupError, 'launching shell'):
                with m.github_credential(self.root):
                    self.fail('Must not accept missing authentication')

    def test_background_credential_only_reaches_github_child_environment(self):
        credential_dir = self.root / 'credentials'
        credential_dir.mkdir()
        (credential_dir / 'github-token').write_text('offline-fake-token\n')
        (self.backup.work / 'source.git').mkdir()
        (self.backup.work / 'source.bundle').write_text('offline bundle')
        self.backup.sha = 'a' * 40
        self.backup.runner = Mock()
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(credential_dir)}), \
             patch.object(self.backup, 'part'):
            self.backup.source_bundle()
        clone = self.backup.runner.run.call_args_list[0]
        self.assertEqual(clone.kwargs['env']['GH_TOKEN'], 'offline-fake-token')
        self.assertNotIn('offline-fake-token', repr(clone.args))
        self.assertNotIn('offline-fake-token', repr(self.backup.metadata))


if __name__ == '__main__':
    unittest.main()
