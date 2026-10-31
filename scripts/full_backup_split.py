#!/usr/bin/env python3
"""Online, unencrypted messenger backup and read-only post-split audit.

No production containers, services, queues or sessions are stopped or changed.
Each database dump and its table counts use the same exported snapshot. Every
dump is restored in a labelled, network-isolated PostgreSQL copy before success.
"""
import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import fcntl
import glob
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4

ROOT = Path('/opt/center-messenger-github')
TRUST = Path('/etc/ramessenger-max-guard')
API, MAX, DB = 'center-messenger-api-1', 'center-messenger-max-worker-1', 'center-messenger-db-1'
TELEPHONY = 'center-messenger-telephony-worker-1'
PROJECT = 'center-messenger'
GIB = 1024 ** 3
RESERVE = 3 * GIB
RUN_ID = re.compile(r'\d{8}T\d{6}Z-[a-f0-9]{12}')


class BackupError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def announce(*args):
    print(now(), *args, flush=True)


def private_dir(path):
    if path.is_symlink():
        raise BackupError('Private directory must not be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid():
        raise BackupError('Private directory has an unexpected owner')
    path.chmod(0o700)


def save_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value):
    return "'" + value.replace("'", "''") + "'"


def env_of(container):
    return dict(item.split('=', 1) for item in container['Config'].get('Env', []) if '=' in item)


def safe_container(container):
    env = env_of(container)
    return {'name': container['Name'].lstrip('/'), 'id': container['Id'], 'image_id': container['Image'],
            'image': container['Config']['Image'], 'build_sha': env.get('CM_BUILD_SHA'),
            'role': env.get('CM_RUNTIME_ROLE'), 'state': container['State']['Status'],
            'health': container['State'].get('Health', {}).get('Status'),
            'started_at': container['State'].get('StartedAt'), 'restarts': container.get('RestartCount', 0)}


