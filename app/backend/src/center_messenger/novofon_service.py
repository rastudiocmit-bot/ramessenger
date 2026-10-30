"""Novofon setup: tenant permissions, encrypted key and fenced diagnostics."""
import json
import logging
from datetime import datetime, timedelta, timezone
from threading import BoundedSemaphore
from uuid import uuid4

from sqlalchemy import text

from center_messenger.accounts import audit as account_audit
from center_messenger.auth_security import AuthError
from center_messenger.novofon_client import Client, audit

LOG = logging.getLogger(__name__)
CHECK_SLOTS = BoundedSemaphore(2)
ERROR_TEXT = {
    'api_http_error': 'Новофон отклонил запрос. Проверьте ключ, права API и разрешённый IP сервера.',
    'api_rejected': 'Новофон отклонил запрос. Проверьте ключ, права API и разрешённый IP сервера.',
    'api_network_error': 'Не удалось связаться с Новофоном. Повторите проверку позже.',
    'quota_exhausted': 'Лимит запросов Новофона исчерпан. Повторите проверку после его восстановления.',
    'account_timezone_invalid': 'Новофон вернул неизвестный часовой пояс аккаунта.',
    'media_is_not_audio': 'Вместо аудио получена страница входа или другой документ.',
    'media_http_error': 'Запись пока недоступна или требует дополнительных прав.',
    'media_network_error': 'Не удалось получить фрагмент записи. Попробуйте позже.',
    'media_url_rejected': 'Адрес записи не относится к поддерживаемым адресам Новофона.',
}


def public_report(report):
    """Persist/display only counts and fixed statuses, never a list of contacts."""
    result = {}
    for name in ('account', 'journal', 'sip_lines', 'audio'):
        item = report.get('checks', {}).get(name)
        if not isinstance(item, dict):
            continue
        allowed = ('status', 'code', 'http_status', 'rpc_code', 'sampled', 'direction_counts',
                   'missed_in_sample', 'transferred_in_sample', 'recordings_in_sample',
                   'outgoing_capable', 'account_id', 'timezone', 'container', 'bytes_checked',
                   'full_audio_validated', 'asr_tested', 'browser_playback_tested')
        result[name] = {k: item[k] for k in allowed if k in item}
        if item.get('status') == 'error':
            result[name]['message'] = ERROR_TEXT.get(item.get('code'), 'Проверка не завершена. Повторите её позже.')
    return result


