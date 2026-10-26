"""Fenced native-browser IO and bounded server-side authorization recovery.

Recovery credentials live INSIDE the existing AEAD browser-state envelope.
Only non-secret coordination metadata lives in the cursor. No schema change.
"""
import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from center_messenger.accounts import audit
from center_messenger.auth_security import AuthError
from center_messenger.alfa_max_browser import AlfaBrowserError
from center_messenger.alfa_history import ready_history
from center_messenger import native_priority as priority

CONTROL = 'session_control'
SECRET = '_cm_login'


def merge_state(previous, renewed):
    result = {key: renewed.get(key, []) for key in ('cookies', 'origins')}
    if isinstance(previous, dict) and isinstance(previous.get(SECRET), dict):
        result[SECRET] = dict(previous[SECRET])
    return result


def same_session(current, snapshot):
    return bool(current and snapshot and current['connection_id'] == snapshot['connection_id']
                and current['version'] == snapshot['version']
                and current['session_ciphertext'] == snapshot['session_ciphertext']
                and all(current.get(key) == snapshot.get(key) for key in (
                    'crm_connection_id', 'current_crm_connection_id', 'crm_origin', 'crm_settings_version')))


def control(row):
    value = (row.get('history_cursor') or {}).get(CONTROL) or {}
    return dict(value) if isinstance(value, dict) else {}


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else datetime.min.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)


def event(meta, name, *, row, state=None, source=None, cause=None, new_generation=False):
    """Bounded non-secret transition history; never copy page/exception text."""
    item = {'at': datetime.now(UTC).isoformat(), 'event': name, 'from': row['state'],
            'state': state or row['state'], 'generation': row['version'],
            'generation_after': row['version'] + int(new_generation),
            'source': source or meta.get('source'), 'cause': cause}
    meta.update(at=item['at'], event=name)
    meta['recent'] = [*meta.get('recent', [])[-9:], item]


