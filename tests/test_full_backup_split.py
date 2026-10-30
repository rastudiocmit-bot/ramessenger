"""No live CRM/production writes: file, snapshot, cleanup and backup regressions."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('full_backup_split', Path(__file__).resolve().parents[1] / 'scripts/full_backup_split.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / '20261030T180408Z-aabbccddeeff'
        self.directory.mkdir()
        self.service = backup.FullBackup(self.directory)

    def container(self, name, *, image=None):
        role = 'api' if name == backup.API else ('max' if name == backup.MAX else None)
        env = ['CM_RUNTIME_ROLE=' + role] if role else []
        env += ['CM_BUILD_SHA=' + 'a' * 40, 'PRIVATE_TOKEN=never-print-me']
        return {'Id': name + '-id', 'Name': '/' + name, 'Image': image or 'sha256:app',
            'Config': {'Image': 'image-' + name, 'Env': env, 'Labels': {}},
            'HostConfig': {'ReadonlyRootfs': True}, 'Mounts': [],
            'State': {'Status': 'running', 'Running': True, 'Health': {'Status': 'healthy'}, 'StartedAt': '2026-10-30T18:00:00Z'},
            'RestartCount': 0}

    def audit_fixture(self):
        containers = {name: self.container(name) for name in (backup.API, backup.MAX, backup.DB)}
        (self.root / '.env').write_text('CM_MAX_IMAGE=' + containers[backup.MAX]['Config']['Image'] + '\n')
        (self.root / 'max-runtime-transition.json').write_text(json.dumps({'phase':'complete',
            'worker_id':containers[backup.MAX]['Image'], 'worker_image':containers[backup.MAX]['Config']['Image']}))
        trust = self.root / 'trust'
        policy = json.dumps({'schema_revision':'0029_invitation_sections'}).encode()
        generation = trust / hashlib.sha256(policy).hexdigest()
        (generation / 'deploy').mkdir(parents=True)
        (generation / 'deploy/max-protection.json').write_bytes(policy)
        (trust / 'active').symlink_to(generation.name)
        result = {'schema':'0029_invitation_sections', 'connections':[{'workspace_id':'workspace', 'state':'active'}],
                  'accepted_without_saved_message':0}
        return containers, trust, result

    def test_audit_is_read_only_and_does_not_print_environment_secrets(self):
        containers, trust, result = self.audit_fixture()
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ready"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        runner = Mock()
        with patch.object(backup, 'inspect', side_effect=lambda r,n: containers[n]), \
             patch.object(backup, 'read_sql', return_value=json.dumps(result)), \
             patch.object(backup.urllib.request, 'urlopen', return_value=response):
            report = backup.audit(runner, self.root, trust)
        self.assertEqual(report['issues'], [])
        self.assertTrue(report['read_only'])
        self.assertEqual((report['crm_requests'], report['message_posts']), (0,0))
        self.assertNotIn('never-print-me', json.dumps(report))
        runner.run.assert_not_called()

    def test_audit_detects_wrong_role_pin_schema_and_missing_message(self):
        containers, trust, result = self.audit_fixture()
        containers[backup.API]['Config']['Env'] = ['CM_RUNTIME_ROLE=combined']
        (self.root / '.env').write_text('CM_MAX_IMAGE=wrong\n')
        result.update(schema='wrong', accepted_without_saved_message=1)
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ready"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with patch.object(backup, 'inspect', side_effect=lambda r,n: containers[n]), \
             patch.object(backup, 'read_sql', return_value=json.dumps(result)), \
             patch.object(backup.urllib.request, 'urlopen', return_value=response):
            report = backup.audit(Mock(), self.root, trust)
        self.assertEqual(len(report['issues']), 4)

    def test_healthy_containers_with_stale_sync_are_reported_as_a_problem(self):
        containers,trust,result=self.audit_fixture()
        result['connections'][0]['sync_lag_seconds']=3600
        response=Mock(status=200)
        response.read.return_value=b'{"status":"ready"}'
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=None)
        with patch.object(backup,'inspect',side_effect=lambda r,n:containers[n]), \
             patch.object(backup,'read_sql',return_value=json.dumps(result)), \
             patch.object(backup.urllib.request,'urlopen',return_value=response):
            report=backup.audit(Mock(),self.root,trust)
        self.assertEqual(len(report['issues']),1)
        self.assertIn('no successful read',report['issues'][0])

    def test_recent_success_does_not_hide_current_native_internal_error(self):
        containers,trust,result=self.audit_fixture()
        result['connections'][0].update(sync_lag_seconds=90,error_code='internal',activity_phase='error')
        response=Mock(status=200)
        response.read.return_value=b'{"status":"ready"}'
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=None)
        with patch.object(backup,'inspect',side_effect=lambda r,n:containers[n]), \
             patch.object(backup,'read_sql',return_value=json.dumps(result)), \
             patch.object(backup.urllib.request,'urlopen',return_value=response):
            report=backup.audit(Mock(),self.root,trust)
        self.assertEqual(len(report['issues']),1)
        self.assertIn('reports internal',report['issues'][0])

    def test_api_restart_returns_runtime_evidence_instead_of_crashing_audit(self):
        containers,trust,result=self.audit_fixture()
        with patch.object(backup,'inspect',side_effect=lambda r,n:containers[n]), \
             patch.object(backup,'read_sql',return_value=json.dumps(result)), \
             patch.object(backup.urllib.request,'urlopen',side_effect=ConnectionResetError):
            report=backup.audit(Mock(),self.root,trust)
        self.assertFalse(report['api_ready'])
        self.assertEqual(report['database'],result)
        self.assertEqual(report['issues'],['Public API readiness failed'])

    def test_sql_enforces_read_only_transaction_and_timeout(self):
        runner = Mock()
        backup.read_sql(runner, 'db-id', 'SELECT 1')
        args, kwargs = runner.run.call_args
        self.assertEqual(args[0][:4], ['docker','exec','-i','db-id'])
        self.assertIn('BEGIN READ ONLY', kwargs['input_text'])
        self.assertIn('statement_timeout', kwargs['input_text'])
        self.assertTrue(kwargs['input_text'].endswith('ROLLBACK;\n'))

    def test_active_session_without_any_successful_read_is_not_healthy_history(self):
        containers, trust, result = self.audit_fixture()
        result['connections'][0]['last_sync_at'] = None
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ready"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with patch.object(backup, 'inspect', side_effect=lambda r,n:containers[n]), \
             patch.object(backup, 'read_sql', return_value=json.dumps(result)), \
             patch.object(backup.urllib.request, 'urlopen', return_value=response):
            report = backup.audit(Mock(), self.root, trust)
        self.assertIn('no recorded successful read', report['issues'][0])

    def test_pending_send_queue_over_five_minutes_is_reported(self):
        containers, trust, result = self.audit_fixture()
        result['waiting_over_5_minutes'] = 1
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ready"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with patch.object(backup, 'inspect', side_effect=lambda r,n:containers[n]), \
             patch.object(backup, 'read_sql', return_value=json.dumps(result)), \
             patch.object(backup.urllib.request, 'urlopen', return_value=response):
            report = backup.audit(Mock(), self.root, trust)
        self.assertEqual(report['issues'], ['Native send commands have waited for over 5 minutes'])

    def test_readiness_with_invalid_json_shape_is_a_readiness_issue(self):
        containers, trust, result = self.audit_fixture()
        response = Mock(status=200)
        response.read.return_value = b'null'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with patch.object(backup, 'inspect', side_effect=lambda r,n:containers[n]), \
             patch.object(backup, 'read_sql', return_value=json.dumps(result)), \
             patch.object(backup.urllib.request, 'urlopen', return_value=response):
            report = backup.audit(Mock(), self.root, trust)
        self.assertFalse(report['api_ready'])
        self.assertEqual(report['issues'], ['Public API readiness failed'])

    def test_dump_and_counts_use_same_snapshot_without_stopping_any_container(self):
        containers = {name:self.container(name) for name in (backup.API, backup.MAX, backup.DB)}
        self.service.containers, self.service.db_id = containers, 'db-id'
        self.service.databases = ['center_messenger','postgres']
        runner = self.service.runner = Mock()
        calls = []
        def part(name, commands):
            calls.append((name,commands))
            path = self.directory / name
            path.write_bytes(b'custom-dump')
            return path
        session = Mock(id='00000003-000004E0-1')
        session.query.side_effect = ['snapshot-time','0029_invitation_sections','snapshot-time-2']
        context = Mock()
        context.__enter__ = Mock(return_value=session)
        context.__exit__ = Mock(return_value=None)
        with patch.object(backup,'inspect',side_effect=lambda r,n:containers[n]), \
             patch.object(backup,'Snapshot',return_value=context), \
             patch.object(backup,'table_counts',return_value={'table':12}), \
             patch.object(self.service,'add_part',side_effect=part):
            self.service.database_files()
        self.assertEqual(len(calls),2)
        for name,commands in calls:
            self.assertIn('--snapshot=00000003-000004E0-1',commands[0])
            self.assertIn('--create',commands[0])
        reads = [c for c in runner.run.call_args_list if 'pg_restore' in c.args[0]]
        self.assertEqual(len(reads),4)
        self.assertTrue(all('input_file' in c.kwargs for c in reads))
        self.assertTrue(all(c.args[0][0] != 'cat' for c in runner.run.call_args_list))
        self.assertFalse(any(set(c.args[0]) & {'stop','restart','start','kill'} for c in runner.run.call_args_list))
        self.assertEqual(self.service.metadata['databases'][0]['table_counts'],{'table':12})

    def test_isolated_restore_has_no_production_network_mount_or_ports(self):
        self.service.db = self.container(backup.DB,image='sha256:postgres')
        item={'database':'center_messenger','part':'database-000.dump','table_counts':{'t':7},
              'schema_revision':'0029_invitation_sections','full_dump_read_verified':True}
        self.service.metadata['databases'] = [item]
        def run(args,**kwargs):
            return 'accepting connections' if 'pg_isready' in args else ''
        self.service.runner = Mock()
        self.service.runner.run.side_effect = run
        with patch.object(backup,'table_counts',return_value={'t':7}), \
             patch.object(backup,'read_sql',return_value='0029_invitation_sections'):
            self.service.verify_restore()
        calls=self.service.runner.run.call_args_list
        create=next(c.args[0] for c in calls if c.args[0][:3]==['docker','run','-d'])
        self.assertEqual(create[create.index('--network')+1],'none')
        self.assertNotIn('--publish',create)
        self.assertNotIn('center-messenger_database',' '.join(create))
        probe=next(c.args[0] for c in calls if 'pg_isready' in c.args[0])
        self.assertEqual(probe[probe.index('-h')+1],'127.0.0.1')
        restore=next(c for c in calls if 'pg_restore' in c.args[0])
        self.assertEqual(restore.args[0][3],self.service.clone)
        self.assertIn('--exit-on-error',restore.args[0])
        self.assertIn('--create',restore.args[0])
        self.assertTrue(restore.kwargs['guard_space'])
        self.assertTrue(item['restore_verified'])

    def test_restore_count_mismatch_is_failure(self):
        self.service.db=self.container(backup.DB)
        self.service.metadata['databases']=[{'database':'center_messenger','part':'database-000.dump',
            'table_counts':{'t':7},'schema_revision':'0029'}]
        self.service.runner=Mock()
        self.service.runner.run.return_value='accepting connections'
        with patch.object(backup,'table_counts',return_value={'t':6}), self.assertRaisesRegex(backup.BackupError,'counts disagree'):
            self.service.verify_restore()
        self.assertFalse(self.service.metadata['databases'][0].get('restore_verified',False))

    def test_cleanup_refuses_foreign_container_and_preserves_its_volume(self):
        self.service.clone_created=self.service.volume_created=True
        self.service.runner=Mock()
        self.service.runner.run.return_value=json.dumps([{'Config':{'Labels':{'com.ramessenger.backup':'someone-else'}}}])
        with self.assertRaisesRegex(backup.BackupError,'not owned'):
            self.service.cleanup()
        self.assertEqual(self.service.runner.run.call_count,1)
        self.assertTrue(self.service.volume_created)

    def test_cleanup_removes_only_own_container_then_own_volume(self):
        self.service.clone_created=self.service.volume_created=True
        label={'com.ramessenger.backup':self.directory.name}
        def run(args,**kwargs):
            if args[2]=='inspect':
                return json.dumps([{'Config':{'Labels':label}}] if args[1]=='container' else [{'Labels':label}])
            return ''
        self.service.runner=Mock()
        self.service.runner.run.side_effect=run
        self.service.cleanup()
        removals=[c.args[0] for c in self.service.runner.run.call_args_list if 'rm' in c.args[0]]
        self.assertEqual(removals,[['docker','container','rm','-f',self.service.clone],['docker','volume','rm',self.service.volume]])

    def test_cleanup_runs_after_snapshot_or_restore_failure(self):
        with patch.object(backup,'locks'), patch.object(self.service,'inventory'), \
             patch.object(self.service,'files'), patch.object(self.service,'database_files',side_effect=RuntimeError('failure')), \
             patch.object(self.service,'cleanup') as cleanup, patch.object(self.service,'finish') as finish:
            with self.assertRaises(RuntimeError):
                self.service.execute()
        cleanup.assert_called_once()
        finish.assert_not_called()
        self.assertEqual(json.loads((self.directory/'status.json').read_text())['state'],'failed')

    def test_incomplete_restore_cannot_publish_complete_manifest(self):
        self.service.metadata['databases']=[{'database':'center_messenger','restore_verified':False}]
        with self.assertRaises(backup.BackupError):
            self.service.finish()
        self.assertFalse((self.directory/'manifest.json').exists())

    def test_snapshot_owner_stays_open_until_dump_context_exits(self):
        process=Mock()
        process.poll.return_value=None
        process.returncode=0
        process.stdin=io.StringIO()
        process.stdout=io.StringIO('00000003-000004E0-1\n')
        selector=Mock()
        selector.__enter__=Mock(return_value=selector)
        selector.__exit__=Mock(return_value=None)
        selector.select.return_value=[(Mock(),1)]
        with patch.object(backup.subprocess,'Popen',return_value=process), \
             patch.object(backup.selectors,'DefaultSelector',return_value=selector):
            with backup.Snapshot(backup.Runner(),'db-id','center_messenger') as snapshot:
                self.assertEqual(snapshot.id,'00000003-000004E0-1')
                process.wait.assert_not_called()
                self.assertIn('REPEATABLE READ READ ONLY',process.stdin.getvalue())
        process.wait.assert_called_once()
        self.assertTrue(process.stdin.closed)

    def test_table_counts_quote_unusual_identifiers(self):
        query=Mock(side_effect=[json.dumps([['strange-schema','double"quote']]),'{"0":12}'])
        counts=backup.table_counts(query)
        self.assertEqual(list(counts.values()),[12])
        self.assertIn('"strange-schema"."double""quote"',query.call_args.args[0])

    def test_direct_file_validation_does_not_produce_cat_sigpipe(self):
        # A consumer reading only a prefix succeeds with regular-file stdin;
        # there is no extra cat process to fail after the consumer exits.
        source=self.directory/'large.dump'
        source.write_bytes(b'x'*1024*1024)
        self.service.runner.run([sys.executable,'-c','import sys; sys.stdin.buffer.read(1)'],input_file=source,discard=True)

    def test_archive_stream_and_tar_full_read_are_real_file_operations(self):
        source=self.directory/'data.txt'
        source.write_text('backup payload')
        path=self.service.add_part('small.tar.gz',[[sys.executable,'-c',
            'import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode="w|gz"); t.add(sys.argv[1],arcname="data.txt"); t.close()',str(source)]])
        with tarfile.open(path) as archive:
            self.assertEqual(archive.extractfile('data.txt').read(),b'backup payload')

    def test_stream_failure_keeps_only_incomplete_part(self):
        with self.assertRaises(backup.BackupError):
            self.service.add_part('failed.tar.gz',[[sys.executable,'-c','import sys; sys.exit(2)']])
        self.assertFalse((self.directory/'failed.tar.gz').exists())
        self.assertEqual(self.service.metadata['parts'],[])

    def complete_fixture(self):
        required=['deployment-files.tar.gz','container-images.tar.gz','postgres-globals.sql','database-000.dump',
                  'recovery-metadata.json','runtime-audit.json','full_backup_split.py','RESTORE.txt']
        parts=[]
        for name in required:
            p=self.directory/name
            p.write_bytes(b'valid data')
            parts.append({'name':name,'bytes':p.stat().st_size,'sha256':backup.checksum(p)})
        manifest={'format':2,'state':'complete','mode':'plain','parts':parts,
                  'databases':[{'database':'center_messenger','part':'database-000.dump',
                                'restore_verified':True,'full_dump_read_verified':True}]}
        backup.save_json(self.directory/'manifest.json',manifest)
        return manifest

    def test_verify_checks_every_mandatory_file(self):
        manifest=self.complete_fixture()
        self.assertEqual(backup.verify_integrity(self.directory),manifest)
        (self.directory/'container-images.tar.gz').write_bytes(b'damaged')
        with self.assertRaisesRegex(backup.BackupError,'checksum mismatch'):
            backup.verify_integrity(self.directory)

    def test_verify_rejects_path_escape_symlink_missing_part_and_unverified_restore(self):
        for mode in ['escape','symlink','missing','not-verified']:
            with self.subTest(mode=mode):
                manifest=self.complete_fixture()
                if mode=='escape':
                    manifest['parts'].append({'name':'../private','bytes':0,'sha256':'a'*64})
                elif mode=='symlink':
                    p=self.directory/'runtime-audit.json'
                    p.unlink()
                    p.symlink_to(self.directory/'RESTORE.txt')
                elif mode=='missing':
                    manifest['parts']=[p for p in manifest['parts'] if p['name']!='RESTORE.txt']
                else:
                    manifest['databases'][0]['restore_verified']=False
                backup.save_json(self.directory/'manifest.json',manifest)
                with self.assertRaises(backup.BackupError):
                    backup.verify_integrity(self.directory)
                (self.directory/'runtime-audit.json').unlink(missing_ok=True)

    def test_backup_lock_excludes_deployment_and_never_changes_timers(self):
        with patch.object(backup,'ROOT',self.root), patch.object(backup.fcntl,'flock') as flock, \
             patch.object(backup.subprocess,'run') as run:
            with backup.locks(self.root):
                self.assertEqual(flock.call_count,2)
        self.assertTrue((self.root/'.deploy.lock').exists())
        run.assert_not_called()

    def test_private_files_stay_private_with_permissive_umask(self):
        old=os.umask(0)
        try:
            backup.save_json(self.directory/'private.json',{'secret':'value'})
            self.service.runner.run([sys.executable,'-c','print("private")'],output_file=self.directory/'private.dump')
        finally:
            os.umask(old)
        self.assertEqual((self.directory/'private.json').stat().st_mode&0o777,0o600)
        self.assertEqual((self.directory/'private.dump').stat().st_mode&0o777,0o600)

    def test_minimal_paths_keep_symlinks_but_deduplicate_nested_directories(self):
        parent=self.root/'project'
        parent.mkdir()
        child=parent/'file'
        child.touch()
        link=self.root/'linked-project'
        link.symlink_to(parent,target_is_directory=True)
        self.assertEqual(set(backup.minimal_paths([parent,child,link])),{parent,link})

    def test_inventory_includes_distinct_max_image_guard_and_external_secrets(self):
        project=self.root/'project'
        project.mkdir()
        (project/'compose.yaml').write_text('services: {}')
        trust=self.root/'guard'
        trust.mkdir()
        external_key=self.root/'external-key'
        external_key.write_text('private-key')
        containers={name:self.container(name,image='sha256:'+name) for name in (backup.API,backup.MAX,backup.DB)}
        containers[backup.DB]['Mounts']=[{'Type':'volume','Source':'/var/lib/docker/live-pgdata',
            'Destination':'/var/lib/postgresql/data','RW':True}]
        containers[backup.MAX]['Mounts']=[{'Type':'bind','Source':str(external_key),
            'Destination':'/run/secrets/connection_master_key','RW':False}]
        images=[{'Id':c['Image'],'Size':100} for c in containers.values()]
        def run(args,**kwargs):
            if args[:2]==['docker','ps']:
                return '\n'.join(containers)
            if args[:3]==['docker','image','inspect']:
                return json.dumps(images)
            if args[0]=='du':
                return '1000\ttotal'
            return 'inactive'
        def sql(runner,db,query,**kwargs):
            if 'json_agg(datname' in query:
                return '["center_messenger","postgres"]'
            if 'data_directory' in query:
                return '/var/lib/postgresql/data'
            if 'tablespace' in query:
                return '0'
            if 'rolsuper' in query:
                return 't'
            return '1000'
        self.service.runner=Mock()
        self.service.runner.run.side_effect=run
        real_glob=Path.glob
        def path_glob(path,pattern):
            return iter([project]) if path==Path('/opt') and pattern=='center-messenger*' else real_glob(path,pattern)
        with patch.object(backup,'ROOT',project),patch.object(backup,'TRUST',trust), \
             patch.object(backup,'inspect',side_effect=lambda r,n:containers[n]), \
             patch.object(backup,'read_sql',side_effect=sql),patch.object(backup,'audit',return_value={'issues':[]}), \
             patch.object(self.service,'external_config_paths',return_value=[]), \
             patch.object(backup.shutil,'which',return_value='/usr/bin/tool'), \
             patch.object(backup.shutil,'disk_usage',return_value=Mock(free=100*backup.GIB)), \
             patch.object(backup.glob,'glob',return_value=[]),patch.object(Path,'glob',path_glob):
            self.service.inventory()
        self.assertEqual(set(self.service.images),{c['Config']['Image'] for c in containers.values()})
        self.assertIn(external_key,self.service.paths)
        self.assertIn(trust,self.service.paths)
        self.assertNotIn(Path('/var/lib/docker/live-pgdata'),self.service.paths)
        self.assertFalse(any('stop' in c.args[0] for c in self.service.runner.run.call_args_list))

    def test_complete_manifest_is_published_only_after_verified_restores(self):
        self.complete_fixture()
        for p in (self.directory/'full_backup_split.py',self.directory/'RESTORE.txt'):
            p.unlink()
        self.service.metadata['databases']=[{'database':'center_messenger','part':'database-000.dump',
            'restore_verified':True,'full_dump_read_verified':True}]
        self.service.metadata['host_paths']=[]
        backup.save_json(self.service.work/'containers.json',[])
        backup.save_json(self.service.work/'images.json',[])
        self.service.finish()
        result=backup.verify_integrity(self.directory)
        self.assertEqual(result['state'],'complete')
        self.assertFalse(self.service.work.exists())
        self.assertIn('не зашифрован',(self.directory/'RESTORE.txt').read_text())

    def test_background_start_has_no_github_credentials_or_production_restart(self):
        log=self.root/'safe-log'
        real_path=backup.Path
        def paths(value):
            return log if value=='/var/log/ramessenger-full-backup.log' else real_path(value)
        with patch.object(backup,'Path',side_effect=paths),patch.object(backup.subprocess,'run') as run:
            backup.start_background(self.root/'backups')
        command=run.call_args.args[0]
        self.assertEqual(command[0],'systemd-run')
        self.assertIn('--run-id',command)
        self.assertFalse(any('LoadCredential' in x or 'restart' in x or 'stop' in x or 'gh'==x for x in command))

    def test_restore_command_stops_at_disk_reserve(self):
        with patch.object(backup.shutil,'disk_usage',return_value=Mock(free=backup.RESERVE-1)):
            with self.assertRaisesRegex(backup.BackupError,'preserve 3 GiB'):
                self.service.runner.run([sys.executable,'-c','import time; time.sleep(60)'],
                                        discard=True,guard_space=True)

    def test_space_watched_restore_supports_direct_file_stdin(self):
        source=self.directory/'dump'
        source.write_bytes(b'example payload')
        self.service.runner.run([sys.executable,'-c','import sys; assert sys.stdin.buffer.read()==b"example payload"'],
                                input_file=source,discard=True,guard_space=True)


if __name__=='__main__':
    unittest.main()
