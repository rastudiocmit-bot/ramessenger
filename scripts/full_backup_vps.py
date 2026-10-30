#!/usr/bin/env python3
"""Create an encrypted, project-scoped recovery set without deploying code.

Run explicitly as root, preferably in a transient systemd unit. No installation,
migration, global prune, live checkout update, or remote upload is performed.
The encryption key is deliberately outside the backup set. A completed backup
requires a verified PostgreSQL base backup booted in a network-isolated clone.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, UTC
import fcntl
import glob
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from uuid import uuid4

ROOT = Path('/opt/center-messenger-github')
REPO = 'rastudiocmit-bot/ramessenger'
API = 'center-messenger-api-1'
DB = 'center-messenger-db-1'
TIMERS = ('ramessenger-local-build.timer', 'ramessenger-support-queue.timer')
LEGACY = ('center-messenger-gitverse-runner.service',
          'center-messenger-deploy.service', 'ramessenger-github-deploy.service')
GIB = 1024 ** 3
ITERATIONS = 200000


class BackupError(Exception):
    pass


def now():
    return datetime.now(UTC).isoformat()


def announce(*values):
    print(now(), *values, flush=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def json_write(path, value):
    temporary = path.with_name(path.name + '.writing')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    sync_directory(path.parent)


def private_directory(path):
    if path.is_symlink():
        raise BackupError('Backup directory must not be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid():
        raise BackupError('Unexpected owner of backup directory')
    path.chmod(0o700)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def authentication_key(secret):
    return hmac.new(secret, b'ramessenger-backup-integrity-v1', hashlib.sha256).digest()


def file_digest(path, secret):
    sha = hashlib.sha256()
    mac = hmac.new(authentication_key(secret), path.name.encode() + b'\0', hashlib.sha256)
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
            mac.update(block)
    return {'name': path.name, 'bytes': path.stat().st_size,
            'sha256': sha.hexdigest(), 'hmac_sha256': mac.hexdigest()}


def verify_integrity(directory, key_file):
    secret = key_file.read_bytes().strip()
    raw = (directory / 'manifest.json').read_bytes()
    expected = (directory / 'manifest.hmac').read_text().strip()
    actual = hmac.new(authentication_key(secret), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, actual):
        raise BackupError('Manifest authentication failed')
    manifest = json.loads(raw)
    if manifest.get('format') != 1 or manifest.get('state') != 'complete':
        raise BackupError('Not a completed backup manifest')
    for item in manifest['parts']:
        name = item['name']
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.enc'):
            raise BackupError('Unsafe backup part name')
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise BackupError('Missing backup part: ' + name)
        saved = file_digest(path, secret)
        if any(not hmac.compare_digest(str(saved[k]), str(item[k]))
               for k in ('bytes', 'sha256', 'hmac_sha256')):
            raise BackupError('Backup part authentication failed: ' + name)
    return manifest


def minimal_paths(paths):
    result = []
    for path in sorted(set(Path(p).absolute() for p in paths), key=lambda p: (len(p.parts), str(p))):
        if not path.exists() and not path.is_symlink():
            continue
        if any(parent.is_dir() and not parent.is_symlink() and parent in path.parents for parent in result):
            continue
        result.append(path)
    return result


def archive_command(paths_file, excludes=()):
    command = ['tar', '--create', '--gzip', '--numeric-owner', '--acls', '--xattrs',
               '--sparse', '--file=-', '--directory=/', '--no-wildcards']
    command += ['--exclude=' + str(Path(p)).lstrip('/') for p in excludes]
    return command + ['--null', '--verbatim-files-from', '--files-from=' + str(paths_file)]


def encryption_command(key_file, *, decrypt=False, source=None):
    command = ['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', str(ITERATIONS),
               '-pass', 'file:' + str(key_file)]
    command += ['-d'] if decrypt else ['-salt']
    if source:
        command += ['-in', str(source)]
    return command


class Runner:
    def __init__(self, work):
        self.work = work
        self.index = 0

    def log_file(self):
        self.index += 1
        return (self.work / ('command-%03d.stderr' % self.index)).open('wb')

    def run(self, command, *, timeout=120, check=True):
        with self.log_file() as errors:
            try:
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=errors,
                                        timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                raise BackupError('Command timeout: ' + Path(command[0]).name) from None
        if check and result.returncode:
            raise BackupError('Command failed: ' + Path(command[0]).name + ' exit=' + str(result.returncode))
        return result.stdout.decode(errors='replace').strip()

    def pipeline(self, commands, *, output=None, timeout=3600):
        processes = []
        deadline = time.monotonic() + timeout
        with ExitStack() as stack:
            destination = stack.enter_context(output.open('xb')) if output else subprocess.DEVNULL
            previous = None
            try:
                for index, command in enumerate(commands):
                    errors = stack.enter_context(self.log_file())
                    process = subprocess.Popen(command, stdin=previous,
                        stdout=destination if index == len(commands) - 1 else subprocess.PIPE,
                        stderr=errors, start_new_session=True)
                    if previous:
                        previous.close()
                    previous = process.stdout
                    processes.append(process)
                while any(process.poll() is None for process in processes):
                    if time.monotonic() >= deadline:
                        raise BackupError('Backup stream timeout')
                    # A growing source must not fill the VPS and take PostgreSQL down.
                    if shutil.disk_usage(self.work).free < 2 * GIB:
                        raise BackupError('Backup stopped to preserve 2 GiB free disk space')
                    time.sleep(.1)
                if any(process.returncode for process in processes):
                    raise BackupError('Backup stream failed; inspect private command logs')
                if output:
                    destination.flush()
                    os.fsync(destination.fileno())
            except BaseException:
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
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                raise


class FullBackup:
    def __init__(self, directory, key_file):
        self.directory, self.key_file = directory, key_file
        self.work = directory / 'private-work'
        private_directory(self.work)
        self.runner = Runner(self.work)
        self.parts = []
        self.metadata = {'started_at': now(), 'host': socket.gethostname(), 'scope': 'center-messenger'}
        self.timers = []
        self.api_stopped = False
        self.clone = None
        self.clone_created = False
        self.api_id = None
        self.recovery_state = {'format': 1, 'run_id': directory.name,
                               'api_stop_intent': False, 'timers': []}
        self.record_recovery()
        self.phase('initializing')

    def record_recovery(self):
        self.recovery_state.update(api_stop_intent=self.api_stopped, timers=list(self.timers))
        if self.api_id:
            self.recovery_state.update(api_id=self.api_id, api_image_id=self.api['Image'])
        json_write(self.directory / 'recovery.json', self.recovery_state)

    def phase(self, name, **extra):
        self.state = dict(self.metadata, phase=name, backup_directory=str(self.directory),
                          key_file=str(self.key_file), **extra)
        json_write(self.directory / 'status.json', self.state)
        announce('PHASE', name)

    def active(self, unit):
        return self.runner.run(['systemctl', 'is-active', unit], check=False) == 'active'

    def inspect(self, container):
        return json.loads(self.runner.run(['docker', 'inspect', container]))[0]

    def sql(self, query, *, container=None, database='postgres'):
        return self.runner.run(['docker', 'exec', container or self.db_id, 'psql', '-X', '-q',
            '-v', 'ON_ERROR_STOP=1', '-U', 'cm_owner', '-d', database, '-A', '-t', '-c', query], timeout=300)

    def paths_file(self, name, paths):
        dest = self.work / name
        dest.write_bytes(b''.join(os.fsencode(str(path).lstrip('/')) + b'\0' for path in paths))
        return dest

    def part(self, name, commands):
        self.require_space(2 * GIB)
        temporary = self.directory / (name + '.incomplete')
        destination = self.directory / name
        self.runner.pipeline([*commands, encryption_command(self.key_file)], output=temporary)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        sync_directory(destination.parent)
        self.parts.append(file_digest(destination, self.key_file.read_bytes().strip()))
        announce('SAVED', name, 'bytes=' + str(destination.stat().st_size))
        return destination

    def decrypt(self, name):
        item = next(part for part in self.parts if part['name'] == name)
        saved = file_digest(self.directory / name, self.key_file.read_bytes().strip())
        if saved != item:
            raise BackupError('Encrypted part changed before restore verification')
        return encryption_command(self.key_file, decrypt=True, source=self.directory / name)

    def require_space(self, minimum):
        free = shutil.disk_usage(self.directory).free
        if free < minimum:
            raise BackupError('Insufficient free space: available %.2f GiB; need %.2f GiB' % (free / GIB, minimum / GIB))

    def lock(self, stack, path, timeout=300):
        handle = stack.enter_context(path.open('a+'))
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BackupError('Existing deployment/queue is busy; retry later')
                announce('WAITING_FOR_LOCK', path.name)
                time.sleep(10)

    def prepare(self):
        for command in ('docker', 'systemctl', 'tar', 'gzip', 'openssl', 'git', 'gh', 'du'):
            if not shutil.which(command):
                raise BackupError('Required command not found: ' + command)
        if not ROOT.is_dir() or not (ROOT / 'compose.yaml').is_file():
            raise BackupError('Known messenger deployment directory not found')
        for service in LEGACY:
            if self.active(service):
                raise BackupError('Legacy deployment service is active; inspect it first: ' + service)

    def inventory(self):
        self.phase('inventory')
        names = self.runner.run(['docker', 'ps', '--filter', 'label=com.docker.compose.project=center-messenger',
                                 '--format', '{{.Names}}']).splitlines()
        if set(names) != {API, DB}:
            raise BackupError('Unexpected running project containers; inventory needs review')
        self.api, self.db = self.inspect(API), self.inspect(DB)
        self.api_id, self.db_id = self.api['Id'], self.db['Id']
        self.record_recovery()
        if not self.api['HostConfig'].get('ReadonlyRootfs'):
            raise BackupError('API has a writable container root; include its changes before backup')
        if any(c['State']['Status'] != 'running' for c in (self.api, self.db)):
            raise BackupError('API and PostgreSQL must initially be running')
        env = dict(item.split('=', 1) for item in self.api['Config']['Env'] if '=' in item)
        self.sha = env.get('CM_BUILD_SHA', '')
        if not re.fullmatch('[a-f0-9]{40}', self.sha):
            raise BackupError('Cannot identify exact installed source revision')
        if not self.api['Config']['Image'].endswith(self.sha[:12]):
            raise BackupError('Installed image tag and source revision disagree')
        self.metadata.update(source_sha=self.sha, api_image=self.api['Config']['Image'],
            api_image_id=self.api['Image'], database_image_id=self.db['Image'])
        self.data_directory = self.sql('SHOW data_directory')
        mounts = self.api.get('Mounts', []) + self.db.get('Mounts', [])
        database_mount = next((m for m in self.db['Mounts'] if m['Destination'] == self.data_directory), None)
        if not database_mount:
            raise BackupError('Database storage mount not identified')
        self.database_source = Path(database_mount['Source'])
        if json.loads(self.sql("SELECT coalesce(json_agg(spcname),'[]'::json) FROM pg_tablespace "
            "WHERE spcname NOT IN ('pg_default','pg_global')")):
            raise BackupError('Additional PostgreSQL tablespaces require a reviewed multi-part base backup')
        if self.sql('SELECT pg_is_in_recovery()') != 'f':
            raise BackupError('Database is not the expected primary')
        if self.sql("SELECT count(*) FROM pg_class WHERE relpersistence='u' AND relkind IN ('r','m')") != '0':
            raise BackupError('Unlogged table data requires a separate reviewed restore plan')
        if self.sql("SELECT rolsuper OR rolreplication FROM pg_roles WHERE rolname=current_user") != 't':
            raise BackupError('Physical backup privilege unavailable')
        self.databases = json.loads(self.sql("SELECT json_agg(datname ORDER BY datname) FROM pg_database "
            "WHERE datallowconn AND NOT datistemplate"))
        if 'center_messenger' not in self.databases:
            raise BackupError('Messenger database not found')
        self.db_bytes = int(self.sql('SELECT sum(pg_database_size(oid)) FROM pg_database'))
        self.recovery_settings = json.loads(self.sql("SELECT json_object_agg(name,setting) FROM pg_settings "
            "WHERE name IN ('max_connections','max_worker_processes','max_wal_senders',"
            "'max_prepared_transactions','max_locks_per_transaction')"))
        if any(not re.fullmatch(r'\d+', value) for value in self.recovery_settings.values()):
            raise BackupError('Unexpected PostgreSQL recovery setting')
        memory = Path('/proc/meminfo').read_text()
        available = re.search(r'^MemAvailable:\s+(\d+)\s+kB$', memory, re.MULTILINE)
        if not available or int(available[1]) * 1024 < 768 * 1024 ** 2:
            raise BackupError('Less than 768 MiB available for isolated restore verification')
        source_key = env.get('CM_CONNECTION_MASTER_KEY_FILE')
        if not source_key or not any(m['Destination'] == source_key and Path(m['Source']).is_file()
                                     for m in self.api['Mounts']):
            raise BackupError('Connection encryption key mount not found')
        self.mutable_paths = minimal_paths(m['Source'] for m in mounts if m.get('RW')
            and Path(m['Source']) != self.database_source and m['Type'] in ('bind', 'volume'))
        paths = list(Path('/opt').glob('center-messenger*'))
        paths += [Path(m['Source']) for m in mounts if m['Type'] in ('bind', 'volume')
                  and Path(m['Source']) != self.database_source]
        patterns = ('/etc/systemd/system/*messenger*', '/run/systemd/system/*messenger*',
            '/usr/local/bin/cm-*', '/usr/local/sbin/cm-*',
            '/usr/local/bin/*messenger*', '/usr/local/sbin/*messenger*')
        paths += [Path(p) for pattern in patterns for p in glob.glob(pattern)]
        paths += [Path(p) for p in ('/etc/nginx', '/etc/letsencrypt', '/etc/ssl',
            '/etc/docker', '/etc/sudoers.d/cm-support', '/home/cm-support',
            '/root/.config/gh', '/root/.ssh', '/root/.docker/config.json',
            '/etc/cron.d', '/etc/crontab', '/var/spool/cron/crontabs/root') if Path(p).exists()]
        paths += self.extra_proxy_paths()
        paths += self.extra_unit_paths()
        # Preserve symlinks themselves and include externally stored targets used by deployment.
        paths += [p.resolve() for p in paths if p.is_symlink() and p.exists()]
        self.host_paths = minimal_paths(paths)
        for path in self.host_paths + self.mutable_paths:
            if path == Path('/') or path == self.directory or path in self.directory.parents:
                raise BackupError('Backup destination overlaps a source path')
            if path == self.key_file or path in self.key_file.parents:
                raise BackupError('Backup encryption key would be included in its own backup')
        host_bytes = int(self.runner.run(['du', '--summarize', '--bytes', '--total',
            *map(str, minimal_paths(self.host_paths + self.mutable_paths))], timeout=600).splitlines()[-1].split()[0])
        images = json.loads(self.runner.run(['docker', 'image', 'inspect', self.api['Image'], self.db['Image']]))
        image_bytes = sum(item.get('Size', 0) for item in images)
        self.metadata.update(databases=self.databases, database_bytes=self.db_bytes,
            postgres_recovery_settings=self.recovery_settings,
            host_paths=[str(p) for p in self.host_paths], mutable_paths=[str(p) for p in self.mutable_paths],
            encryption={'cipher': 'AES-256-CBC', 'kdf': 'PBKDF2', 'iterations': ITERATIONS,
                        'authentication': 'HMAC-SHA256 over manifest and encrypted parts'})
        # Worst-case uncompressed source files plus image export, SQL/base backups,
        # one extracted verification cluster and WAL headroom. No old copies deleted.
        needed = host_bytes + image_bytes * 1.1 + self.db_bytes * 3 + 3 * GIB
        self.require_space(int(needed))
        announce('SPACE_OK', 'free_GiB=%.2f' % (shutil.disk_usage(self.directory).free / GIB),
                 'conservative_needed_GiB=%.2f' % (needed / GIB))
        json_write(self.work / 'containers.json', [self.api, self.db])
        json_write(self.work / 'images.json', images)

    def extra_proxy_paths(self):
        if not shutil.which('nginx'):
            return []
        # nginx -T writes expanded configuration to stdout AND stderr; keep both private.
        with (self.work / 'nginx-expanded.txt').open('wb') as dest:
            result = subprocess.run(['nginx', '-T'], stdout=dest, stderr=dest, timeout=30)
        if result.returncode:
            raise BackupError('Cannot inventory existing nginx configuration')
        text = (self.work / 'nginx-expanded.txt').read_text(errors='replace')
        values = re.findall(r'(?m)^\s*(?:ssl_certificate|ssl_certificate_key|ssl_trusted_certificate|ssl_dhparam|auth_basic_user_file)\s+([^;]+);', text)
        paths = [Path(p) for p in re.findall(r'(?m)^# configuration file (/[^\n]+):$', text)]
        for value in values:
            value = value.strip().strip('"').strip("'")
            if '$' in value:
                raise BackupError('Dynamic nginx certificate path needs explicit inventory')
            path = Path(value)
            if not path.is_absolute() or not path.exists():
                raise BackupError('Referenced nginx dependency is not a regular available path')
            paths += [path, path.resolve()]
        return paths

    def extra_unit_paths(self):
        text = self.runner.run(['systemctl', 'cat', '*messenger*'], check=False)
        (self.work / 'systemd-expanded.txt').write_text(text, encoding='utf-8')
        # Do not inventory this transient backup unit's output/key directories as
        # application inputs. Its executable is already in the deployment archive.
        selected, skip = [], False
        for line in text.splitlines():
            if line.startswith('# /'):
                skip = Path(line[2:]).name.startswith('ramessenger-full-backup-')
            if not skip:
                selected.append(line)
        text = '\n'.join(selected)
        paths = [Path(p) for p in re.findall(r'(?m)^# (/[^\n]+)$', text)]
        text = text.replace('\\\n', '')
        for line in text.splitlines():
            if line.startswith('EnvironmentFile='):
                for item in shlex.split(line.split('=', 1)[1]):
                    item = item.removeprefix('-')
                    if '%' in item or '$' in item or not item.startswith('/'):
                        raise BackupError('Dynamic systemd environment path needs explicit inventory')
                    paths += [Path(p) for p in glob.glob(item)]
            elif re.match(r'^Exec(?:Start|StartPre|StartPost|Stop|StopPost|Reload)=', line):
                for item in shlex.split(line.split('=', 1)[1]):
                    candidate = Path(item.lstrip('-+!:@'))
                    if candidate.is_absolute() and candidate.exists() and str(candidate).startswith(('/opt/', '/root/', '/usr/local/')):
                        paths.append(candidate)
        return paths

    def source_bundle(self):
        self.phase('source_repository')
        mirror = self.work / 'source.git'
        self.runner.run(['gh', 'repo', 'clone', REPO, str(mirror), '--', '--mirror'], timeout=600)
        self.runner.run(['git', '-C', str(mirror), 'cat-file', '-e', self.sha + '^{commit}'])
        bundle = self.work / 'source.bundle'
        self.runner.run(['git', '-C', str(mirror), 'bundle', 'create', str(bundle), '--all'], timeout=600)
        self.runner.run(['git', '-C', str(mirror), 'bundle', 'verify', str(bundle)], timeout=600)
        self.part('source.bundle.enc', [['cat', str(bundle)]])
        self.metadata['source_bundle_verified'] = True
        bundle.unlink()
        shutil.rmtree(mirror)

    def host_files(self):
        self.phase('deployment_files')
        # The support dispatcher can enqueue while its worker lock is held.
        # Use SQLite's online backup API instead of a racing db/WAL file copy.
        queue = ROOT / 'support' / 'requests.sqlite'
        excludes = list(self.mutable_paths)
        if queue.exists():
            queue_copy = self.work / 'support-queue.sqlite'
            with sqlite3.connect('file:' + str(queue) + '?mode=ro', uri=True) as source:
                with sqlite3.connect(queue_copy) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                        raise BackupError('Support queue SQLite verification failed')
            self.part('support-queue.sqlite.enc', [['cat', str(queue_copy)]])
            queue_copy.unlink()
            excludes += [queue, Path(str(queue) + '-wal'), Path(str(queue) + '-shm')]
            self.metadata['sqlite_restore_path'] = str(queue)
        paths_file = self.paths_file('host-paths.nul', self.host_paths)
        self.part('deployment-files.tar.gz.enc', [archive_command(paths_file, excludes)])
        self.runner.pipeline([self.decrypt('deployment-files.tar.gz.enc'), ['tar', '-tzf', '-']],
                             timeout=3600)
        self.metadata['deployment_archive_verified'] = True

    def image_files(self):
        self.phase('container_images')
        self.part('container-images.tar.gz.enc', [
            ['docker', 'image', 'save', self.api['Image'], self.db['Image']], ['gzip', '-1']])
        self.runner.pipeline([self.decrypt('container-images.tar.gz.enc'), ['tar', '-tzf', '-']],
                             timeout=3600)
        self.metadata['image_archive_verified'] = True

    def counts(self, *, container=None):
        result = {}
        for database in self.databases:
            tables = json.loads(self.sql("SELECT coalesce(json_agg(json_build_array(nspname,relname)), '[]'::json) "
                "FROM (SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p','m') AND n.nspname NOT LIKE 'pg_%' "
                "AND n.nspname<>'information_schema' ORDER BY n.nspname,c.relname) t",
                container=container, database=database))
            if not tables:
                result[database] = {}
                continue
            queries = []
            for index, (schema, table) in enumerate(tables):
                quote = lambda s: '"' + s.replace('"', '""') + '"'
                queries.append('SELECT %d AS i,count(*) AS n FROM %s.%s' %
                               (index, quote(schema), quote(table)))
            raw = json.loads(self.sql('SELECT json_object_agg(i::text,n) FROM (' +
                ' UNION ALL '.join(queries) + ') q', container=container, database=database))
            result[database] = {json.dumps(pair, ensure_ascii=False): raw[str(i)]
                                for i, pair in enumerate(tables)}
        return result

    def database_files(self):
        self.phase('database_consistency_window')
        if self.inspect(API)['Id'] != self.api_id or self.inspect(DB)['Id'] != self.db_id:
            raise BackupError('Project containers changed during backup')
        self.api_stopped = True  # Set before stop so cleanup also handles a timeout.
        self.record_recovery()
        self.runner.run(['docker', 'stop', '--time', '120', self.api_id], timeout=150)
        if self.inspect(self.api_id)['State']['Running']:
            raise BackupError('API did not stop')
        if self.sql("SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' "
                    "AND pid<>pg_backend_pid() AND datname IS NOT NULL") != '0':
            raise BackupError('Other database clients remain; consistent backup requires review')
        self.metadata['business_snapshot_at'] = now()
        self.source_counts = self.counts()
        self.schema_revision = self.sql('SELECT version_num FROM alembic_version', database='center_messenger')
        self.phase('database_logical_dumps')
        self.part('postgres-globals.sql.enc', [[
            'docker', 'exec', self.db_id, 'pg_dumpall', '-U', 'cm_owner', '--globals-only']])
        self.logical_parts = []
        for index, database in enumerate(self.databases):
            name = 'database-%03d.dump.enc' % index
            self.part(name, [['docker', 'exec', self.db_id, 'pg_dump', '-U', 'cm_owner',
                             '-d', database, '-Fc']])
            self.logical_parts.append({'database': database, 'part': name})
            self.runner.pipeline([self.decrypt(name), ['docker', 'exec', '-i', self.db_id,
                'pg_restore', '--list']], timeout=600)
        self.phase('database_physical_backup')
        # PostgreSQL creates a consistent online base backup INCLUDING recovery WAL.
        # Copying a running PGDATA directory with plain tar is expressly not used.
        self.part('postgres-base.tar.gz.enc', [[
            'docker', 'exec', self.db_id, 'pg_basebackup', '-U', 'cm_owner',
            '-D', '-', '-F', 't', '-X', 'fetch', '--checkpoint=fast',
            '--manifest-checksums=SHA256', '--no-password'], ['gzip', '-1']])
        if self.mutable_paths:
            self.part('persistent-files.tar.gz.enc', [
                archive_command(self.paths_file('persistent-paths.nul', self.mutable_paths))])
            self.runner.pipeline([self.decrypt('persistent-files.tar.gz.enc'), ['tar', '-tzf', '-']])
        if self.counts() != self.source_counts:
            raise BackupError('Business data changed during the consistency window')
        json_write(self.work / 'table-counts.json', self.source_counts)
        self.metadata.update(schema_revision=self.schema_revision, logical_databases=self.logical_parts,
                             logical_dumps_toc_verified=True, business_snapshot_finished_at=now())
        self.resume_api()

    def wait_database(self, container, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.inspect(container)['State']
            if not state['Running']:
                raise BackupError('Verification database stopped unexpectedly')
            ready = self.runner.run(['docker', 'exec', container, 'pg_isready', '-U', 'cm_owner',
                                      '-d', 'postgres'], timeout=10, check=False)
            if 'accepting connections' in ready:
                return
            time.sleep(2)
        raise BackupError('Database readiness timed out')

    def resume_api(self):
        if not self.api_stopped:
            return
        # Restart the exact original container, never create/promote an image.
        self.runner.run(['docker', 'start', self.api_id], timeout=60)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8100/health/ready', timeout=5) as response:
                    if response.status == 200 and json.load(response).get('status') == 'ready':
                        current = self.inspect(API)
                        if current['Id'] != self.api_id or current['Image'] != self.api['Image']:
                            raise BackupError('API identity changed during backup')
                        self.api_stopped = False
                        self.record_recovery()
                        self.metadata['api_resumed_at'] = now()
                        announce('API_READY', self.api['Config']['Image'])
                        return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise BackupError('Original API has not recovered readiness; manual intervention required')

    def verify_database(self):
        self.phase('database_restore_verification')
        self.require_space(self.db_bytes * 2 + 2 * GIB)
        data = self.work / 'restore-data'
        data.mkdir(mode=0o700)
        self.runner.pipeline([self.decrypt('postgres-base.tar.gz.enc'),
            ['tar', '--extract', '--gzip', '--numeric-owner', '--same-owner', '--file=-',
             '--directory=' + str(data)]], timeout=3600)
        run_id = self.directory.name
        self.verifier = 'cm-backup-check-' + run_id[-12:]
        self.clone = 'cm-backup-restore-' + run_id[-12:]
        common = ['--network', 'none', '--memory', '512m', '--cpus', '0.5', '--pids-limit', '128',
                  '--label', 'com.ramessenger.backup=' + run_id]
        self.runner.run(['docker', 'run', '--rm', '--name', self.verifier, *common,
            '--mount', 'type=bind,src=' + str(data) + ',dst=/backup,readonly',
            '--entrypoint', 'pg_verifybackup', self.db['Image'], '/backup'], timeout=1800)
        self.metadata['postgres_manifest_verified'] = True
        # Isolated clone: no published ports, no production network, own copied files.
        recovery_args = [value for name, setting in self.recovery_settings.items()
                         for value in ('-c', name + '=' + setting)]
        self.runner.run(['docker', 'create', '--name', self.clone, *common,
            '--mount', 'type=bind,src=' + str(data) + ',dst=/var/lib/postgresql/data',
            self.db['Image'], 'postgres', '-c', 'listen_addresses=', '-c', 'shared_buffers=32MB',
            *recovery_args, '-c', 'autovacuum=off', '-c', 'jit=off'])
        self.runner.run(['docker', 'start', self.clone])
        self.wait_database(self.clone)
        if self.counts(container=self.clone) != self.source_counts:
            raise BackupError('Restored physical database table counts disagree')
        if self.sql('SELECT version_num FROM alembic_version', container=self.clone,
                    database='center_messenger') != self.schema_revision:
            raise BackupError('Restored schema revision disagrees')
        self.metadata.update(physical_restore_verified=True, all_table_counts_verified=True,
                             restore_verified_at=now())
        self.remove_owned_container(self.clone)
        shutil.rmtree(data)
        announce('DATABASE_RESTORE_VERIFIED', 'all databases and user table counts match')

    def remove_owned_container(self, name):
        result = self.runner.run(['docker', 'inspect', name], check=False)
        try:
            containers = json.loads(result)
        except json.JSONDecodeError:
            return
        if not containers:
            return
        container = containers[0]
        labels = container['Config'].get('Labels') or {}
        if labels.get('com.ramessenger.backup') != self.directory.name:
            raise BackupError('Refusing to remove a container not owned by this backup')
        self.runner.run(['docker', 'rm', '-f', container['Id']], timeout=60)

    def clean_resources(self):
        errors = []
        for name in (getattr(self, 'clone', None), getattr(self, 'verifier', None)):
            if name:
                try:
                    self.remove_owned_container(name)
                except Exception as error:
                    errors.append(type(error).__name__ + ': verification container cleanup failed')
        try:
            self.resume_api()
        except Exception as error:
            errors.append(type(error).__name__ + ': original API recovery failed')
        failed_timers = []
        for timer in reversed(self.timers):
            try:
                self.runner.run(['systemctl', 'start', timer], timeout=60)
            except Exception as error:
                errors.append(type(error).__name__ + ': timer recovery failed: ' + timer)
                failed_timers.append(timer)
        self.timers = failed_timers
        self.record_recovery()
        if errors:
            raise BackupError('; '.join(errors))

    def execute(self):
        self.prepare()
        with ExitStack() as locks:
            self.lock(locks, self.directory.parent / '.full-backup.lock', timeout=1)
            failure = None
            try:
                # Do not stop active deploy services. Wait in their documented lock order.
                for timer in TIMERS:
                    if self.active(timer):
                        self.timers.append(timer)
                        self.record_recovery()
                        self.runner.run(['systemctl', 'stop', timer], timeout=60)
                self.lock(locks, ROOT / 'support' / 'queue.lock')
                self.lock(locks, ROOT / '.deploy.lock')
                self.inventory()
                self.capture_metadata()
                self.source_bundle()
                self.host_files()
                self.image_files()
                self.database_files()
                self.verify_database()
            except BaseException as error:
                failure = error
            try:
                self.clean_resources()
            except Exception as error:
                failure = error if failure is None else BackupError(str(failure) + '; ' + str(error))
            if failure is not None:
                raise failure
            self.finish()

    def capture_metadata(self):
        self.metadata['initially_active_timers'] = list(self.timers)
        for name, command in {
            'units.txt': ['systemctl', 'list-unit-files', '--no-pager', '*messenger*'],
            'timers.txt': ['systemctl', 'list-timers', '--all', '--no-pager', '*messenger*'],
            'docker-version.txt': ['docker', 'version'],
            'docker-info.json': ['docker', 'info', '--format', '{{json .}}'],
            'packages.txt': ['dpkg-query', '-W'],
        }.items():
            (self.work / name).write_text(self.runner.run(command, check=False), encoding='utf-8')
        (self.work / 'systemd-units.txt').write_text(self.runner.run(
            ['systemctl', 'cat', '*messenger*'], check=False), encoding='utf-8')
        for image_name in (API, DB):
            for mount in self.inspect(image_name)['Mounts']:
                if mount['Type'] == 'volume':
                    name = mount.get('Name')
                    if name:
                        (self.work / (image_name + '-volume.json')).write_text(
                            self.runner.run(['docker', 'volume', 'inspect', name]), encoding='utf-8')
        json_write(self.work / 'networks.json', json.loads(self.runner.run(
            ['docker', 'network', 'inspect', *self.api['NetworkSettings']['Networks']])) )

    def finish(self):
        required = ('source_bundle_verified', 'deployment_archive_verified', 'image_archive_verified',
                    'logical_dumps_toc_verified', 'postgres_manifest_verified',
                    'physical_restore_verified', 'all_table_counts_verified')
        if any(self.metadata.get(field) is not True for field in required):
            raise BackupError('Backup is incomplete: required verification did not finish')
        required_parts = {'source.bundle.enc', 'deployment-files.tar.gz.enc',
            'container-images.tar.gz.enc', 'postgres-globals.sql.enc', 'postgres-base.tar.gz.enc'}
        required_parts.update(item['part'] for item in self.metadata.get('logical_databases', []))
        if not required_parts.issubset({part['name'] for part in self.parts}):
            raise BackupError('Backup is incomplete: required archive is missing')
        self.phase('final_integrity_verification')
        self.metadata['finished_at'] = now()
        json_write(self.work / 'recovery-metadata.json', self.metadata)
        # Only closed, already-existing files: do not archive the archive command's
        # own live stderr or a files-from list while it is being created.
        metadata_files = [p for p in self.work.iterdir() if p.is_file()]
        self.part('recovery-metadata.tar.gz.enc', [archive_command(
            self.paths_file('metadata-paths.nul', metadata_files))])
        # Authenticate the exact manifest bytes, then independently re-read every part.
        manifest = dict(self.metadata, format=1, state='complete', parts=self.parts)
        json_write(self.directory / 'manifest.json', manifest)
        secret = self.key_file.read_bytes().strip()
        raw = (self.directory / 'manifest.json').read_bytes()
        (self.directory / 'manifest.hmac').write_text(
            hmac.new(authentication_key(secret), raw, hashlib.sha256).hexdigest() + '\n')
        (self.directory / 'SHA256SUMS').write_text(''.join(
            part['sha256'] + '  ' + part['name'] + '\n' for part in self.parts))
        verify_integrity(self.directory, self.key_file)
        (self.directory / 'RESTORE.txt').write_text(restore_notes(self.key_file), encoding='utf-8')
        shutil.copy2(__file__, self.directory / 'full_backup_vps.py')
        shutil.rmtree(self.work)
        for path in self.directory.iterdir():
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        sync_directory(self.directory)
        self.phase('complete', completed_at=now(), physical_restore_verified=True,
                   all_table_counts_verified=True, parts=len(self.parts))
        announce('BACKUP_COMPLETE', self.directory)
        announce('KEY_FILE', self.key_file)
        announce('Download the backup directory and keep its key separately; do not post either in chat.')


def restore_notes(key_file):
    return '''CENTER MESSENGER RECOVERY SET

Scope: the messenger deployment, all /opt/center-messenger* directories,
mounted persistent files, installed connection encryption keys, relevant
host configuration, exact running application/PostgreSQL images, repository
bundle, PostgreSQL globals and logical databases, and a physical cluster backup.
This is not a disk image of other applications on this VPS.

The backup encryption key is stored separately at:
''' + str(key_file) + '''
It is NOT included in this directory. Preserve it separately. Without this
key the encrypted parts cannot be restored. Do not upload secrets to a chat.

FIRST verify the authenticated manifest and every encrypted part:
  python3 full_backup_vps.py --verify BACKUP_DIRECTORY --key-file KEY_FILE
  cd BACKUP_DIRECTORY && sha256sum -c SHA256SUMS

Decrypt an individual part only into a private recovery environment:
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:KEY_FILE \\
    -in postgres-base.tar.gz.enc | tar -xzf - -C PRIVATE_EMPTY_PGDATA
Use the same decryption command for other parts. Do not extract host paths
over a live server. Review proxy/systemd/credentials before selectively restoring.
When restoring host-file archives into an isolated recovery root, use tar's
--acls --xattrs --xattrs-include='*' --numeric-owner options to preserve metadata.

container-images.tar.gz.enc decrypts to gzip-compressed docker save output:
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:KEY_FILE \\
    -in container-images.tar.gz.enc | gzip -d | docker image load
Image IDs, original tags/digests, Compose files and environment are in the
encrypted recovery metadata and deployment-files archive. Docker image IDs can
have different representations after import across engine/store versions.
Verify image configuration/layers before tagging; review the restored Compose
image references, especially the digest-only PostgreSQL reference.

The physical cluster contains all databases, roles and recovery WAL. Before
starting it use the saved PostgreSQL image/version and validate backup_manifest
with pg_verifybackup. The creator already booted an isolated restored physical
copy and compared every user table count and the messenger schema revision.
Logical dumps are an additional recovery option, with pg_restore --list checked;
their individual full logical restores were not performed by this script.

support-queue.sqlite.enc, if present, is a consistent online SQLite backup.
Its original deployment path is sqlite_restore_path in the manifest. Do not
restore stale WAL/SHM files beside it. Review/disable automatic deploy timers
until recovery is verified, so old queued deployments do not run unexpectedly.

The physical database and business row counts share business_snapshot_at.
Host configuration and repository/images were captured before that window
under the project queue/deployment locks. New production activity after API
restart is intentionally later than the saved snapshot.

Keep an independent downloaded/off-server copy AND its separately stored key.
No automatic retention/deletion or external transfer is performed.
'''


def valid_run_id(value):
    if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{12}', value):
        raise BackupError('Invalid backup run identifier')
    return value


def recover_run(output_root, run_id):
    """Independent systemd ExecStopPost recovery after an error/OOM/termination."""
    valid_run_id(run_id)
    directory = output_root / run_id
    recovery = directory / 'recovery.json'
    if not recovery.exists():
        return
    state = json.loads(recovery.read_text())
    if state.get('run_id') != run_id or state.get('format') != 1:
        raise BackupError('Untrusted recovery state')
    if not state.get('api_stop_intent') and not state.get('timers'):
        return
    work = directory / 'private-work'
    private_directory(work)
    runner = Runner(work)
    with ExitStack() as locks:
        # Same order as the backup/installer. Never bypass another operation's lease.
        for path in (output_root / '.full-backup.lock', ROOT / 'support' / 'queue.lock', ROOT / '.deploy.lock'):
            handle = locks.enter_context(path.open('a+'))
            deadline = time.monotonic() + 120
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BackupError('Post-backup recovery blocked by another operation')
                    time.sleep(2)
        if state.get('api_stop_intent'):
            container = json.loads(runner.run(['docker', 'inspect', API]))[0]
            if (container['Id'] != state.get('api_id')
                    or container['Image'] != state.get('api_image_id')):
                raise BackupError('Post-backup recovery refuses a different API container')
            runner.run(['docker', 'start', container['Id']], timeout=60)
            announce('RECOVERY_API_STARTED', container['Id'][:12])
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8100/health/ready', timeout=5) as response:
                        if response.status == 200 and json.load(response).get('status') == 'ready':
                            state['api_stop_intent'] = False
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(2)
        remaining = []
        for timer in state.get('timers', []):
            if timer not in TIMERS:
                raise BackupError('Unexpected timer in recovery state')
            try:
                runner.run(['systemctl', 'start', timer], timeout=60)
            except BackupError:
                remaining.append(timer)
        state['timers'] = remaining
        json_write(recovery, state)
        if remaining or state.get('api_stop_intent'):
            raise BackupError('Manual API/timer recovery required')
        announce('POST_BACKUP_RECOVERY_DONE')


def start_background(args):
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:12]
    script = Path(__file__).resolve()
    if any(re.search(r'\s', str(p)) for p in (script, args.output_root, args.key_directory)):
        raise BackupError('Background operation requires paths without spaces')
    unit = 'ramessenger-full-backup-' + stamp[-12:]
    log = Path('/var/log/ramessenger-full-backup.log')
    if log.is_symlink():
        raise BackupError('Backup log must not be a symlink')
    with log.open('a'):
        pass
    log.chmod(0o600)
    recover = '/usr/bin/python3 ' + str(script) + ' --recover-run ' + stamp + ' --output-root ' + str(args.output_root)
    subprocess.run(['systemd-run', '--unit=' + unit, '--collect', '--property=Type=exec',
        '--property=RuntimeMaxSec=3h', '--property=TimeoutStopSec=300',
        '--property=StandardOutput=append:' + str(log),
        '--property=StandardError=append:' + str(log),
        '--property=ExecStopPost=' + recover,
        '/usr/bin/python3', str(script), '--run-id', stamp,
        '--output-root', str(args.output_root), '--key-directory', str(args.key_directory)], check=True)
    announce('BACKUP_STARTED', unit + '.service')
    announce('LOG', log)
    announce('EXPECTED_DIRECTORY', args.output_root / stamp)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=Path('/var/backups/ramessenger'))
    parser.add_argument('--key-directory', type=Path, default=Path('/root/ramessenger-backup-keys'))
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--start', action='store_true', help='Run independently in systemd with recovery')
    parser.add_argument('--run-id', type=valid_run_id)
    parser.add_argument('--recover-run', type=valid_run_id)
    args = parser.parse_args(argv)
    if args.verify:
        if not args.key_file:
            parser.error('--verify requires --key-file')
        value = verify_integrity(args.verify, args.key_file)
        announce('INTEGRITY_VERIFIED', 'parts=' + str(len(value['parts'])))
        return 0
    if os.geteuid() != 0:
        parser.error('Run the backup as root')
    os.umask(0o077)
    if args.recover_run:
        recover_run(args.output_root, args.recover_run)
        return 0
    if args.start:
        return start_background(args)
    private_directory(args.output_root)
    private_directory(args.key_directory)
    stamp = args.run_id or datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:12]
    directory = args.output_root / stamp
    directory.mkdir(mode=0o700)
    key_file = args.key_directory / (stamp + '.key')
    with key_file.open('xb') as stream:
        stream.write(os.urandom(48).hex().encode() + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(key_file.parent)
    backup = FullBackup(directory, key_file)
    def interrupted(signum, frame):
        raise BackupError('Backup interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        backup.execute()
        return 0
    except BaseException as error:
        # Only sanitized failures go to the normal log; command stderr stays private.
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        try:
            backup.phase('failed', error=message)
        except OSError:
            announce('STATUS_WRITE_FAILED')
        announce('BACKUP_FAILED', message)
        announce('PRIVATE_DIAGNOSTICS', backup.work)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