class NativeSessions:
    def __init__(self, service):
        self.service = service

    def _row(self, db, workspace):
        return self.service._row(db, workspace)

    @contextmanager
    def operation(self, workspace, connection, snapshot=None, *, administrative=False, purpose=None):
        try:
            lease = self.claim(workspace, connection, snapshot, purpose=purpose)
        except AlfaBrowserError:
            if administrative:
                raise AuthError(409, 'Браузер AlfaCRM занят или подключение изменилось. Повторите действие через несколько секунд.') from None
            raise
        try:
            yield lease
        except AlfaBrowserError as error:
            if administrative and error.code in {'settings_changed', 'browser_busy'}:
                raise AuthError(409, 'Подключение изменилось. Повторите действие с актуальными настройками.') from None
            raise
        finally:
            self.release(lease)

    @staticmethod
    def _write_control(db, workspace, value):
        db.execute(text("UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,"
                        "'{session_control}',CAST(:value AS jsonb)) WHERE workspace_id=:w"),
                   {'w': workspace, 'value': json.dumps(value)})

    def _ready(self, db, row, now):
        config = getattr(self.service.accounts, 'settings', None)
        ready = ready_history(row.get('history_cursor') or {}, now) if getattr(config, 'crm_sync_worker', True) else {}
        if getattr(config, 'max_worker', True):
            due = db.execute(text("SELECT min(j.due_at) FROM message_jobs j JOIN send_commands s "
                "ON s.workspace_id=j.workspace_id AND s.id=j.subject_id "
                "WHERE j.workspace_id=:w AND j.connection_id=:c AND j.kind='send' "
                "AND j.channel_kind='alfa_max_browser' AND s.state='queued' "
                "AND j.due_at<=clock_timestamp()+interval '0.25 seconds'"),
                {'w': row['workspace_id'], 'c': row['connection_id']}).scalar_one()
            if due is not None:
                ready['send'] = due
        return ready

    def claim(self, workspace, connection, snapshot=None, *, purpose=None):
        deferred = False
        with self.service.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w': workspace})
            row = self._row(db, workspace)
            if not row or row['connection_id'] != connection or (snapshot and not same_session(row, snapshot)):
                raise AlfaBrowserError('settings_changed')
            meta, now = control(row), datetime.now(UTC)
            if meta.get('lease') and stamp(meta.get('until')) > now:
                raise AlfaBrowserError('browser_busy')
            kind = purpose
            if purpose in priority.KINDS | {'history'}:
                ready = self._ready(db, row, now)
                # A sender reaching this point has already claimed its durable job.
                if purpose == 'send':
                    ready.setdefault('send', now)
                dispatch = dict(meta.get('dispatch') or {})
                preferred = priority.choose(ready, dispatch, now)
                requested = ({'live', 'catchup', 'archive'} if purpose == 'history' else {purpose}) & ready.keys()
                reservation = dispatch.get('reservation') or {}
                if preferred not in requested:
                    # Give the preferred worker a bounded exclusive handoff. If it
                    # died or is delayed elsewhere, do not block all other work.
                    expired = reservation.get('kind') == preferred and stamp(reservation.get('until')) <= now
                    if expired and requested:
                        kind = priority.choose({k: ready[k] for k in requested}, dispatch, now)
                    else:
                        if reservation.get('kind') != preferred:
                            dispatch = priority.reserve(dispatch, preferred, now)
                        meta['dispatch'] = dispatch
                        deferred = True
                else:
                    kind = preferred
                if not deferred:
                    dispatch.pop('reservation', None)
                    meta['dispatch'] = priority.record_turn(dispatch, kind) if kind == 'send' else dispatch
            token = str(uuid4())
            if not deferred:
                meta.update(lease=token, until=(now + timedelta(seconds=180)).isoformat())
            self._write_control(db, workspace, meta)
        # Commit the handoff reservation before reporting a benign queue wait.
        if deferred:
            raise AlfaBrowserError('browser_priority_wait')
        return {'workspace': workspace, 'connection': connection, 'token': token, 'row': dict(row), 'kind': kind}

    def can_read(self, db, lease, kind):
        self.renew(db, lease)
        if not lease.get('pages') and kind == lease.get('kind'):
            return True
        row = self._row(db, lease['workspace'])
        now = datetime.now(UTC)
        return priority.choose(self._ready(db, row, now), control(row).get('dispatch') or {}, now) == kind

    @staticmethod
    def page_finished(cursor, kind):
        meta = dict(cursor.get(CONTROL) or {})
        meta['dispatch'] = priority.record_turn(meta.get('dispatch') or {}, kind)
        cursor[CONTROL] = meta

    def renew(self, db, lease, *, force=False):
        db.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w': lease['workspace']})
        current = self._row(db, lease['workspace'])
        meta = control(current) if current else {}
        if not same_session(current, lease['row']) or meta.get('lease') != lease['token']:
            raise AlfaBrowserError('settings_changed')
        now = datetime.now(UTC)
        if stamp(meta.get('until')) <= now:
            raise AlfaBrowserError('settings_changed')
        if not force and stamp(meta.get('until')) > now + timedelta(seconds=90):
            return
        changed = db.execute(text("UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,"
            "'{session_control,until}',CAST(:until AS jsonb)) WHERE workspace_id=:w AND connection_id=:c "
            "AND version=:v AND session_ciphertext IS NOT DISTINCT FROM :s "
            "AND history_cursor#>>'{session_control,lease}'=:token RETURNING connection_id"),
            {'w': lease['workspace'], 'c': lease['connection'], 'v': current['version'],
             's': current['session_ciphertext'], 'token': lease['token'],
             'until': json.dumps((now + timedelta(seconds=180)).isoformat())}).scalar_one_or_none()
        if changed is None:
            raise AlfaBrowserError('settings_changed')

    def release(self, lease):
        if not lease:
            return
        try:
            self._release(lease)
        except SQLAlchemyError:
            # TTL releases a stranded lease. Cleanup must not replace a known
            # accepted send with an unknown result or trigger its replay.
            logging.getLogger(__name__).warning('Native CRM lease cleanup deferred workspace=%s connection=%s',
                                                lease['workspace'], lease['connection'])

    def _release(self, lease):
        with self.service.accounts.transaction(workspace_id=lease['workspace']) as db:
            db.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w': lease['workspace']})
            row = self._row(db, lease['workspace'])
            if not row or row['connection_id'] != lease['connection'] or control(row).get('lease') != lease['token']:
                return
            meta, now = control(row), datetime.now(UTC)
            dispatch = dict(meta.get('dispatch') or {})
            ready = self._ready(db, row, now)
            meta['dispatch'] = priority.reserve(dispatch, priority.choose(ready, dispatch, now), now)
            self._write_control(db, lease['workspace'], meta)
            db.execute(text("UPDATE alfa_max_settings SET history_cursor=history_cursor "
                            "#- '{session_control,lease}' #- '{session_control,until}' "
                            "WHERE workspace_id=:w AND connection_id=:c "
                            "AND history_cursor#>>'{session_control,lease}'=:token"),
                       {'w': lease['workspace'], 'c': lease['connection'], 'token': lease['token']})

            db.execute(text("UPDATE alfa_max_settings SET history_cursor=history_cursor-'session_control' "
                            "WHERE workspace_id=:w AND connection_id=:c AND history_cursor->'session_control'='{}'::jsonb"),
                       {'w': lease['workspace'], 'c': lease['connection']})
            # A released browser wakes queue waiters; network/auth backoffs stay intact.
            db.execute(text("UPDATE message_jobs j SET due_at=LEAST(j.due_at,clock_timestamp()) "
                "FROM send_commands s WHERE j.workspace_id=:w AND j.connection_id=:c "
                "AND j.kind='send' AND j.channel_kind='alfa_max_browser' "
                "AND j.lease_owner IS NULL AND s.workspace_id=j.workspace_id AND s.id=j.subject_id "
                "AND s.state='queued' AND s.last_error_code IN ('browser_busy','browser_priority_wait')"),
                {'w': lease['workspace'], 'c': lease['connection']})
            if any(k != 'send' for k in ready):
                db.execute(text("UPDATE alfa_max_sync_queue SET due_at=LEAST(due_at,clock_timestamp()) "
                    "WHERE workspace_id=:w AND connection_id=:c AND lease_owner IS NULL "
                    "AND due_at<=clock_timestamp()+interval '0.25 seconds'"),
                    {'w': lease['workspace'], 'c': lease['connection']})

    def suspect(self, db, workspace, snapshot, source):
        db.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w': workspace})
        current = self._row(db, workspace)
        if not same_session(current, snapshot) or current['state'] != 'active':
            return False
        meta = control(current)
        meta.update(needs_check=True, phase='checking', source=source, after=None)
        event(meta, 'authorization_check_requested', row=current, source=source)
        self._write_control(db, workspace, meta)
        self.service._schedule(db, workspace, current['connection_id'])
        audit(db, 'alfa_max_session_check_requested', workspace_id=workspace, subject_id=current['connection_id'])
        return True

    def finish(self, lease, browser_state, *, state, code=None, phase='active', after=None,
               attempts=0, new_generation=False, needs_check=False, cause=None):
        workspace = lease['workspace']
        with self.service.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w': workspace})
            self.renew(db, lease)
            row = self._row(db, workspace)
            previous = self.service.vault.open_browser_state(workspace, lease['connection'], row['session_ciphertext'])
            encrypted = self.service.vault.seal_browser_state(workspace, lease['connection'], merge_state(previous, browser_state))
            meta = control(row)
            transition = (row['state'] != state or meta.get('phase') != phase or code or cause or new_generation)
            meta.update(needs_check=needs_check, phase=phase, after=after, attempts=attempts, cause=cause)
            if transition:
                event(meta, code or phase, row=row, state=state, cause=cause, new_generation=new_generation)
            self._write_control(db, workspace, meta)
            if code is None and state == 'active' and (row.get('last_error_code') or '').startswith('history_'):
                code = row['last_error_code']
            changed = db.execute(text(
                "UPDATE alfa_max_settings SET state=:state,session_ciphertext=:s,last_error_code=:code,"
                "version=version+:bump,updated_at=now() WHERE workspace_id=:w AND connection_id=:c "
                "AND version=:v AND session_ciphertext=:old RETURNING version"
            ), {'w': workspace, 'c': lease['connection'], 'state': state, 's': encrypted, 'code': code,
                'v': row['version'], 'old': row['session_ciphertext'], 'bump': int(new_generation)}).scalar_one_or_none()
            if changed is None:
                raise AlfaBrowserError('settings_changed')
            db.execute(text('UPDATE connections SET status=:s WHERE workspace_id=:w AND id=:c'),
                       {'w': workspace, 'c': lease['connection'], 's': 'active' if state == 'active' else 'connecting' if state == 'otp_required' else 'reauth_required'})
            lease['row'].update(version=changed, session_ciphertext=encrypted, state=state,
                                history_cursor=dict(row.get('history_cursor') or {}) | {CONTROL: meta})
            if transition:
                audit(db, 'alfa_max_session_' + phase, workspace_id=workspace, subject_id=lease['connection'])
            logging.getLogger(__name__).info('Native CRM session transition workspace=%s connection=%s phase=%s generation=%s code=%s',
                workspace, lease['connection'], phase, changed, code)

    def recover(self, workspace, connection):
        """One recovery attempt. Queue delay bounds retries across restarts."""
        lease = self.claim(workspace, connection)
        confirmed_expired = False
        stage = 'check_session'
        try:
            row, now = lease['row'], datetime.now(UTC)
            meta = control(row)
            if row['state'] == 'not_configured' or row['state'] == 'otp_required':
                return 300
            if meta.get('phase') in {'credentials_required', 'credentials_rejected', 'confirmation_required', 'automatic_disabled'}:
                return 300
            if stamp(meta.get('after')) > now:
                return max(1, int((stamp(meta['after']) - now).total_seconds()))
            state = self.service.vault.open_browser_state(workspace, connection, row['session_ciphertext'])
            with self.service.accounts.transaction(workspace_id=workspace) as db:
                branches = db.execute(text('SELECT external_id FROM alfa_branches WHERE workspace_id=:w '
                    'AND connection_id=:c AND available ORDER BY external_id'),
                    {'w': workspace, 'c': row['crm_connection_id']}).scalars().all()
            if not branches:
                return 300
            def check_available(browser_state):
                # A branch-level permission denial does not prove logout.
                for branch in branches:
                    with self.service.accounts.transaction(workspace_id=workspace) as db:
                        self.renew(db, lease)
                    try:
                        return self.service.browser.check_session(row['crm_origin'], browser_state, branch)
                    except AlfaBrowserError as error:
                        if error.code != 'crm_access_denied':
                            raise
                raise AlfaBrowserError('crm_access_denied')
            # A stale/auth-looking response must never trigger a new login by itself.
            try:
                checked = check_available(state)
                self.finish(lease, checked, state='active', new_generation=True)
                return 0
            except AlfaBrowserError as error:
                if error.code != 'reauth_required':
                    raise
                confirmed_expired = True
            credentials = state.get(SECRET) or {}
            if not credentials.get('enabled') or not credentials.get('password'):
                disabled = meta.get('enabled') is False
                self.finish(lease, state, state='reauth_required',
                            code='recovery_disabled' if disabled else 'recovery_credentials_required',
                            phase='automatic_disabled' if disabled else 'credentials_required')
                return 300
            with self.service.accounts.transaction(workspace_id=workspace) as db:
                self.renew(db, lease)
                current = self._row(db, workspace)
                value = control(current)
                value.update(phase='reconnecting')
                event(value, 'automatic_login_started', row=current)
                self._write_control(db, workspace, value)
                audit(db, 'alfa_max_session_login_started', workspace_id=workspace, subject_id=connection)
            stage = 'login'
            status, renewed = self.service.browser.login(row['crm_origin'], credentials['email'], credentials['password'])
            if status == 'active':
                # Save the new cookies BEFORE the confirmation read. If that
                # read times out, retry these cookies instead of another login.
                self.finish(lease, renewed, state='active', phase='checking', needs_check=True, new_generation=True)
                confirmed_expired = False
                stage = 'verify_login'
                renewed = check_available(renewed)
            self.finish(lease, renewed, state=status, code='otp_required' if status == 'otp_required' else None,
                        phase='confirmation_required' if status == 'otp_required' else 'active', new_generation=status != 'active')
            return 300 if status == 'otp_required' else 0
        except AlfaBrowserError as error:
            if error.code in {'settings_changed', 'browser_busy'}:
                return 10
            logging.getLogger(__name__).warning('Native CRM recovery failed workspace=%s connection=%s stage=%s code=%s', workspace, connection, stage, error.code)
            row = lease['row']
            # A local IO error preserves the encrypted state and credentials.
            state = self.service.vault.open_browser_state(workspace, connection, row['session_ciphertext'])
            partial = getattr(error, 'browser_state', None)
            if isinstance(partial, dict) and isinstance(partial.get('cookies'), list):
                state = merge_state(state, partial)
            if error.code == 'otp_required':
                self.finish(lease, state, state='otp_required', code='otp_required', phase='confirmation_required')
                return 300
            attempts = int(control(row).get('attempts', 0)) + 1
            blocked = error.code in {'credentials_rejected', 'otp_rejected', 'login_form_unknown', 'otp_unavailable'}
            delay = (60, 300, 900)[min(attempts - 1, 2)]
            code = error.code if blocked else 'recovery_retry'
            self.finish(lease, state, state='reauth_required' if confirmed_expired or row['state'] == 'reauth_required' else 'active', code=code,
                        phase='credentials_rejected' if blocked else 'waiting', attempts=attempts,
                        after=None if blocked else (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                        needs_check=not blocked, cause=error.code)
            return delay
        finally:
            self.release(lease)


class CoordinatedHistoryReader:
    """Operational audits share the same lease/cookie fencing as live workers."""
    def __init__(self, service, workspace, connection):
        self.service, self.workspace, self.connection = service, workspace, connection

    def read_histories(self, origin, state, requests, **options):
        sessions = self.service.sessions
        with sessions.operation(self.workspace, self.connection) as operation:
            row = operation['row']
            if row['state'] != 'active' or control(row).get('needs_check'):
                raise AlfaBrowserError('browser_busy')
            if row.get('crm_origin') != origin:
                raise AlfaBrowserError('settings_changed')
            fresh = self.service.vault.open_browser_state(self.workspace, self.connection, row['session_ciphertext'])
            on_progress, on_result = options.pop('on_progress', None), options.pop('on_result', None)
            def heartbeat():
                with self.service.accounts.transaction(workspace_id=self.workspace) as db:
                    sessions.renew(db, operation)
                if on_progress:
                    on_progress()
            def save(request, result, renewed):
                if not result.get('error') and not result.get('skipped'):
                    sessions.finish(operation, renewed, state='active')
                if on_result:
                    on_result(request, result, renewed)
            results, renewed = self.service.browser.read_histories(origin, fresh, requests,
                on_progress=heartbeat, on_result=save, **options)
            return results, merge_state(fresh, renewed)