class Runner:
    def __init__(self, work=None):
        self.work, self.index = work, 0
        self.space_paths = [work] if work else []

    def errors(self, stack):
        if self.work is None:
            return subprocess.DEVNULL
        self.index += 1
        path = self.work / ('command-%03d.stderr' % self.index)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        return stack.enter_context(os.fdopen(descriptor, 'wb'))

    def run(self, args, *, input_file=None, output_file=None, input_text=None,
            timeout=120, check=True, discard=False, guard_space=False):
        with ExitStack() as stack:
            source = stack.enter_context(input_file.open('rb')) if input_file else None
            dest = stack.enter_context(output_file.open('xb')) if output_file else (
                subprocess.DEVNULL if discard else subprocess.PIPE)
            if output_file:
                output_file.chmod(0o600)
            errors = self.errors(stack)
            if guard_space:
                if input_text or not discard:
                    raise BackupError('Space-watched command requires file stdin and discarded stdout')
                process = subprocess.Popen(args, stdin=source, stdout=dest, stderr=errors, start_new_session=True)
                deadline, progress = time.monotonic() + timeout, time.monotonic()
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise BackupError('Restore command timed out')
                        if any(shutil.disk_usage(p).free < RESERVE for p in self.space_paths):
                            raise BackupError('Restore stopped to preserve 3 GiB free disk')
                        if time.monotonic() - progress >= 10:
                            announce('RESTORING_DATABASE')
                            progress = time.monotonic()
                        time.sleep(.2)
                    result = subprocess.CompletedProcess(args, process.returncode, b'')
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
            else:
                result = subprocess.run(args, stdin=source, input=input_text.encode() if input_text else None,
                                        stdout=dest, stderr=errors, timeout=timeout)
        if check and result.returncode:
            raise BackupError('Command failed: ' + Path(args[0]).name + '; inspect private command logs')
        return (result.stdout or b'').decode(errors='strict').strip()

    def stream(self, commands, output, *, timeout=7200):
        processes, previous = [], None
        started, progress = time.monotonic(), time.monotonic()
        with ExitStack() as stack:
            dest = stack.enter_context(output.open('xb'))
            output.chmod(0o600)
            try:
                for index, args in enumerate(commands):
                    process = subprocess.Popen(args, stdin=previous,
                        stdout=dest if index == len(commands) - 1 else subprocess.PIPE,
                        stderr=self.errors(stack), start_new_session=True)
                    if previous:
                        previous.close()
                    previous = process.stdout
                    processes.append(process)
                while any(p.poll() is None for p in processes):
                    if time.monotonic() - started > timeout:
                        raise BackupError('Archive command timed out')
                    if shutil.disk_usage(output.parent).free < RESERVE:
                        raise BackupError('Backup stopped to preserve 3 GiB of free disk')
                    if time.monotonic() - progress >= 10:
                        announce('COPYING', output.name, 'MiB=' + str(output.stat().st_size // 1024 ** 2))
                        progress = time.monotonic()
                    time.sleep(.2)
                if any(p.returncode != 0 for p in processes):
                    raise BackupError('Archive command failed; inspect private command logs')
                dest.flush()
                os.fsync(dest.fileno())
            finally:
                for process in processes:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


def inspect(runner, name):
    return json.loads(runner.run(['docker', 'inspect', name]))[0]


def read_sql(runner, db_id, query, database='center_messenger', user='cm_owner'):
    # Transaction defaults also cover a SELECT invoking an unexpected function.
    sql = "BEGIN READ ONLY; SET LOCAL statement_timeout='30s'; " + query + '; ROLLBACK;\n'
    return runner.run(['docker', 'exec', '-i', db_id, 'psql', '-XqAt', '-v', 'ON_ERROR_STOP=1',
                       '-U', user, '-d', database], input_text=sql, timeout=45)


AUDIT_SQL = """SELECT json_build_object(
 'checked_at',now(), 'schema',(SELECT version_num FROM alembic_version),
 'connections',(SELECT coalesce(json_agg(json_build_object('workspace_id',workspace_id,
   'state',state,'last_sync_at',last_sync_at,'error_code',last_error_code,
   'sync_lag_seconds',CASE WHEN last_sync_at IS NOT NULL THEN greatest(0,extract(epoch FROM now()-last_sync_at)) END,
   'activity_phase',history_cursor#>>'{activity,phase}',
   'activity_at',history_cursor#>>'{activity,updated_at}')), '[]'::json) FROM alfa_max_settings),
 'history_queue',(SELECT coalesce(json_agg(json_build_object('workspace_id',workspace_id,
   'due_at',CASE WHEN isfinite(due_at) THEN due_at END,
   'lease_active',lease_owner IS NOT NULL AND lease_until>now(),
   'lease_until',CASE WHEN isfinite(lease_until) THEN lease_until END)), '[]'::json) FROM alfa_max_sync_queue),
 'send_states',(SELECT coalesce(json_object_agg(state,n),'{}'::json) FROM (
   SELECT s.state,count(*) n FROM send_commands s JOIN conversations c
   ON c.workspace_id=s.workspace_id AND c.id=s.conversation_id JOIN connections cn
   ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id
   WHERE cn.kind='alfa_max_browser' AND s.created_at>=now()-interval '24 hours' GROUP BY s.state) s),
 'failed_send_codes',(SELECT coalesce(json_agg(row_to_json(f)), '[]'::json) FROM (
   SELECT s.last_error_code,count(*) n,min(s.updated_at) first_at,max(s.updated_at) last_at
   FROM send_commands s JOIN conversations c ON c.workspace_id=s.workspace_id AND c.id=s.conversation_id
   JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id
   WHERE cn.kind='alfa_max_browser' AND s.state='failed' AND s.created_at>=now()-interval '24 hours'
   GROUP BY s.last_error_code) f),
 'waiting_over_5_minutes',(SELECT count(*) FROM send_commands s JOIN conversations c
   ON c.workspace_id=s.workspace_id AND c.id=s.conversation_id JOIN connections cn
   ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id WHERE cn.kind='alfa_max_browser'
   AND s.state IN ('queued','sending') AND s.updated_at<now()-interval '5 minutes'),
 'accepted_without_saved_message',(SELECT count(*) FROM send_commands s JOIN conversations c
   ON c.workspace_id=s.workspace_id AND c.id=s.conversation_id JOIN connections cn
   ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id WHERE cn.kind='alfa_max_browser'
   AND s.state IN ('accepted','delivered','read') AND s.updated_at>=now()-interval '24 hours'
   AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.workspace_id=s.workspace_id AND m.send_command_id=s.id)),
 'journal_last_24_hours',(SELECT coalesce(json_agg(row_to_json(j)), '[]'::json) FROM (
   SELECT m.direction,count(*) imported,max(m.occurred_at) last_message_at,max(m.received_at) last_import_at
   FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id
   JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id
   WHERE cn.kind='alfa_max_browser' AND m.payload ? 'crm_record_id'
   AND m.received_at>=now()-interval '24 hours' GROUP BY m.direction) j),
 'native_jobs',(SELECT count(*) FROM message_jobs WHERE channel_kind='alfa_max_browser'))"""


def audit(runner, root=ROOT, trust=None):
    trust = TRUST if trust is None else trust
    runtime_names=[API,MAX,DB]+([TELEPHONY] if (root/'telephony-runtime.json').exists() else [])
    containers = {name: inspect(runner, name) for name in runtime_names}
    issues = []
    for name, container in containers.items():
        if container['State']['Status'] != 'running' or container['State'].get('Health', {}).get('Status') != 'healthy':
            issues.append(name + ': not healthy')
    if env_of(containers[API]).get('CM_RUNTIME_ROLE') != 'api' or env_of(containers[MAX]).get('CM_RUNTIME_ROLE') != 'max':
        issues.append('Runtime roles are not split')
    transition = json.loads((root / 'max-runtime-transition.json').read_text())
    worker = containers[MAX]
    if (transition.get('phase') != 'complete' or transition.get('worker_id') != worker['Image']
            or transition.get('worker_image') != worker['Config']['Image']):
        issues.append('MAX transition or image pin disagrees')
    pins = [line.split('=', 1)[1].strip() for line in (root / '.env').read_text().splitlines()
            if line.startswith('CM_MAX_IMAGE=')]
    if pins != [worker['Config']['Image']]:
        issues.append('CM_MAX_IMAGE disagrees')
    if TELEPHONY in containers:
        phone=json.loads((root/'telephony-runtime.json').read_text())
        item=containers[TELEPHONY]
        pins=[line.split('=',1)[1].strip() for line in (root/'.env').read_text().splitlines() if line.startswith('CM_TELEPHONY_IMAGE=')]
        if phone.get('phase')!='complete' or phone.get('worker_id')!=item['Image'] or pins!=[item['Config']['Image']]:
            issues.append('Telephony image pin disagrees')
    generation = (trust / 'active').resolve(strict=True)
    if generation.parent != trust or not re.fullmatch('[a-f0-9]{64}', generation.name):
        raise BackupError('Unexpected installed MAX guard location')
    policy_path = generation / 'deploy/max-protection.json'
    if checksum(policy_path) != generation.name:
        issues.append('Installed MAX policy checksum disagrees')
    policy = json.loads(policy_path.read_text())
    database = json.loads(read_sql(runner, containers[DB]['Id'], AUDIT_SQL))
    if database['schema'] != policy['schema_revision']:
        issues.append('Database schema disagrees with installed MAX policy')
    if database['accepted_without_saved_message']:
        issues.append('Accepted commands without saved messages require investigation')
    if database.get('waiting_over_5_minutes'):
        issues.append('Native send commands have waited for over 5 minutes')
    for connection in database['connections']:
        if connection['state'] != 'active':
            issues.append('Native session is not active: ' + connection['workspace_id'])
        elif 'last_sync_at' in connection and connection['last_sync_at'] is None:
            issues.append('Native history has no recorded successful read: ' + connection['workspace_id'])
        elif (connection.get('sync_lag_seconds') or 0) > 300:
            issues.append('Native history has no successful read for over 5 minutes: ' + connection['workspace_id'])
        if connection.get('error_code'):
            issues.append('Native history reports ' + connection['error_code'] + ': ' + connection['workspace_id'])
        elif connection.get('activity_phase') == 'error':
            issues.append('Native history activity reports an error: ' + connection['workspace_id'])
    try:
        with urllib.request.urlopen('http://127.0.0.1:8100/health/ready', timeout=5) as response:
            value = json.load(response)
            api_ready = response.status == 200 and isinstance(value, dict) and value.get('status') == 'ready'
    except (OSError, ValueError, HTTPException):
        # An audit during API replacement still returns the DB/runtime evidence;
        # a connection reset here is not a failure of a completed backup.
        api_ready = False
    if not api_ready:
        issues.append('Public API readiness failed')
    return {'checked_at': now(), 'read_only': True, 'crm_requests': 0, 'message_posts': 0,
            'installed_policy': generation.name,
            'containers': [safe_container(c) for c in containers.values()], 'database': database,
            'api_ready': api_ready, 'issues': issues,
            'limits': ['Stored journal and queue evidence; no live delivery or CRM session recovery test',
                       'No API restart was performed; compare worker ID/start time across a later ordinary release']}


class Snapshot:
    """Keep the snapshot owner open until pg_dump and source counts finish."""
    def __init__(self, runner, db_id, database):
        self.runner, self.db_id, self.database = runner, db_id, database

    def __enter__(self):
        self.stack = ExitStack()
        try:
            self.process = subprocess.Popen(['docker', 'exec', '-i', self.db_id, 'psql', '-XqAt',
                '-v', 'ON_ERROR_STOP=1', '-U', 'cm_owner', '-d', self.database],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.runner.errors(self.stack),
                text=True, bufsize=1, start_new_session=True)
        except BaseException:
            self.stack.close()
            raise
        try:
            self.id = self.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
                "SET LOCAL statement_timeout='60s'; SET LOCAL idle_in_transaction_session_timeout='20min'; "
                'SELECT pg_export_snapshot()')
            if not re.fullmatch('[A-Fa-f0-9]+-[A-Fa-f0-9]+-[0-9]+', self.id):
                raise BackupError('Invalid exported PostgreSQL snapshot')
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def query(self, query):
        self.process.stdin.write(query + ';\n')
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=90):
                raise BackupError('Snapshot query timed out')
            value = self.process.stdout.readline().strip()
        if not value:
            raise BackupError('Snapshot session closed unexpectedly')
        return value

    def __exit__(self, *error):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write('ROLLBACK;\n')
                    self.process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=10)
            if not error[0] and self.process.returncode:
                raise BackupError('Exported snapshot session failed')
        finally:
            self.process.stdout.close()
            if not self.process.stdin.closed:
                self.process.stdin.close()
            self.stack.close()