class Novofon:
    def __init__(self, accounts, vault, client_factory=Client):
        self.accounts, self.vault, self.client_factory = accounts, vault, client_factory

    @staticmethod
    def _row(db, workspace, lock=False):
        return db.execute(text('SELECT * FROM novofon_settings WHERE workspace_id=:w' +
                               (' FOR UPDATE' if lock else '')), {'w': workspace}).mappings().one_or_none()

    def _public(self, row):
        now = datetime.now(timezone.utc)
        configured = bool(row and row['key_ciphertext'])
        checking = bool(row and row['check_until'] and row['check_until'] > now)
        status = row['status'] if row and configured else 'not_configured'
        if status == 'checking' and not checking:
            status = 'saved'
        return {'configured': configured, 'vault_ready': self.vault.ready,
                'version': row['version'] if row else 0,
                'sample_hours': row['sample_hours'] if row else 24,
                'status': status, 'checking': checking,
                'checked_at': row['checked_at'] if row else None,
                'checks': dict(row['report'] or {}) if row else {},
                'browser_call_ready': False,
                'browser_call_note': 'Разговор через микрофон будет подключён отдельным этапом.'}

    def settings(self, actor, workspace):
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            return self._public(self._row(db, workspace))

    def save(self, actor, workspace, data):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            row = self._row(db, workspace, True)
            if (row['version'] if row else 0) != data['expected_version']:
                raise AuthError(409, 'Настройки изменились. Обновите страницу перед сохранением.')
            key = (data.get('api_key') or '').strip()
            if key and (not 16 <= len(key) <= 4096 or any(c.isspace() or ord(c) < 32 for c in key)):
                raise AuthError(422, 'Вставьте полный Secret API Новофона без кавычек и пробелов.')
            connection = row['connection_id'] if row else uuid4()
            cipher = row['key_ciphertext'] if row else None
            if key:
                cipher = self.vault.seal(workspace, connection, {'api_key': key}, purpose='novofon')
            if not cipher:
                raise AuthError(422, 'Введите Secret API Новофона.')
            if row:
                db.execute(text("UPDATE novofon_settings SET key_ciphertext=:c,sample_hours=:h,version=version+1,"
                                "status='saved',checked_at=NULL,check_token=NULL,check_until=NULL,report='{}'::jsonb,"
                                "updated_at=now(),updated_by=:u WHERE workspace_id=:w"),
                           {'w': workspace, 'c': cipher, 'h': data['sample_hours'], 'u': actor.user_id})
            else:
                inserted = db.execute(text("INSERT INTO novofon_settings(workspace_id,connection_id,key_ciphertext,sample_hours,updated_by) "
                                "VALUES (:w,:id,:c,:h,:u) ON CONFLICT(workspace_id) DO NOTHING RETURNING version"),
                           {'w': workspace, 'id': connection, 'c': cipher, 'h': data['sample_hours'], 'u': actor.user_id}).mappings().one_or_none()
                if inserted is None:
                    raise AuthError(409, 'Подключение уже настроил другой администратор. Обновите настройки.')
            account_audit(db, 'novofon_settings_saved', user_id=actor.user_id, workspace_id=workspace)
            return self._public(self._row(db, workspace))

    def disconnect(self, actor, workspace, expected_version):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            row = self._row(db, workspace, True)
            if (row['version'] if row else 0) != expected_version:
                raise AuthError(409, 'Настройки изменились. Обновите страницу.')
            if row:
                db.execute(text("UPDATE novofon_settings SET key_ciphertext=NULL,version=version+1,status='not_configured',"
                                "check_token=NULL,check_until=NULL,checked_at=NULL,report='{}'::jsonb,"
                                "updated_at=now(),updated_by=:u WHERE workspace_id=:w"),
                           {'w': workspace, 'u': actor.user_id})
                account_audit(db, 'novofon_disconnected', user_id=actor.user_id, workspace_id=workspace)
            return self._public(self._row(db, workspace))

    def verify(self, actor, workspace, expected_version, probe_recording=False):
        # Check permission before consuming the actor's diagnostic budget.
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
        self.accounts.throttle('novofon-check:' + str(workspace), limit=4, minutes=1)
        if not CHECK_SLOTS.acquire(blocking=False):
            raise AuthError(429, 'Проверки подключений заняты. Повторите немного позже.', retry_after=10)
        token = uuid4()
        try:
            with self.accounts.transaction(actor) as db:
                self.accounts._workspace(db, actor, workspace, manage=True)
                row = self._row(db, workspace, True)
                if not row or not row['key_ciphertext']:
                    raise AuthError(422, 'Сначала сохраните API-ключ Новофона.')
                if row['version'] != expected_version:
                    raise AuthError(409, 'Ключ изменился. Обновите настройки и повторите проверку.')
                now = datetime.now(timezone.utc)
                if row['check_until'] and row['check_until'] > now:
                    raise AuthError(409, 'Проверка этого подключения уже выполняется.')
                key = self.vault.open(workspace, row['connection_id'], row['key_ciphertext'], purpose='novofon')['api_key']
                hours = row['sample_hours']
                db.execute(text("UPDATE novofon_settings SET status='checking',check_token=:t,check_until=:until "
                                "WHERE workspace_id=:w"), {'w': workspace, 't': token, 'until': now + timedelta(seconds=90)})
            # No database transaction, row lock or MAX/browser lease during IO.
            try:
                report = public_report(audit(self.client_factory(key), hours=hours, probe_recording=probe_recording))
            except Exception as error:
                LOG.warning('Novofon diagnostic failed type=%s', type(error).__name__)
                report = {'account': {'status': 'error', 'code': 'check_failed',
                                       'message': 'Проверка прервана. Повторите её позже.'}}
            with self.accounts.transaction(actor) as db:
                self.accounts._workspace(db, actor, workspace, manage=True)
                row = self._row(db, workspace, True)
                if not row or row['version'] != expected_version or row['check_token'] != token:
                    raise AuthError(409, 'Настройки изменились во время проверки. Получите актуальное состояние.')
                verified = all(report.get(k, {}).get('status') == 'ok' for k in ('account', 'journal'))
                db.execute(text("UPDATE novofon_settings SET status=:s,report=CAST(:r AS jsonb),checked_at=now(),"
                                "check_token=NULL,check_until=NULL WHERE workspace_id=:w"),
                           {'w': workspace, 's': 'api_verified' if verified else 'error', 'r': json.dumps(report)})
                account_audit(db, 'novofon_checked', user_id=actor.user_id, workspace_id=workspace)
                return self._public(self._row(db, workspace))
        finally:
            CHECK_SLOTS.release()
