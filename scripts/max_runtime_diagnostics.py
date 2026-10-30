#!/usr/bin/env python3
"""Read-only MAX diagnostics: failed recipient IDs, database waits and safe logs.

Run on the project VPS alongside full_backup_split.py. Never opens CRM, changes
queues, retries messages, alters database settings or restarts a container.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

HELPER = Path(__file__).with_name('full_backup_split.py')
if not HELPER.is_file():
    # The established VPS support command uses a hyphenated filename.
    HELPER = Path(__file__).with_name('full-backup-split.py')
SPEC = importlib.util.spec_from_file_location('cm_backup_audit', HELPER)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

FAILED_SQL = """SELECT coalesce(json_agg(row_to_json(f)), '[]'::json) FROM (
 SELECT s.workspace_id,s.id AS command_id,s.conversation_id,s.last_error_code,
 s.attempts,s.created_at,s.updated_at,c.contact_id,c.crm_link_manual,
 (SELECT count(*) FROM messages m WHERE m.workspace_id=s.workspace_id AND m.send_command_id=s.id) AS saved_messages,
 json_build_object('message_id',incoming.id,'occurred_at',incoming.occurred_at,
   'record_id',incoming.payload->>'crm_record_id',
   'source_branch',incoming.payload->>'branch_id',
   'customer_id',incoming.payload->>'customer_id',
   'customer_branch_id',incoming.payload->>'customer_branch_id',
   'recipient_stale',incoming.payload->>'crm_recipient_stale',
   'has_explicit_peer',incoming.payload->>'max_user_id' IS NOT NULL) AS latest_stored_incoming,
 json_build_object('customer_id',preference.payload->>'customer',
   'branch_id',preference.payload->>'branch','status',preference.payload->>'status',
   'reason',preference.payload->>'reason','checked_at',preference.payload->>'checked_at',
   'publication_matches',preference.payload->>'run'=sc.published_id::text,
   'settings_match',preference.payload->>'version'=a.version::text) AS stored_display_projection,
 (SELECT coalesce(json_agg(row_to_json(cards)), '[]'::json) FROM (
   SELECT ac.external_id AS customer_id,ac.branch_id,ac.status,ac.contact_id,
   ac.contact_id=c.contact_id AS same_contact,
   (SELECT count(*) FROM alfa_memberships mem WHERE mem.workspace_id=ac.workspace_id
     AND mem.run_id=ac.run_id AND mem.branch_id=ac.branch_id AND mem.customer_id=ac.external_id
     AND (mem.b_date IS NULL OR mem.b_date<=(now() AT TIME ZONE w.timezone)::date)
     AND (mem.e_date IS NULL OR mem.e_date>=(now() AT TIME ZONE w.timezone)::date)) AS current_memberships
   FROM alfa_customers ac WHERE ac.workspace_id=c.workspace_id AND ac.run_id=sc.published_id
   AND (ac.contact_id=c.contact_id OR ac.external_id::text=incoming.payload->>'customer_id')
   ORDER BY ac.external_id,ac.branch_id LIMIT 30) cards) AS published_cards
 FROM send_commands s JOIN conversations c ON c.workspace_id=s.workspace_id AND c.id=s.conversation_id
 JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id
 JOIN workspaces w ON w.id=s.workspace_id
 LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id
 LEFT JOIN alfa_sync_control sc ON sc.workspace_id=c.workspace_id AND sc.connection_id=am.crm_connection_id
 LEFT JOIN alfa_settings a ON a.workspace_id=sc.workspace_id AND a.connection_id=sc.connection_id
 LEFT JOIN outbox_events preference ON preference.workspace_id=c.workspace_id
   AND preference.topic='crm.card.preference'
   AND preference.event_key=am.crm_connection_id::text||':'||c.contact_id::text
 LEFT JOIN LATERAL (SELECT m.id,m.occurred_at,m.payload FROM messages m
   WHERE m.workspace_id=c.workspace_id AND m.conversation_id=c.id AND m.direction='in'
   AND m.actor_type='contact' AND coalesce(m.payload->>'deleted','false')<>'true'
   AND m.payload->>'superseded_by' IS NULL AND coalesce(m.payload->>'crm_mirror','false')<>'true'
   ORDER BY m.occurred_at DESC,CASE WHEN m.payload->>'crm_order' ~ '^[0-9]{1,19}$'
     THEN (m.payload->>'crm_order')::numeric ELSE 0 END DESC,m.id DESC LIMIT 1) incoming ON true
 WHERE cn.kind='alfa_max_browser' AND s.state='failed'
 AND s.created_at>=now()-interval '30 days' ORDER BY s.updated_at DESC,s.id LIMIT 20) f"""

DATABASE_SQL = """SELECT json_build_object(
 'checked_at',now(),
 'activity',(SELECT coalesce(json_agg(row_to_json(a)), '[]'::json) FROM (
   SELECT pid,state,wait_event_type,wait_event,
     greatest(0,extract(epoch FROM now()-query_start)) AS query_age_seconds,
     greatest(0,extract(epoch FROM now()-xact_start)) AS transaction_age_seconds,
     CASE WHEN query IS NOT NULL AND query<>'<insufficient privilege>' THEN md5(query) END AS query_fingerprint,
     cardinality(pg_blocking_pids(pid)) AS blocking_backends
   FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
     AND (state='active' OR wait_event_type='Lock' OR state='idle in transaction')
   ORDER BY query_start LIMIT 25) a),
 'tables',(SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM (
   SELECT relname,n_live_tup,n_dead_tup,seq_scan,idx_scan,last_analyze,last_autoanalyze
   FROM pg_stat_user_tables WHERE schemaname='public' AND relname IN
    ('messages','source_records','send_commands','conversations','outbox_events','alfa_max_settings','alfa_max_sync_queue')
   ORDER BY relname) t),
 'role_timeouts',(SELECT coalesce(json_agg(row_to_json(r)), '[]'::json) FROM (
   SELECT cfg AS setting FROM pg_db_role_setting s CROSS JOIN LATERAL unnest(s.setconfig) cfg
   WHERE s.setrole=(SELECT oid FROM pg_roles WHERE rolname='cm_app')
     AND (s.setdatabase=0 OR s.setdatabase=(SELECT oid FROM pg_database WHERE datname=current_database()))
     AND cfg ~ '^(statement_timeout|lock_timeout)=' ORDER BY cfg) r))"""

TABLES = ('messages', 'source_records', 'send_commands', 'conversations', 'outbox_events',
          'alfa_max_settings', 'alfa_max_sync_queue', 'chat_read_versions', 'alfa_customers',
          'alfa_memberships', 'contacts')
REASONS = {'canceling statement due to statement timeout': 'statement_timeout',
           'canceling statement due to user request': 'user_request',
           'canceling statement due to lock timeout': 'lock_timeout'}
LOG_LIMIT = 2 * 1024 * 1024


def log_tail(container, since, *, limit=LOG_LIMIT):
    """Bound both stdout and stderr; never return raw logs to the user."""
    args = ['docker', 'logs', '--timestamps', '--since', since, '--tail', '2000', container]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks, length, truncated = [], 0, False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if not selector.select(min(.2, max(0, deadline-time.monotonic()))):
                continue
            block = os.read(process.stdout.fileno(), min(32768, limit-length+1))
            if not block:
                process.wait(timeout=1)
                return b''.join(chunks).decode(errors='replace'), truncated, process.returncode
            if length + len(block) > limit:
                chunks.append(block[:limit-length])
                truncated = True
                break
            chunks.append(block)
            length += len(block)
        return b''.join(chunks).decode(errors='replace'), True, None
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            # Only the local read-only docker-log client, never a container.
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def summarize_logs(raw, *, postgres=False):
    result = {'cancellations': [], 'history_database_errors': []}
    pending = {}
    for line in raw.splitlines():
        timestamp = re.match(r'^(\d{4}-\d\d-\d\dT[0-9:.]+Z)', line)
        at = timestamp.group(1) if timestamp else None
        if postgres:
            error = re.search(r'\[(\d+)\]\s+ERROR:\s+([^\r\n]+)', line)
            if error and error.group(2).strip() in REASONS:
                item = {'at': at, 'backend_pid': int(error.group(1)),
                        'reason': REASONS[error.group(2).strip()]}
                result['cancellations'].append(item)
                pending[error.group(1)] = item
            statement = re.search(r'\[(\d+)\]\s+STATEMENT:\s+(.+)', line)
            if statement and statement.group(1) in pending:
                item = pending.pop(statement.group(1))
                query = statement.group(2)
                item['statement_fingerprint'] = hashlib.sha256(query.encode()).hexdigest()
                verb = re.match(r'\s*(SELECT|UPDATE|INSERT|DELETE|WITH)\b', query, re.I)
                item['statement_kind'] = verb.group(1).upper() if verb else 'other'
                item['tables'] = [table for table in TABLES if re.search(r'\b' + table + r'\b', query, re.I)]
        else:
            error = re.search(r'Database operation failed operation=alfa_max_sync:([a-z_]+) '
                              r'exception=([A-Za-z]+) driver=([A-Za-z]+) sqlstate=([A-Z0-9]{5})\b', line)
            if error:
                result['history_database_errors'].append(dict(zip(
                    ('phase', 'exception', 'driver', 'sqlstate'), error.groups()), at=at))
    # Keep aggregates and a bounded recent sample. No SQL, text or repr(error).
    return {'cancellation_count': len(result['cancellations']),
            'cancellations': result['cancellations'][-20:],
            'history_database_error_count': len(result['history_database_errors']),
            'history_database_errors': result['history_database_errors'][-20:]}


def baseline(report):
    return {'version': 1, 'checked_at': report['checked_at'],
            'containers': [{key: c.get(key) for key in ('name', 'id', 'image_id', 'image', 'started_at', 'restarts')}
                           for c in report['containers']],
            'connections': [{key: c.get(key) for key in ('workspace_id', 'last_sync_at')}
                            for c in report['database']['connections']]}


def compare(report, previous):
    if previous.get('version') != 1:
        raise backup.BackupError('Unsupported diagnostics baseline')
    before = {c['name']: c for c in previous['containers']}
    after = {c['name']: c for c in report['containers']}
    keys = ('id', 'image_id', 'image', 'started_at', 'restarts')
    unchanged = all(before[backup.MAX].get(k) == after[backup.MAX].get(k) for k in keys)
    if not unchanged:
        report['issues'].append('MAX runtime changed since the recorded baseline; investigate the release')
    old_sync = {c['workspace_id']: c.get('last_sync_at') for c in previous['connections']}
    progressed = []
    for c in report['database']['connections']:
        old, new = old_sync.get(c['workspace_id']), c.get('last_sync_at')
        advanced = bool(old and new and datetime.fromisoformat(new) > datetime.fromisoformat(old))
        progressed.append({'workspace_id': c['workspace_id'], 'successful_read_advanced': advanced})
    return {'baseline_at': previous['checked_at'], 'max_unchanged': unchanged,
            'api_replaced': before[backup.API]['id'] != after[backup.API]['id'],
            'history': progressed}


def collect(runner, *, since='1h', expected_worker=None):
    report = backup.audit(runner)
    worker = next(c for c in report['containers'] if c['name'] == backup.MAX)
    if expected_worker and worker['id'] != expected_worker:
        report['issues'].append('MAX container ID differs from the expected verified runtime')
    db_id = next(c['id'] for c in report['containers'] if c['name'] == backup.DB)
    report['failed_commands'] = json.loads(backup.read_sql(runner, db_id, FAILED_SQL))
    report['database_diagnostics'] = json.loads(backup.read_sql(runner, db_id, DATABASE_SQL))
    report['configured_worker_statement_timeout_ms'] = env_timeout(runner)
    report['log_window'] = {'since': since, 'tail_lines': 2000, 'max_bytes_per_container': LOG_LIMIT}
    report['log_summaries'] = {}
    for container in (backup.DB, backup.MAX):
        raw, truncated, code = log_tail(container, since)
        report['log_summaries'][container] = dict(summarize_logs(raw, postgres=container == backup.DB),
                                                truncated=truncated, read_ok=code == 0)
    final_worker = backup.safe_container(backup.inspect(runner, backup.MAX))
    if any(final_worker.get(k) != worker.get(k) for k in ('id', 'image_id', 'started_at', 'restarts')):
        report['issues'].append('MAX runtime changed while diagnostics were being collected; repeat the read')
    report['limits'] += [
        'Failed commands: latest 20 from 30 days; no retry or CRM verification was performed',
        'Stored display projection is a diagnostic hint, not an approved send recipient',
        'Database waits are a current snapshot; historical cancellations may be outside the bounded log sample',
        'PostgreSQL cancellations are not automatically attributed to MAX by time alone',
        'The configured worker timeout is read from container configuration, not from an existing DB session']
    return report


def env_timeout(runner):
    env = backup.env_of(backup.inspect(runner, backup.MAX))
    value = env.get('CM_DB_STATEMENT_TIMEOUT_MS', '2500')
    return int(value) if re.fullmatch('[0-9]{1,8}', value) else None


def since_value(value):
    if re.fullmatch(r'[1-9][0-9]{0,3}[smh]', value):
        return value
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('Use 1h, 30m or an ISO timestamp with timezone')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default='1h', type=since_value)
    parser.add_argument('--expected-worker-id', type=lambda s: s if re.fullmatch('[a-f0-9]{64}', s)
                        else parser.error('Expected worker ID must be the full Docker ID'))
    snapshots = parser.add_mutually_exclusive_group()
    snapshots.add_argument('--record-baseline', type=Path)
    snapshots.add_argument('--compare-baseline', type=Path)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise backup.BackupError('Root required for project container and PostgreSQL diagnostics')
    report = collect(backup.Runner(), since=args.since, expected_worker=args.expected_worker_id)
    if args.compare_baseline:
        report['baseline_comparison'] = compare(report, json.loads(args.compare_baseline.read_text()))
    if args.record_baseline:
        if report['issues']:
            raise backup.BackupError('Cannot record a healthy runtime baseline while audit reports issues')
        # No overwrites, symlink following, directory changes or production writes.
        fd = os.open(args.record_baseline, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(baseline(report), stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report['issues'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        backup.announce('DIAGNOSTICS_FAILED', str(error) if isinstance(error, backup.BackupError) else type(error).__name__)
        sys.exit(1)