def table_counts(query):
    tables = json.loads(query("SELECT coalesce(json_agg(json_build_array(n.nspname,c.relname) "
        "ORDER BY n.nspname,c.relname),'[]'::json) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relkind IN ('r','p','m') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'"))
    if not tables:
        return {}
    counts = ['SELECT %d AS i,count(*) AS n FROM %s.%s' %
              (i, quote_identifier(schema), quote_identifier(table)) for i, (schema, table) in enumerate(tables)]
    values = json.loads(query('SELECT json_object_agg(i::text,n) FROM (' + ' UNION ALL '.join(counts) + ') x'))
    return {json.dumps(pair, ensure_ascii=False): values[str(i)] for i, pair in enumerate(tables)}


def minimal_paths(paths):
    result = []
    for path in sorted(set(Path(p).absolute() for p in paths), key=lambda p: (len(p.parts), str(p))):
        if path.exists() or path.is_symlink():
            if not any(parent in path.parents and parent.is_dir() and not parent.is_symlink() for parent in result):
                result.append(path)
    return result


def read_manifest(directory):
    """Validate the inventory; callers must still verify every payload hash."""
    if directory.is_symlink():
        raise BackupError('Backup path must not be a symlink')
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_symlink():
        raise BackupError('Backup manifest must not be a symlink')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('format') != 2 or manifest.get('state') != 'complete' or manifest.get('mode') != 'plain':
        raise BackupError('Not a completed split-runtime backup')
    required = {'deployment-files.tar.gz', 'container-images.tar.gz', 'postgres-globals.sql',
                'recovery-metadata.json', 'runtime-audit.json', 'full_backup_split.py', 'RESTORE.txt'}
    parts = manifest.get('parts', [])
    names = [part['name'] for part in parts]
    required.update(item['part'] for item in manifest.get('databases', []))
    if not manifest.get('databases') or len(names) != len(set(names)) or not required.issubset(names):
        raise BackupError('Backup manifest is missing mandatory parts')
    if not all(item.get('restore_verified') and item.get('full_dump_read_verified') for item in manifest['databases']):
        raise BackupError('Backup lacks complete database restore verification')
    for part in parts:
        name = part['name']
        if not isinstance(name, str) or Path(name).name != name or name in {'.', '..'}:
            raise BackupError('Unsafe part name')
        if (type(part.get('bytes')) is not int or part['bytes'] < 0
                or not isinstance(part.get('sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', part['sha256'])):
            raise BackupError('Invalid backup part size/checksum: ' + name)
    return manifest


def verify_part(directory, part):
    path = directory / part['name']
    if path.is_symlink() or not path.is_file() or path.stat().st_size != part['bytes'] or checksum(path) != part['sha256']:
        raise BackupError('Backup checksum mismatch: ' + part['name'])


def verify_integrity(directory):
    manifest = read_manifest(directory)
    for part in manifest['parts']:
        verify_part(directory, part)
    return manifest


def snapshot_sqlite(source_path, destination):
    """Copy a live WAL database to a closed, self-contained rollback-mode file."""
    # sqlite3.Connection's context manager does not close the connection.
    # Leaving the destination open would seal temporary WAL/SHM files that
    # disappear only when the backup process exits.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(source_path.absolute().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                raise BackupError('Support queue snapshot is not standalone')
            if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise BackupError('Support queue snapshot is invalid')
    if any(Path(str(destination) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise BackupError('Support queue snapshot still has temporary files')


class FullBackup:
    def __init__(self, directory):
        self.directory = directory
        self.work = directory / 'private-work'
        private_dir(self.work)
        self.runner = Runner(self.work)
        self.script_bytes = Path(__file__).read_bytes()
        self.clone, self.volume = 'cm-backup-check-' + directory.name[-12:], 'cm-backup-check-data-' + directory.name[-12:]
        self.clone_created = self.volume_created = False
        self.metadata = {'format': 2, 'state': 'running', 'mode': 'plain', 'started_at': now(),
            'run_id': directory.name, 'scope': 'messenger project, all project directories and logical databases',
            'production_stopped': False, 'databases': [], 'parts': [],
            'backup_script_sha256': hashlib.sha256(self.script_bytes).hexdigest()}

    def phase(self, name):
        self.metadata['phase'] = name
        save_json(self.directory / 'status.json', self.metadata)
        announce('PHASE', name)

    def inventory(self):
        self.phase('inventory')
        for name in ('docker', 'tar', 'gzip', 'du'):
            if not shutil.which(name):
                raise BackupError('Required command not found: ' + name)
        for unit in ('center-messenger-gitverse-runner.service', 'center-messenger-deploy.service',
                     'center-messenger-deploy.timer', 'ramessenger-github-deploy.service',
                     'ramessenger-github-deploy.timer'):
            if self.runner.run(['systemctl', 'is-active', unit], check=False) == 'active':
                raise BackupError('Legacy deployment owner is active: ' + unit)
        names = self.runner.run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=' + PROJECT,
                                 '--format', '{{.Names}}']).splitlines()
        expected={API,MAX,DB}|({TELEPHONY} if (ROOT/'telephony-runtime.json').exists() else set())
        if set(names) != expected:
            raise BackupError('Unexpected project containers; inspect inventory first')
        self.containers = {name: inspect(self.runner, name) for name in [API,MAX,DB]+([TELEPHONY] if TELEPHONY in expected else [])}
        for name, container in self.containers.items():
            if container['State']['Status'] != 'running' or container['State'].get('Health', {}).get('Status') != 'healthy':
                raise BackupError('Project container is not healthy: ' + name)
            if name != DB and (not container['HostConfig'].get('ReadonlyRootfs') or
                any(m.get('RW') for m in container.get('Mounts', []) if m['Type'] in ('bind','volume'))):
                raise BackupError('Writable application files require a separate consistent file snapshot')
        audit_result = audit(self.runner)
        save_json(self.directory / 'runtime-audit.json', audit_result)
        if audit_result['issues']:
            # A stale session or a historical failed send must not prevent
            # preserving data. Structural container/mount checks remain strict.
            announce('AUDIT_REQUIRES_REVIEW', '; '.join(audit_result['issues']))
        self.db = self.containers[DB]
        self.db_id = self.db['Id']
        self.databases = json.loads(read_sql(self.runner, self.db_id,
            "SELECT json_agg(datname ORDER BY datname) FROM pg_database WHERE datallowconn AND NOT datistemplate"))
        if 'center_messenger' not in self.databases:
            raise BackupError('Messenger database is missing')
        data_directory = read_sql(self.runner, self.db_id, "SELECT current_setting('data_directory')")
        data_mount = next((m for m in self.db['Mounts'] if m['Destination'] == data_directory), None)
        if not data_mount or data_directory != '/var/lib/postgresql/data':
            raise BackupError('Unexpected PostgreSQL data layout')
        self.runner.space_paths = [self.directory, Path(data_mount['Source'])]
        if read_sql(self.runner, self.db_id, "SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global')") != '0':
            raise BackupError('Additional tablespaces require a reviewed recovery plan')
        if read_sql(self.runner, self.db_id, "SELECT rolsuper FROM pg_roles WHERE rolname=current_user") != 't':
            raise BackupError('Full backup requires the local PostgreSQL owner role')
        self.db_bytes = int(read_sql(self.runner, self.db_id, 'SELECT sum(pg_database_size(oid)) FROM pg_database'))
        mounts = [m for c in self.containers.values() for m in c.get('Mounts', [])
                  if m['Type'] in ('bind','volume') and m['Source'] != data_mount['Source']]
        paths = list(Path('/opt').glob('center-messenger*')) + [Path(m['Source']) for m in mounts]
        paths += [TRUST, Path('/etc/systemd/system/ramessenger-local-build.service.d')]
        paths += [Path(p) for pattern in ('/etc/systemd/system/*messenger*', '/usr/local/bin/cm-*',
            '/usr/local/sbin/cm-*', '/usr/local/bin/*messenger*', '/usr/local/sbin/*messenger*') for p in glob.glob(pattern)]
        paths += [Path(p) for p in ('/etc/nginx','/etc/letsencrypt','/etc/ssl','/etc/docker',
            '/etc/sudoers.d/cm-support','/home/cm-support','/root/.config/gh','/root/.ssh',
            '/root/.docker/config.json','/etc/cron.d','/etc/crontab','/var/spool/cron/crontabs/root')]
        paths += self.external_config_paths()
        paths += [p.resolve() for p in paths if p.is_symlink() and p.exists()]
        self.paths = minimal_paths(paths)
        if ROOT not in self.paths or TRUST not in self.paths:
            raise BackupError('Deployment files or installed MAX protection are missing')
        for path in self.paths:
            if path == Path('/') or path == self.directory or path in self.directory.parents:
                raise BackupError('Backup output overlaps its source files')
            if path == Path(data_mount['Source']) or path in Path(data_mount['Source']).parents:
                raise BackupError('Live PGDATA must not be archived as ordinary files')
        self.images = list(dict.fromkeys(c['Config']['Image'] for c in self.containers.values()))
        images = json.loads(self.runner.run(['docker', 'image', 'inspect', *self.images]))
        for reference, image in zip(self.images, images, strict=True):
            if any(c['Image'] != image['Id'] for c in self.containers.values() if c['Config']['Image'] == reference):
                raise BackupError('Image tag no longer matches its running container')
        image_bytes = sum({i['Id']: i.get('Size', 0) for i in images}.values())
        host_bytes = int(self.runner.run(['du', '-sb', '--total', *map(str, self.paths)], timeout=600).splitlines()[-1].split()[0])
        needed = host_bytes + image_bytes * 1.1 + self.db_bytes * 3 + RESERVE
        if shutil.disk_usage(self.directory).free < needed:
            raise BackupError('Insufficient disk: need %.2f GiB including 3 GiB reserve; old copies are not deleted' % (needed / GIB))
        memory = dict((line.split(':', 1)[0], int(line.split()[1])) for line in Path('/proc/meminfo').read_text().splitlines())
        if memory.get('MemAvailable', 0) < 768 * 1024:
            raise BackupError('Less than 768 MiB available for isolated restore verification')
        self.metadata.update(containers=[safe_container(c) for c in self.containers.values()],
            host_paths=list(map(str, self.paths)), image_references=self.images, database_bytes=self.db_bytes)
        save_json(self.work / 'containers.json', list(self.containers.values()))
        save_json(self.work / 'images.json', images)
        announce('SPACE_OK', 'required_GiB=%.2f' % (needed / GIB))

    def external_config_paths(self):
        paths = []
        units = self.runner.run(['systemctl', 'cat', '*messenger*'], check=False)
        (self.work / 'systemd-units.txt').write_text(units)
        paths += [Path(p) for p in re.findall(r'(?m)^# (/[^\n]+)$', units)
                  if not p.startswith('/run/systemd/transient/ramessenger-full-backup-')]
        selected, skip = [], False
        for line in units.splitlines():
            if line.startswith('# /'):
                skip = Path(line[2:]).name.startswith('ramessenger-full-backup-')
            if not skip:
                selected.append(line)
        import shlex
        for line in '\n'.join(selected).replace('\\\n', '').splitlines():
            if line.startswith('EnvironmentFile='):
                for value in shlex.split(line.split('=', 1)[1]):
                    value = value.removeprefix('-')
                    if not value.startswith('/') or '$' in value or '%' in value:
                        raise BackupError('Dynamic unit dependency needs explicit inventory')
                    paths += [Path(p) for p in glob.glob(value)]
            elif re.match(r'^Exec(?:Start|StartPre|StartPost|Stop|StopPost|Reload)=', line):
                for value in shlex.split(line.split('=', 1)[1]):
                    path = Path(value.lstrip('-+!:@'))
                    if path.is_absolute() and path.exists() and str(path).startswith(('/opt/', '/root/', '/usr/local/')):
                        paths.append(path)
        if shutil.which('nginx'):
            # nginx -T contains private config. Do not print it in the journal.
            text = self.runner.run(['nginx', '-T'])
            (self.work / 'nginx-config.txt').write_text(text)
            paths += [Path(p) for p in re.findall(r'(?m)^# configuration file (/[^\n]+):$', text)]
            for value in re.findall(r'(?m)^\s*(?:ssl_certificate|ssl_certificate_key|ssl_trusted_certificate|ssl_dhparam|auth_basic_user_file)\s+([^;]+);', text):
                value = value.strip().strip('"').strip("'")
                if '$' in value or not value.startswith('/'):
                    raise BackupError('Dynamic proxy dependency needs explicit inventory')
                paths += [Path(value), Path(value).resolve()]
        return paths

    def add_part(self, name, commands):
        temp = self.directory / (name + '.incomplete')
        self.runner.stream(commands, temp)
        path = self.directory / name
        os.replace(temp, path)
        self.metadata['parts'].append({'name': name, 'bytes': path.stat().st_size, 'sha256': checksum(path)})
        announce('SAVED', name, 'MiB=' + str(path.stat().st_size // 1024 ** 2))
        return path

    def files(self):
        self.phase('deployment_files')
        queue = ROOT / 'support/requests.sqlite'
        excludes = []
        if queue.exists():
            copy = self.directory / 'support-queue.sqlite'
            snapshot_sqlite(queue, copy)
            self.metadata['sqlite_restore_path'] = str(queue)
            excludes = [queue, Path(str(queue) + '-wal'), Path(str(queue) + '-shm')]
        # Deployment lock excludes application/config promotions. Live file logs
        # are deliberately omitted; they are not recovery inputs and race tar.
        exclude_names = [str(p).lstrip('/') for p in excludes]
        exclude_names += ['*.log', '*.log.*', 'events.jsonl', '__pycache__']
        paths_file = self.work / 'paths.nul'
        paths_file.write_bytes(b''.join(os.fsencode(str(p).lstrip('/')) + b'\0' for p in self.paths))
        command = ['tar', '--create', '--gzip', '--numeric-owner', '--acls', '--xattrs', '--sparse', '--file=-',
                   '--directory=/'] + ['--exclude=' + p for p in exclude_names]
        command += ['--null', '--verbatim-files-from', '--files-from=' + str(paths_file)]
        path = self.add_part('deployment-files.tar.gz', [command])
        self.runner.run(['tar', '-tzf', str(path)], discard=True, timeout=7200)
        self.phase('container_images')
        path = self.add_part('container-images.tar.gz', [['docker', 'image', 'save', *self.images], ['gzip', '-1']])
        self.runner.run(['tar', '-tzf', str(path)], discard=True, timeout=7200)

    def database_files(self):
        self.phase('database_snapshots')
        for name, previous in self.containers.items():
            current = inspect(self.runner, name)
            if current['Id'] != previous['Id'] or current['Image'] != previous['Image']:
                raise BackupError('Production container changed during backup')
        self.runner.run(['docker', 'exec', self.db_id, 'pg_dumpall', '-U', 'cm_owner', '--globals-only'],
                        output_file=self.directory / 'postgres-globals.sql', timeout=300)
        for index, database in enumerate(self.databases):
            with Snapshot(self.runner, self.db_id, database) as snapshot:
                at = snapshot.query('SELECT transaction_timestamp()')
                counts = table_counts(snapshot.query)
                schema = snapshot.query('SELECT version_num FROM alembic_version') if database == 'center_messenger' else None
                name = 'database-%03d.dump' % index
                path = self.add_part(name, [['docker', 'exec', self.db_id, 'pg_dump', '-U', 'cm_owner',
                    '-d', database, '--format=custom', '--create', '--snapshot=' + snapshot.id, '--lock-wait-timeout=10s']])
            # Direct file stdin: pg_restore --list can stop reading early. Do not
            # feed it through cat, whose SIGPIPE previously caused false failure.
            self.runner.run(['docker', 'exec', '-i', self.db_id, 'pg_restore', '--list'],
                            input_file=path, discard=True, timeout=300)
            self.runner.run(['docker', 'exec', '-i', self.db_id, 'pg_restore', '--file=/dev/null'],
                            input_file=path, discard=True, timeout=600)
            self.metadata['databases'].append({'database': database, 'part': name, 'snapshot_at': at,
                'schema_revision': schema, 'table_counts': counts, 'full_dump_read_verified': True})

    def remove_owned(self, kind, name):
        raw = self.runner.run(['docker', kind, 'inspect', name], check=False)
        if not raw:
            return
        obj = json.loads(raw)[0]
        labels = (obj.get('Config') or {}).get('Labels') if kind == 'container' else obj.get('Labels')
        if not labels or labels.get('com.ramessenger.backup') != self.directory.name:
            raise BackupError('Refusing to remove a resource not owned by this backup')
        self.runner.run(['docker', kind, 'rm', *(['-f'] if kind == 'container' else []), name])

    def verify_restore(self):
        self.phase('isolated_restore')
        label = 'com.ramessenger.backup=' + self.directory.name
        user = 'cm_verify_' + self.directory.name[-12:]
        self.volume_created = True  # Creation can succeed even if the client times out.
        self.runner.run(['docker', 'volume', 'create', '--label', label, self.volume])
        self.clone_created = True  # A client timeout may still create a container.
        self.runner.run(['docker', 'run', '-d', '--name', self.clone, '--label', label,
            '--network', 'none', '--memory', '512m', '--cpus', '0.5', '--pids-limit', '128',
            '--mount', 'type=volume,src=' + self.volume + ',dst=/var/lib/postgresql/data',
            '--env', 'POSTGRES_USER=' + user, '--env', 'POSTGRES_DB=postgres',
            '--env', 'POSTGRES_PASSWORD=' + uuid4().hex, self.db['Image'],
            'postgres', '-c', 'listen_addresses=127.0.0.1', '-c', 'shared_buffers=32MB',
            '-c', 'max_connections=10', '-c', 'autovacuum=off', '-c', 'jit=off'])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            # The official image uses a temporary Unix-socket-only server while
            # initializing PGDATA. TCP loopback becomes ready only in the final
            # server, avoiding a restore interrupted by entrypoint's shutdown.
            output = self.runner.run(['docker', 'exec', self.clone, 'pg_isready', '-h', '127.0.0.1',
                                      '-U', user, '-d', 'postgres'], check=False)
            if 'accepting connections' in output:
                break
            time.sleep(2)
        else:
            raise BackupError('Isolated PostgreSQL did not become ready')
        self.runner.run(['docker', 'exec', '-i', self.clone, 'psql', '-Xq', '-v', 'ON_ERROR_STOP=1',
            '-U', user, '-d', 'template1'], input_file=self.directory / 'postgres-globals.sql', discard=True)
        for item in self.metadata['databases']:
            self.runner.run(['docker', 'exec', '-i', self.clone, 'pg_restore', '-U', user,
                '--clean', '--if-exists', '--create', '--exit-on-error', '-d', 'template1'],
                input_file=self.directory / item['part'], discard=True, timeout=1800, guard_space=True)
            query = lambda sql: read_sql(self.runner, self.clone, sql, database=item['database'])
            if table_counts(query) != item['table_counts']:
                raise BackupError('Restored table counts disagree: ' + item['database'])
            if item['schema_revision'] and query('SELECT version_num FROM alembic_version') != item['schema_revision']:
                raise BackupError('Restored schema revision disagrees')
            item['restore_verified'] = True
            announce('DATABASE_RESTORED', item['database'])
        self.metadata['restore_verified_at'] = now()

    def cleanup(self):
        try:
            if self.clone_created:
                self.remove_owned('container', self.clone)
                self.clone_created = False
        finally:
            if self.volume_created and not self.clone_created:
                self.remove_owned('volume', self.volume)
                self.volume_created = False

    def finish(self):
        if not self.metadata['databases'] or not all(x.get('restore_verified') for x in self.metadata['databases']):
            raise BackupError('Restore verification did not finish')
        if any((self.directory / ('support-queue.sqlite' + suffix)).exists()
               for suffix in ('-wal', '-shm', '-journal')):
            raise BackupError('Support queue snapshot still has temporary files')
        self.phase('final_verification')
        self.metadata.update(finished_at=now(), state='complete')
        recovery = {'containers': json.loads((self.work / 'containers.json').read_text()),
                    'images': json.loads((self.work / 'images.json').read_text()),
                    'host_paths': self.metadata['host_paths'], 'databases': self.metadata['databases']}
        save_json(self.directory / 'recovery-metadata.json', recovery)
        (self.directory / 'RESTORE.txt').write_text(restore_notes(), encoding='utf-8')
        (self.directory / 'full_backup_split.py').write_bytes(self.script_bytes)
        known = {p['name'] for p in self.metadata['parts']}
        for path in sorted(self.directory.iterdir()):
            if path.is_file() and path.name not in known | {'manifest.json', 'status.json', 'SHA256SUMS'} and not path.name.endswith('.incomplete'):
                path.chmod(0o600)
                self.metadata['parts'].append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': checksum(path)})
        save_json(self.directory / 'manifest.json', self.metadata)
        (self.directory / 'SHA256SUMS').write_text(''.join(p['sha256'] + '  ' + p['name'] + '\n' for p in self.metadata['parts']))
        (self.directory / 'SHA256SUMS').chmod(0o600)
        verify_integrity(self.directory)
        shutil.rmtree(self.work)
        save_json(self.directory / 'status.json', self.metadata)
        announce('BACKUP_COMPLETE', self.directory)

    def execute(self):
        try:
            with locks(self.directory.parent):
                self.inventory()
                self.files()
                self.database_files()
                self.verify_restore()
                self.cleanup()
                self.finish()
        except BaseException:
            self.metadata['state'] = 'failed'
            self.phase('failed')
            raise
        finally:
            self.cleanup()


@contextmanager
def locks(output_root):
    with ExitStack() as stack:
        for path in (output_root / '.full-backup.lock', ROOT / '.deploy.lock'):
            if path.is_symlink():
                raise BackupError('Lock path must not be a symlink')
            handle = stack.enter_context(path.open('a+'))
            path.chmod(0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupError('Another backup or deployment is running; retry after it finishes') from None
        yield


def restore_notes():
    return '''ОБЫЧНЫЙ ПОЛНЫЙ БЭКАП МЕССЕНДЖЕРА ПОСЛЕ РАЗДЕЛЕНИЯ MAX

Архив не зашифрован. В нём есть пароли, сессии, персональные данные и ключи.
Скопируйте всю папку в закрытое хранилище за пределами этого VPS.
Проверка: python3 full_backup_split.py --verify /путь/к/копии

Содержимое: все найденные /opt/center-messenger* каталоги (включая скрытые
файлы), runtime secrets, установленный /etc/ramessenger-max-guard, настройки
systemd/proxy/SSL, образы API/MAX/PostgreSQL с метаданными, PostgreSQL роли
и все нетиповые базы, отдельный согласованный снимок support-queue.sqlite.
Из файлов исключены временные журналы, __pycache__, исходная живая SQLite
с WAL/SHM и PGDATA. PostgreSQL полностью сохраняется логическими дампами.
Это резервная копия проекта, а не образ всего диска VPS или других сайтов.

Приложения продолжали работать. Для каждой базы pg_dump --snapshot и числа
строк использовали один REPEATABLE READ snapshot. У разных баз время снимка
может различаться; приложение хранит бизнес-данные в center_messenger.
Каждый дамп реально восстановлен в отдельной PostgreSQL того же image ID:
проверены все пользовательские таблицы по числу строк и версия схемы.
Проверены роли, владельцы и ACL штатным восстановлением без --no-owner.
Полное восстановление приложения, SSL/DNS и входа во внешнюю CRM не проверялось.

ВОССТАНОВЛЕНИЕ сначала только на отдельной машине/в изолированном окружении.
1. Проверьте SHA256SUMS и manifest.json. Сохраните исходные копии.
2. Загрузите образы: gzip -dc container-images.tar.gz | docker image load.
   Сверьте image IDs и теги с recovery-metadata.json. Для digest-only образа
   PostgreSQL может понадобиться локальный тег и проверенная правка Compose.
3. Распакуйте deployment-files.tar.gz В ПУСТОЙ КАТАЛОГ, сохраняя числовых
   владельцев, ACL/xattrs. Не накладывайте архив на рабочий сервер.
4. Подготовьте пустую PostgreSQL сохранённой версии. Оставьте API, MAX и
   deploy/support timers выключенными. Восстановите postgres-globals.sql
   через psql с ON_ERROR_STOP, затем базы из manifest.json через pg_restore
   --clean --if-exists --create --exit-on-error -d template1.
5. Перенесите сохранённые secrets с исходными правами, .env, Compose и guard.
   SQLite восстановите из support-queue.sqlite без старых WAL/SHM. Старые
   запросы деплоя сначала просмотрите, не запускайте очередь автоматически.
6. Проверьте БД, RLS, готовность API и закрепление MAX. Контейнеры сначала
   запускайте без доступа к CRM/отправке сообщений. Перед включением MAX
   убедитесь, что исходный worker не работает: не создавайте два production
   подключения. Проверки внешних сессий и реальной доставки выполняются отдельно.

Архивы не удаляются автоматически; очистка старых копий — отдельное решение.
'''


def start_background(output_root):
    private_dir(output_root)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:12]
    unit = 'ramessenger-full-backup-' + stamp[-12:]
    log = Path('/var/log/ramessenger-full-backup.log')
    if log.is_symlink():
        raise BackupError('Backup log must not be a symlink')
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    subprocess.run(['systemd-run', '--unit=' + unit, '--collect', '--property=Type=exec',
        '--property=RuntimeMaxSec=3h', '--property=Nice=19', '--property=CPUQuota=50%',
        '--property=MemoryMax=768M', '--property=StandardOutput=append:' + str(log),
        '--property=StandardError=append:' + str(log), '/usr/bin/python3', str(Path(__file__).resolve()),
        '--run-id', stamp, '--output-root', str(output_root)], check=True, timeout=30)
    announce('BACKUP_STARTED', unit)
    announce('DIRECTORY', output_root / stamp)
    announce('LOG', log)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--audit', action='store_true')
    action.add_argument('--start', action='store_true')
    action.add_argument('--run-id')
    action.add_argument('--verify', type=Path)
    parser.add_argument('--output-root', type=Path, default=Path('/var/backups/ramessenger'))
    options = parser.parse_args(argv)
    if options.verify:
        verify_integrity(options.verify.resolve())
        print('BACKUP_INTEGRITY_OK')
        return 0
    if os.geteuid() != 0:
        raise BackupError('Root required for project container and local PostgreSQL access')
    if options.audit:
        result = audit(Runner())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result['issues'] else 0
    options.output_root = options.output_root.absolute()
    if options.start:
        start_background(options.output_root)
        return 0
    if not RUN_ID.fullmatch(options.run_id):
        raise BackupError('Invalid run identifier')
    private_dir(options.output_root)
    directory = options.output_root / options.run_id
    directory.mkdir(mode=0o700)  # No accidental overwrite/resume of an old copy.
    directory.chmod(0o700)
    def terminated(signum, frame):
        raise BackupError('Backup interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, terminated)
    FullBackup(directory).execute()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # PostgreSQL stderr can contain private data; normal logs show a bounded
        # operational error. Detailed command errors stay in private-work/.
        announce('AUDIT_FAILED' if '--audit' in sys.argv[1:] else 'BACKUP_FAILED',
                 str(error) if isinstance(error, BackupError) else type(error).__name__)
        sys.exit(1)
