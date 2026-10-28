"""Independent AlfaCRM native-MAX connection and durable inbox synchronization."""

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from center_messenger.accounts import audit
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser
from center_messenger.alfa_repair import REPAIR_BATCH, customer_directory, reconcile_messages, repair_batch, resolve_customer
from center_messenger.alfa_monitor import AlfaSyncMonitor
from center_messenger.alfa_history import (
    advance_history, history_coverage, history_pending, plan_history,
    reconcile_history, retry_history, verify_saved_pages, coverage_rows, adopt_history, next_history_delay, history_flow, live_freshness,
)
from center_messenger.history_receipts import record_pages, finish_pages, new_records
from center_messenger import active_review, family_cache
from center_messenger import alfa_shared_parent
from center_messenger import native_relink
from center_messenger.history_receipts import observed_customers
from center_messenger.auth_security import AuthError
from center_messenger.chat_store import changed, refresh_unread
from center_messenger.crm_api import DirectoryReader
from center_messenger.database import log_database_error
from center_messenger.message_order import message_order, source_order, visible_message
from center_messenger.alfa_session import NativeSessions, SECRET, CONTROL, merge_state, control, same_session, stamp, event
from center_messenger.native_priority import BUSY_RETRY

ERRORS = {
    "recovery_disabled": "Автоматическое восстановление выключено. Войдите в AlfaCRM через настройки подключения.",
    "browser_busy": "Браузер AlfaCRM занят. Операция продолжится автоматически.",
    "browser_priority_wait": "Операция в очереди браузера AlfaCRM. Продолжим автоматически.",
    "send_prepare_timeout": "Подготовка отправки задержалась. Повторим автоматически; сообщение ещё не отправлялось.",
    "send_prepare_failed": "Подготовка отправки прервана. Повторим автоматически; сообщение ещё не отправлялось.",
    "session_check_pending": "Проверяем авторизацию AlfaCRM. Отправка продолжится после восстановления.",
    "recovery_credentials_required": "Для автоматического входа один раз сохраните почту и пароль AlfaCRM в настройках подключения.",
    "recovery_retry": "Восстанавливаем подключение AlfaCRM автоматически. Повторим после паузы.",
    "browser_unavailable": "Серверный браузер AlfaCRM не установлен. Обновите сервис и повторите.",
    "unsafe_address": "Адрес CRM не прошёл проверку безопасности.",
    "network": "Не удалось связаться с AlfaCRM. Повторите позже.",
    "credentials_rejected": "AlfaCRM не приняла почту или пароль.",
    "login_form_unknown": "Не удалось распознать форму входа AlfaCRM.",
    "login_failed": "Вход в AlfaCRM не завершён. Проверьте учётные данные и попробуйте снова.",
    "otp_required": "AlfaCRM отправила код подтверждения на почту. Введите его ниже.",
    "otp_unavailable": "AlfaCRM не показала поле кода. Запустите подключение заново.",
    "otp_rejected": "AlfaCRM не приняла код. Проверьте письмо и попробуйте снова.",
    "redirect_blocked": "AlfaCRM перенаправила браузер на другой домен. Подключение остановлено.",
    "crm_unavailable": "Не удалось загрузить штатную страницу MAX в AlfaCRM.",
    "invalid_response": "AlfaCRM вернула страницу MAX неизвестного формата.",
    "form_unavailable": "AlfaCRM не вернула форму отправки MAX для этой карточки. Проверьте привязку MAX и права доступа в AlfaCRM.",
    "max_recipient_missing": "У получателя не найден аккаунт MAX в AlfaCRM. Сообщение не отправлено.",
    "max_form_missing": "Для этого клиента AlfaCRM не предоставила форму MAX. Проверьте, подключён ли MAX к его карточке.",
    "crm_customer_missing": "Карточка клиента не найдена в AlfaCRM. Проверьте филиал и карточку получателя.",
    "crm_access_denied": "AlfaCRM отказала в доступе к карточке или форме MAX. Проверьте права пользователя.",
    "crm_rate_limited": "AlfaCRM временно ограничила запросы. Повторите отправку позже.",
    "recipient_missing": "AlfaCRM не указала MAX ID получателя. Сообщение не отправлено.",
    "invalid_recipient": "Филиал или получатель MAX не совпадает. Сообщение не отправлено.",
    "multiple_recipients": "В форме MAX найдено несколько получателей. Сообщение не отправлено.",
    "csrf_missing": "В форме MAX нет защитного токена. Сообщение не отправлено.",
    "message_too_long": "Сообщение длиннее ограничения штатной формы MAX.",
    "crm_rejected": "AlfaCRM отклонила сообщение. Проверьте журнал исходящих.",
    "send_unknown": (
        "Результат отправки неизвестен. Проверьте исходящие в AlfaCRM; повтор не выполнялся."
    ),
    "reauth_required": "Сессия AlfaCRM завершилась. Подключите её заново.",
    "not_connected": "Сначала подключите отдельную веб-сессию AlfaCRM для MAX.",
    "history_unavailable": "В истории AlfaCRM пока нет связанной карточки клиента.",
    "history_incomplete": "Полнота первичной истории требует проверки исходных ID.",
    "history_gap": "Есть неподтверждённые записи первичной истории. Проверьте конкретный журнал.",
    "history_projection": "Не все прочитанные ID сообщений сохранены. Страница будет проверена повторно; позиция обхода не изменена.",
    "settings_changed": "Настройки изменились во время чтения. Сервис повторит текущий проход автоматически.",
    "internal": "Не удалось обновить историю AlfaCRM MAX. Повторите позже.",
}


def _failure(code, status=502):
    return AuthError(status, ERRORS.get(code, ERRORS["internal"]))


def _source_record_id(item, branch):
    return f"{item['direction']}:{branch}:{item['record_id']}"


class AlfaMaxConnections:
    def __init__(self, alfa, browser=None):
        self.alfa, self.accounts, self.vault = alfa, alfa.accounts, alfa.vault
        self.monitor = AlfaSyncMonitor()
        self.browser = browser or AlfaMaxBrowser()
        self.sessions = NativeSessions(self)
        self._sms_templates = []
        self._sms_templates_at = None
        self._sms_template_scope = None
        self._progress_cache = {}
        self._card_progress_cache = {}

    @staticmethod
    def _match_sms_template(text, templates):
        if not text or not templates:
            return None
        message = " ".join(text.split())
        best, best_len = None, -1
        for template in templates:
            parts = [" ".join(p.split()) for p in re.split(r"\{[^{}]*\}", template["body"])]
            parts = [p for p in parts if p]
            if not parts:
                continue
            pattern = ".*?".join(re.escape(p) for p in parts)
            if re.search(pattern, message, flags=re.IGNORECASE | re.DOTALL) and len(pattern) > best_len:
                best, best_len = template, len(pattern)
        return best["name"] if best else None

    def sms_templates(self, origin, state, branch, *, scope=None):
        now = datetime.now(UTC)
        if (
            self._sms_template_scope == scope
            and self._sms_templates_at
            and now - self._sms_templates_at < timedelta(minutes=10)
        ):
            return self._sms_templates
        try:
            if self._sms_template_scope != scope:
                self._sms_templates = []
            self._sms_templates = self.browser.read_sms_templates(origin, state, branch)
            self._sms_templates_at = now
            self._sms_template_scope = scope
        except AlfaBrowserError:
            # Keep the previous cache when the CRM is briefly unavailable.
            pass
        return self._sms_templates

    @contextmanager
    def _transaction(self, actor, workspace, *, lock=False, snapshot=False):
        with self.alfa._transaction(actor, workspace, lock=lock, snapshot=snapshot) as db:
            yield db

    @staticmethod
    def _row(db, workspace):
        return (
            db.execute(
                text(
                    "SELECT m.*,c.name,c.status AS channel_status,a.origin AS crm_origin,"
                    "a.connection_id AS current_crm_connection_id,a.version AS crm_settings_version "
                    "FROM alfa_max_settings m JOIN connections c ON "
                    "c.workspace_id=m.workspace_id AND c.id=m.connection_id "
                    "LEFT JOIN alfa_settings a ON a.workspace_id=m.workspace_id AND "
                    "a.connection_id=m.crm_connection_id AND a.secret_ciphertext IS NOT NULL "
                    "WHERE m.workspace_id=:w"
                ),
                {"w": workspace},
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def public(row, *, vault_ready=True):
        if row is None:
            return {
                "configured": False,
                "vault_ready": vault_ready,
                "state": "not_configured",
                "id": None,
                "name": "MAX через AlfaCRM",
                "email": "",
                "last_sync_at": None,
                "error": None,
                "can_send": False,
                "kind": "alfa_max_browser",
            }
        state = row["state"]
        if row["current_crm_connection_id"] is None:
            state = "reauth_required"
        return {
            "configured": True,
            "vault_ready": vault_ready,
            "state": state,
            "id": str(row["connection_id"]),
            "name": row["name"],
            "email": row["email"],
            "last_sync_at": row["last_sync_at"],
            "error": ERRORS.get(row["last_error_code"]),
            "can_send": (
                state == "active"
                and not control(row).get('needs_check')
                and row["session_ciphertext"] is not None
                and row["current_crm_connection_id"] is not None
            ),
            "kind": "alfa_max_browser",
            "recovery": {key: control(row).get(key) for key in ('phase', 'at', 'after', 'attempts', 'event', 'cause', 'recent')} | {
                'enabled': bool(row['session_ciphertext'] and control(row).get('enabled')),
                'credentials_saved': bool(row['session_ciphertext'] and control(row).get('credentials_saved')),
                'configured': 'enabled' in control(row),
                'manual_error': ERRORS.get(control(row).get('last_manual_error')),
            },
        }

    def status(self, actor, workspace):
        with self._transaction(actor, workspace) as db:
            row = self._row(db, workspace)
            crm = self.alfa._row(db, workspace)
            result = self.public(row, vault_ready=self.vault.ready)
            result["crm_configured"] = bool(crm and crm["secret_ciphertext"])
            result["branches"] = []
            if crm and crm["secret_ciphertext"]:
                branches = db.execute(
                    text(
                        "SELECT external_id,name FROM alfa_branches WHERE workspace_id=:w "
                        "AND connection_id=:c AND available ORDER BY lower(name),external_id"
                    ),
                    {"w": workspace, "c": crm["connection_id"]},
                ).mappings()
                result["branches"] = [
                    {"id": str(item["external_id"]), "name": item["name"]} for item in branches
                ]
            return result

    def channels(self, actor, workspace):
        result = self.status(actor, workspace)
        if not result["configured"]:
            return []
        return [
            {
                "id": result["id"],
                "name": result["name"],
                "kind": result["kind"],
                "enabled": result["state"] == "active",
                "verified": result["state"] == "active",
                "can_send": result["can_send"],
                "error": result["error"],
                "last_inbound_at": result["last_sync_at"],
                "history_backfill": True,
            }
        ]

    def progress(self, actor, workspace, *, activity_only=False):
        # A read-only snapshot keeps counters and cursors consistent without
        # waiting for, or blocking, the projector's exclusive workspace lock.
        with self._transaction(actor, workspace, snapshot=True) as db:
            row = self._row(db, workspace)
            if row is None:
                return {
                    "configured": False,
                    "state": "not_configured",
                    "syncing": False,
                    "messages": 0,
                    "conversations": 0,
                    "cursor": {},
                    "last_sync_at": None,
                }
            cursor = dict(row["history_cursor"] or {})
            queue = db.execute(
                text(
                    "SELECT CASE WHEN isfinite(due_at) THEN due_at END AS due_at,"
                    "CASE WHEN isfinite(lease_until) THEN lease_until END AS lease_until,"
                    "lease_owner,lease_owner IS NOT NULL AND lease_until>clock_timestamp() AS running "
                    "FROM alfa_max_sync_queue WHERE workspace_id=:w AND connection_id=:c"
                ),
                {"w": workspace, "c": row["connection_id"]},
            ).mappings().one_or_none()
            live = self.monitor.snapshot(workspace, row["connection_id"], queue["lease_owner"]) if queue and queue["running"] else None
            worker = dict(live or cursor.get("activity") or {}, live=live is not None)
            base = {"configured": True, "state": self.public(row)["state"],
                    "syncing": bool(queue and queue["running"]), "scheduled": queue is not None,
                    "next_attempt_at": queue["due_at"] if queue else None,
                    "lease_expires_at": queue["lease_until"] if queue and queue["running"] else None,
                    "worker": worker, "last_sync_at": row["last_sync_at"],
                    "server_at": datetime.now(UTC).isoformat(),
                    "active_review": active_review.status(db, workspace, row["crm_connection_id"]),
                    "client_repair": cursor.get("client_repair"),
                    "archive_paused": bool(cursor.get("archive_paused")),
                    "live_cycle": cursor.get("live_cycle"),
                    "last_live_cycle": cursor.get("last_live_cycle"),
                    "freshness": live_freshness(cursor),
                    "card_worker": family_cache.current(db, workspace, row["crm_connection_id"]),
                    "error": ERRORS.get(row["last_error_code"]), "error_code": row["last_error_code"]}
            if activity_only:
                return base | {"activity_only": True, "backfill_pending": history_pending(cursor)}
            cache_key = (str(workspace), str(row["connection_id"]), row["version"])
            cached = self._progress_cache.get(cache_key)
            if not cached or cached["expires"] <= time.monotonic():
                counts = db.execute(
                    text(
                        "SELECT count(DISTINCT c.id) FILTER(WHERE " + visible_message() + ") AS conversations,"
                        "count(m.id) FILTER(WHERE " + visible_message() + ") AS messages,"
                        "count(m.id) AS stored_messages,"
                        "min(m.occurred_at) FILTER(WHERE " + visible_message() + ") AS oldest_at,"
                        "max(m.occurred_at) FILTER(WHERE " + visible_message() + ") AS newest_at "
                        "FROM conversations c JOIN messages m ON m.workspace_id=c.workspace_id "
                        "AND m.conversation_id=c.id WHERE c.workspace_id=:w AND c.connection_id=:c"
                    ),
                    {"w": workspace, "c": row["connection_id"]},
                ).mappings().one()
                cached = {"counts": dict(counts), "rows": coverage_rows(db, workspace, row["connection_id"]),
                          "as_of": datetime.now(UTC).isoformat(), "expires": time.monotonic() + 30}
                if len(self._progress_cache) >= 64:
                    self._progress_cache.clear()
                self._progress_cache[cache_key] = cached
            counts = cached["counts"]
            card_key = (str(workspace), str(row["crm_connection_id"]), row.get("crm_settings_version"))
            cards = self._card_progress_cache.get(card_key)
            if not cards or cards["expires"] <= time.monotonic():
                cards = {"value": family_cache.progress(db, workspace, row["crm_connection_id"],
                    DirectoryReader._published(db, workspace)), "expires": time.monotonic() + 15}
                if len(self._card_progress_cache) >= 64:
                    self._card_progress_cache.clear()
                self._card_progress_cache[card_key] = cards
            branch_rows = db.execute(text(
                "SELECT external_id,name FROM alfa_branches WHERE workspace_id=:w "
                "AND connection_id=:c AND available ORDER BY external_id"
            ), {"w": workspace, "c": row["crm_connection_id"]}).mappings().all()
            names = {str(b["external_id"]): b["name"] for b in branch_rows}
            cursor = adopt_history(cursor, [b["external_id"] for b in branch_rows])
            coverage = history_coverage(db, workspace, row["connection_id"], cursor, [b["external_id"] for b in branch_rows], saved_rows=cached["rows"])
            for stream in coverage["streams"]:
                stream["branch_name"] = names[stream["branch_id"]]
                stream["error"] = ERRORS.get(stream["error_code"])
            return base | {
                "messages": counts["messages"],
                "stored_messages": counts["stored_messages"],
                "conversations": counts["conversations"],
                "cursor": cursor,
                "counters_as_of": cached["as_of"],
                "oldest_at": counts["oldest_at"],
                "newest_at": counts["newest_at"],
                "cards": cards["value"],
                **coverage,
            }

    def _ensure_channel(self, actor, workspace, email, crm_connection):
        with self._transaction(actor, workspace, lock=True) as db:
            existing = self._row(db, workspace)
            if existing:
                metadata = control(existing)
                if metadata.get('lease') and stamp(metadata.get('until')) > datetime.now(UTC):
                    raise AuthError(409, ERRORS['browser_busy'])
                # Preparation is read-only for existing connections. Commit a
                # replacement only after the candidate login has been checked.
                connection, version = existing['connection_id'], existing['version']
            else:
                connection = uuid4()
                version = 1
                db.execute(
                    text(
                        "INSERT INTO connections(workspace_id,id,kind,name,status) "
                        "VALUES (:w,:c,'alfa_max_browser','MAX через AlfaCRM','connecting')"
                    ),
                    {"w": workspace, "c": connection},
                )
                db.execute(
                    text(
                        "INSERT INTO alfa_max_settings("
                        "workspace_id,connection_id,crm_connection_id,email) "
                        "VALUES (:w,:c,:crm,:e)"
                    ),
                    {"w": workspace, "c": connection, "crm": crm_connection, "e": email},
                )
            return connection, version

    def _crm(self, actor, workspace):
        with self._transaction(actor, workspace) as db:
            row = self.alfa._row(db, workspace)
            if row is None or not row["secret_ciphertext"]:
                raise AuthError(409, "Сначала подключите AlfaCRM в разделе «Подключения».")
            branches = (
                db.execute(
                    text(
                        "SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                        "AND connection_id=:c AND available ORDER BY external_id"
                    ),
                    {"w": workspace, "c": row["connection_id"]},
                )
                .scalars()
                .all()
            )
            if not branches:
                raise AuthError(409, "Сначала загрузите филиалы AlfaCRM.")
            return dict(row), list(branches)

    def connect(self, actor, workspace, email, password, auto_reconnect=True):
        self.accounts.throttle(f"alfa-max-login:{workspace}:{actor.user_id}", limit=8, minutes=10)
        if not self.vault.ready:
            raise AuthError(503, "Ключ хранилища веб-сессий недоступен на сервере.")
        crm, branches = self._crm(actor, workspace)
        connection, version = self._ensure_channel(actor, workspace, email, crm["connection_id"])
        with self.sessions.operation(workspace, connection, administrative=True) as operation:
            if operation['row']['version'] != version:
                raise AuthError(409, 'Подключение изменилось. Повторите вход.')
            try:
                state, browser_state = self.browser.login(crm["origin"], email, password.get_secret_value())
            except AlfaBrowserError as error:
                self._set_error(actor, workspace, connection, version, error.code, operation['row'])
                raise _failure(error.code, 422 if error.code == "credentials_rejected" else 502) from None
            if state not in {'active', 'otp_required'}:
                raise _failure('invalid_response')
            verified, cause = False, None
            if state == 'active':
                try:
                    browser_state = self.browser.check_session(crm['origin'], browser_state, branches[0])
                    verified = True
                except AlfaBrowserError as error:
                    if error.code not in {'network', 'crm_unavailable', 'browser_unavailable'}:
                        self._set_error(actor, workspace, connection, version, error.code, operation['row'])
                        raise _failure(error.code) from None
                    # Login succeeded; retain its fresh cookies and verify later.
                    cause = error.code
                    partial = getattr(error, 'browser_state', None)
                    if isinstance(partial, dict) and isinstance(partial.get('cookies'), list):
                        browser_state = partial
            browser_state = merge_state({}, browser_state)
            if auto_reconnect:
                browser_state[SECRET] = {'email': email, 'password': password.get_secret_value(), 'enabled': True}
            self._connected(actor, workspace, connection, version, state, browser_state,
                            operation=operation, crm=crm, email=email, verified=verified, cause=cause)
        return self.status(actor, workspace)

    def _connected(self, actor, workspace, connection, version, state, browser_state, *, operation, crm, email, verified, cause):
        encrypted = self.vault.seal_browser_state(workspace, connection, browser_state)
        with self._transaction(actor, workspace, lock=True) as db:
            self.sessions.renew(db, operation)
            current = self._row(db, workspace)
            candidate_crm = self.alfa._row(db, workspace)
            if (not candidate_crm or candidate_crm['connection_id'] != crm['connection_id']
                or candidate_crm['version'] != crm['version'] or candidate_crm['origin'] != crm['origin']):
                raise AuthError(409, 'Настройки AlfaCRM изменились. Повторите вход.')
            changed_account = current['crm_connection_id'] != crm['connection_id'] or current['email'] != email
            cursor = ({} if changed_account else {k: v for k, v in (current['history_cursor'] or {}).items()
                                                   if k not in {'activity', 'live_cycle'}})
            metadata = control(current)
            metadata.update(enabled=bool(browser_state.get(SECRET)), credentials_saved=bool(browser_state.get(SECRET)),
                            phase='confirmation_required' if state == 'otp_required' else 'active' if verified else 'checking',
                            needs_check=state == 'active' and not verified, attempts=0, after=None, cause=cause,
                            last_manual_error=None, event='manual_login', at=datetime.now(UTC).isoformat())
            event(metadata, 'manual_login', row=current, state=state, source='manual', cause=cause, new_generation=True)
            cursor[CONTROL] = metadata
            changed = db.execute(text(
                "UPDATE alfa_max_settings SET state=:state,email=:email,crm_connection_id=:crm,session_ciphertext=:session,"
                "version=version+1,last_error_code=NULL,history_cursor=CAST(:cursor AS jsonb),updated_at=now() "
                "WHERE workspace_id=:w AND connection_id=:c AND version=:v "
                "AND session_ciphertext IS NOT DISTINCT FROM :old RETURNING version"),
                {'w': workspace, 'c': connection, 'state': state, 'email': email, 'crm': crm['connection_id'],
                 'session': encrypted, 'cursor': json.dumps(cursor), 'v': version,
                 'old': operation['row']['session_ciphertext']}).scalar_one_or_none()
            if changed is None:
                raise AuthError(409, 'Подключение изменилось. Повторите вход.')
            db.execute(text('UPDATE connections SET status=:s WHERE workspace_id=:w AND id=:c'),
                       {'w': workspace, 'c': connection, 's': 'active' if state == 'active' else 'connecting'})
            if changed_account:
                db.execute(text('DELETE FROM alfa_max_sync_queue WHERE workspace_id=:w'), {'w': workspace})
            if state == 'active':
                self._schedule(db, workspace, connection)
            audit(db, 'alfa_max_web_connected' if state == 'active' else 'alfa_max_otp_requested',
                  user_id=actor.user_id, workspace_id=workspace, subject_id=connection)

    def verify_otp(self, actor, workspace, code):
        with self._transaction(actor, workspace) as db:
            row = self._row(db, workspace)
        if row is None:
            raise AuthError(409, 'Сначала начните подключение AlfaCRM MAX.')
        with self.sessions.operation(workspace, row['connection_id'], dict(row), administrative=True) as operation:
            return self._verify_otp(actor, workspace, code, operation)

    def _verify_otp(self, actor, workspace, code, operation):
        self.accounts.throttle(f"alfa-max-otp:{workspace}:{actor.user_id}", limit=10, minutes=10)
        with self._transaction(actor, workspace) as db:
            row = self._row(db, workspace)
            if (
                row is None
                or row["state"] != "otp_required"
                or not row["session_ciphertext"]
                or row["current_crm_connection_id"] is None
            ):
                raise AuthError(409, "Сначала начните подключение AlfaCRM MAX заново.")
            row = dict(row)
        session = self.vault.open_browser_state(
            workspace, row["connection_id"], row["session_ciphertext"]
        )
        try:
            state, browser_state = self.browser.verify_otp(row["crm_origin"], session, code)
        except AlfaBrowserError as error:
            partial = getattr(error, 'browser_state', None)
            if isinstance(partial, dict) and isinstance(partial.get('cookies'), list):
                if error.code == 'otp_rejected':
                    self.sessions.finish(operation, partial, state='otp_required', code='otp_rejected', phase='confirmation_required')
                else:
                    self.sessions.finish(operation, partial, state='active', code='recovery_retry', phase='checking', needs_check=True)
                    with self.accounts.transaction(workspace_id=workspace) as db:
                        self._schedule(db, workspace, row['connection_id'])
            raise _failure(error.code, 422 if error.code == "otp_rejected" else 502) from None
        browser_state = merge_state(session, browser_state)
        encrypted = self.vault.seal_browser_state(workspace, row["connection_id"], browser_state)
        with self._transaction(actor, workspace, lock=True) as db:
            self.sessions.renew(db, operation)
            current = self._row(db, workspace)
            if current is None or current["version"] != row["version"]:
                raise AuthError(409, "Подключение изменилось. Начните настройку заново.")
            db.execute(
                text(
                    "UPDATE alfa_max_settings SET state='active',session_ciphertext=:session,"
                    "version=version+1,last_error_code=NULL,updated_at=now() WHERE workspace_id=:w"
                ),
                {"w": workspace, "session": encrypted},
            )
            db.execute(
                text("UPDATE connections SET status='active' WHERE workspace_id=:w AND id=:c"),
                {"w": workspace, "c": row["connection_id"]},
            )
            self._schedule(db, workspace, row["connection_id"])
            metadata = control(current)
            metadata.update(phase='checking', needs_check=True, attempts=0, after=None,
                            event='confirmation_completed', at=datetime.now(UTC).isoformat())
            event(metadata, 'confirmation_completed', row=current, state='active', source='manual', new_generation=True)
            self.sessions._write_control(db, workspace, metadata)
            audit(
                db,
                "alfa_max_web_connected",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=row["connection_id"],
            )
        return self.status(actor, workspace)

    def _set_error(self, actor, workspace, connection, version, code, snapshot=None):
        with self._transaction(actor, workspace, lock=True) as db:
            row = self._row(db, workspace)
            if not row or row['connection_id'] != connection or row['version'] != version or (snapshot and not same_session(row, snapshot)):
                return
            metadata = control(row)
            metadata.update(last_manual_error=code)
            event(metadata, 'manual_login_failed', row=row, source='manual', cause=code)
            self.sessions._write_control(db, workspace, metadata)
            # A rejected candidate password must never erase a working/expired
            # session or the credentials already saved for automatic recovery.
            if row['session_ciphertext'] is None:
                db.execute(text("UPDATE alfa_max_settings SET state='error',last_error_code=:code,updated_at=now() "
                                "WHERE workspace_id=:w AND connection_id=:c AND version=:v"),
                           {'w': workspace, 'c': connection, 'v': version, 'code': code})
                db.execute(text("UPDATE connections SET status='error' WHERE workspace_id=:w AND id=:c"),
                           {'w': workspace, 'c': connection})

    @staticmethod
    def _schedule(db, workspace, connection, delay=0):
        db.execute(
            text(
                "INSERT INTO alfa_max_sync_queue AS q(workspace_id,connection_id,due_at) "
                "VALUES (:w,:c,now()+(:delay*interval '1 second')) ON CONFLICT(workspace_id) "
                "DO UPDATE SET connection_id=EXCLUDED.connection_id,due_at=least(q.due_at,EXCLUDED.due_at),"
                "lease_owner=CASE WHEN q.connection_id=EXCLUDED.connection_id AND q.lease_until>clock_timestamp() "
                "THEN q.lease_owner ELSE NULL END,"
                "lease_until=CASE WHEN q.connection_id=EXCLUDED.connection_id AND q.lease_until>clock_timestamp() "
                "THEN q.lease_until ELSE '-infinity' END"
            ),
            {"w": workspace, "c": connection, "delay": delay},
        )

    def sync(self, actor, workspace, *, branch=None, direction=None, review_active=False, archive_paused=None):
        with self._transaction(actor, workspace, lock=True) as db:
            row = self._row(db, workspace)
            if row is None or row["state"] != "active" or row["session_ciphertext"] is None or row["current_crm_connection_id"] is None:
                raise AuthError(409, "Сначала подключите веб-сессию AlfaCRM MAX.")
            if review_active:
                return active_review.request(db, workspace, row["crm_connection_id"])
            if branch is not None:
                available = db.execute(text("SELECT 1 FROM alfa_branches WHERE workspace_id=:w AND connection_id=:c "
                    "AND external_id=:b AND available"), {"w": workspace, "c": row["crm_connection_id"], "b": branch}).scalar_one_or_none()
                if not available or direction not in {"receive", "sent"}:
                    raise AuthError(422, "Журнал недоступен.")
            running = db.execute(text(
                "SELECT 1 FROM alfa_max_sync_queue WHERE workspace_id=:w AND connection_id=:c "
                "AND lease_owner IS NOT NULL AND lease_until>clock_timestamp() FOR UPDATE"
            ), {"w": workspace, "c": row["connection_id"]}).scalar_one_or_none()
            if running and branch is None and archive_paused is None:
                return {"queued": True, "already_running": True}
            cursor = dict(row["history_cursor"] or {})
            if archive_paused is not None:
                cursor["archive_paused"] = bool(archive_paused)
                cursor["archive_pause_changed_at"] = datetime.now(UTC).isoformat()
            if branch is not None:
                if cursor.get("archive_paused"):
                    raise AuthError(409, "Продолжите загрузку истории перед повтором журнала.")
                key = f"{branch}:{direction}"
                if running:
                    cursor["retry_requests"] = dict(cursor.get("retry_requests") or {})
                    cursor["retry_requests"][key] = str(uuid4())
                else:
                    cursor = retry_history(cursor, [key])
            db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) "
                            "WHERE workspace_id=:w"), {"w": workspace, "cursor": json.dumps(cursor)})
            self._schedule(db, workspace, row["connection_id"])
            audit(
                db,
                ("alfa_max_archive_paused" if archive_paused else "alfa_max_archive_resumed")
                if archive_paused is not None else "alfa_max_sync_requested",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=row["connection_id"],
            )
        return {"queued": True, "already_running": bool(running),
                "archive_paused": bool(cursor.get("archive_paused"))}

    def disconnect(self, actor, workspace):
        with self._transaction(actor, workspace, lock=True) as db:
            row = self._row(db, workspace)
            if row is None:
                return
            db.execute(
                text(
                    "UPDATE alfa_max_settings SET state='not_configured',session_ciphertext=NULL,"
                    "last_error_code=NULL,version=version+1,history_cursor=history_cursor-'session_control',updated_at=now() WHERE workspace_id=:w"
                ),
                {"w": workspace},
            )
            db.execute(
                text(
                    "UPDATE connections SET status='not_configured' WHERE workspace_id=:w AND id=:c"
                ),
                {"w": workspace, "c": row["connection_id"]},
            )
            metadata = control(row)
            for key in ('lease', 'until'):
                metadata.pop(key, None)
            metadata.update(phase='disabled', enabled=False, credentials_saved=False, needs_check=False, after=None, attempts=0, last_manual_error=None)
            event(metadata, 'manual_disconnect', row=row, state='not_configured', source='manual', new_generation=True)
            self.sessions._write_control(db, workspace, metadata)
            db.execute(
                text("DELETE FROM alfa_max_sync_queue WHERE workspace_id=:w"), {"w": workspace}
            )
            audit(
                db,
                "alfa_max_web_disconnected",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=row["connection_id"],
            )

    @staticmethod
    def _message_identity(connection, customer, branch):
        contact = customer.get("contact_id") if customer else None
        if contact:
            identity = f"alfa-contact:{contact}"
        elif customer:
            identity = f"alfa-customer:{customer['branch_id']}:{customer['customer_id']}"
        else:
            identity = f"alfa-unlinked:{branch}"
        conversation_id = uuid5(connection, identity)
        display = customer["name"] if customer and customer.get("name") else "Клиент не определён"
        return identity, conversation_id, display

    @staticmethod
    def _supersede(db, workspace, old, canonical):
        db.execute(text("UPDATE messages keeper SET payload=keeper.payload||jsonb_build_object('handled_receipt',"
                        "old.payload->'handled_receipt'),version=keeper.version+1 FROM messages old "
                        "WHERE keeper.workspace_id=:w AND keeper.id=:canonical AND old.workspace_id=:w "
                        "AND old.id=:old AND old.payload->>'handled_receipt' IS NOT NULL "
                        "AND keeper.payload->>'handled_receipt' IS NULL"),
                   {"w": workspace, "old": old, "canonical": canonical})
        db.execute(
            text("UPDATE messages SET provider_message_id=NULL,"
                 "payload=jsonb_set((payload-'crm_mirror')||CASE WHEN provider_message_id IS NOT NULL "
                 "AND NOT (payload ? 'original_provider_message_id') THEN "
                 "jsonb_build_object('original_provider_message_id',provider_message_id) ELSE '{}'::jsonb END,"
                 "'{superseded_by}',to_jsonb(CAST(:canonical AS text))),"
                 "version=version+1 WHERE workspace_id=:w AND id=:old"),
            {"w": workspace, "old": old, "canonical": str(canonical)},
        )
        db.execute(
            text("UPDATE source_records SET message_id=:canonical "
                 "WHERE workspace_id=:w AND message_id=:old"),
            {"w": workspace, "old": old, "canonical": canonical},
        )

    def _merge_contact_conversations(self, db, workspace, channel, contact, canonical, *, family_contacts=None):
        """Unify a verified CRM contact or proven shared incoming MAX sender.

        Retain the old conversation IDs as aliases. All messages, pending sends
        and manual handling state survive; only proven provider-ID echoes hide.
        The worker holds the workspace lock throughout this operation.
        """
        if db.execute(text("SELECT crm_link_manual FROM conversations WHERE workspace_id=:w AND id=:c"),
                      {"w": workspace, "c": canonical}).scalar_one_or_none():
            return
        contacts = family_contacts or [contact]
        old = list(db.execute(
            text("SELECT c.id FROM conversations c WHERE c.workspace_id=:w "
                 "AND c.connection_id=:channel AND c.contact_id=ANY(CAST(:contacts AS uuid[])) "
                 "AND c.id<>:canonical AND NOT c.crm_link_manual "
                 "AND (EXISTS(SELECT 1 FROM messages m WHERE m.workspace_id=c.workspace_id "
                 "AND m.conversation_id=c.id) OR EXISTS(SELECT 1 FROM send_commands s "
                 "WHERE s.workspace_id=c.workspace_id AND s.conversation_id=c.id)) "
                 "ORDER BY c.id FOR UPDATE"),
            {"w": workspace, "channel": channel, "contacts": contacts, "canonical": canonical},
        ).scalars())
        if not old:
            return
        from center_messenger.chat_queue import materialize_legacy_handled
        for identifier in old + [canonical]:
            materialize_legacy_handled(db, workspace, identifier)
        handled = db.execute(
            text("SELECT c.handled_inbound_id,c.handled_at,c.handled_by FROM conversations c "
                 "JOIN messages m ON m.workspace_id=c.workspace_id AND m.id=c.handled_inbound_id "
                 "WHERE c.workspace_id=:w AND c.id=ANY(CAST(:ids AS uuid[])) "
                 "ORDER BY " + message_order(descending=True) + " LIMIT 1"),
            {"w": workspace, "ids": old + [canonical]},
        ).mappings().one_or_none()
        for previous in old:
            duplicates = list(db.execute(
                text("SELECT m.id,m.send_command_id,n.id AS canonical_id,n.send_command_id AS canonical_send "
                     "FROM messages m JOIN messages n ON n.workspace_id=m.workspace_id "
                     "AND n.conversation_id=:canonical AND n.payload->>'superseded_by' IS NULL "
                     "AND (n.provider_message_id=m.provider_message_id OR ("
                     "m.direction='in' AND n.direction='in' "
                     "AND m.payload->>'branch_id'=n.payload->>'branch_id' "
                     "AND m.payload->>'crm_record_id' IS NOT NULL "
                     "AND m.payload->>'crm_record_id'=n.payload->>'crm_record_id')) "
                     "WHERE m.workspace_id=:w AND m.conversation_id=:old "
                     "AND m.payload->>'superseded_by' IS NULL"),
                {"w": workspace, "old": previous, "canonical": canonical},
            ).mappings())
            for duplicate in duplicates:
                if duplicate["send_command_id"] and not duplicate["canonical_send"]:
                    self._supersede(db, workspace, duplicate["canonical_id"], duplicate["id"])
                    changed(db, workspace, canonical, duplicate["canonical_id"])
                elif not duplicate["send_command_id"]:
                    self._supersede(db, workspace, duplicate["id"], duplicate["canonical_id"])
                else:
                    # Two distinct send requests are not proof of a duplicate.
                    db.execute(text("UPDATE messages SET provider_message_id=NULL,version=version+1 "
                                    "WHERE workspace_id=:w AND id=:id"),
                               {"w": workspace, "id": duplicate["id"]})
            db.execute(
                text("UPDATE send_commands SET conversation_id=:canonical "
                     "WHERE workspace_id=:w AND conversation_id=:old"),
                {"w": workspace, "old": previous, "canonical": canonical},
            )
            db.execute(
                text("WITH moved AS (UPDATE messages SET conversation_id=:canonical,version=version+1 "
                     "WHERE workspace_id=:w AND conversation_id=:old RETURNING id,direction,payload), "
                     "changes AS (INSERT INTO message_changes(workspace_id,conversation_id,message_id) "
                     "SELECT :w,:old,id FROM moved UNION ALL SELECT :w,:canonical,id FROM moved) "
                     "UPDATE conversations SET inbound_seq=inbound_seq+(SELECT count(*) FROM moved "
                     "WHERE direction='in' AND payload->>'superseded_by' IS NULL),revision=revision+1 "
                     "WHERE workspace_id=:w AND id=:canonical"),
                {"w": workspace, "old": previous, "canonical": canonical},
            )
            refresh_unread(db, workspace, previous)
        if handled:
            db.execute(
                text("UPDATE conversations SET handled_inbound_id=:message,handled_at=:at,handled_by=:user,answered_seq=greatest(answered_seq,1) "
                     "WHERE workspace_id=:w AND id=:id"),
                {"w": workspace, "id": canonical, "message": handled["handled_inbound_id"],
                 "at": handled["handled_at"], "user": handled["handled_by"]},
            )

    def _project_page(self, db, workspace, channel, crm_connection, branch, rows, *, on_progress=None):
        snapshot = DirectoryReader._published(db, workspace)
        previous = native_relink.previous_rows(db, workspace, channel, branch, rows)
        directory = customer_directory(db, workspace, snapshot, [
            customer["customer_id"] for item in rows for customer in item.get("customers", []) if customer] + [
            int(row['payload']['customer_id']) for row in previous.values() if row['payload'].get('customer_id')])
        touched = set()
        verified_conversations = set()
        merged_contacts = set()
        refreshed_families = set()
        peer_cache = {}
        def peer_family(customer):
            key = (customer["contact_id"], customer.get("primary_phone"))
            if key not in peer_cache:
                peer_cache[key] = alfa_shared_parent.members(db, workspace, channel, snapshot, *key)
            return peer_cache[key]
        for item_index, item in enumerate(rows):
            customers = {}
            resolved = []
            for raw_customer in item.get("customers") or []:
                customer = dict(raw_customer) if raw_customer else None
                if customer and snapshot:
                    customer = resolve_customer(customer, directory)
                if customer:
                    resolved.append(customer)
                if customer and not customer.get("contact_id"):
                    from center_messenger.customer_lookup import enqueue
                    enqueue(db, workspace, crm_connection, customer["customer_id"], customer["branch_id"])
            if item["direction"] == "in" and item["actor_type"] == "contact":
                proven = native_relink.prove(db, workspace, channel, snapshot, branch, item,
                    previous.get(str(item['record_id'])), resolved, directory)
                if not proven:
                    proven = alfa_shared_parent.prove(
                        db, workspace, channel, snapshot, resolved,
                        source=_source_record_id(item, branch))
                if proven:
                    refreshed_families.add(proven[0])
                    peer_cache.clear()
            for customer in resolved or [None]:
                if not customer:
                    group = "unlinked"
                elif customer.get("contact_id"):
                    family = peer_family(customer)
                    group = f"contact:{family[0]}"
                else:
                    group = f"customer:{customer['branch_id']}:{customer['customer_id']}"
                customers.setdefault(group, []).append(customer)
            for group, records in sorted(customers.items()):
                # One conversation per proven peer; retain every CRM Customer ID.
                customer = min(
                    records,
                    key=lambda value: (
                        value is None,
                        (value.get("contact_id") is None) if value else True,
                        -{"client": 3, "lead": 2, "archive": 1}.get(value.get("status"), 0) if value else 0,
                        value["branch_id"] != int(branch) if value else True,
                        value["branch_id"] if value else 0,
                        value["customer_id"] if value else 0,
                    ),
                )
                family_contacts = (peer_family(customer)
                    if customer and customer.get("contact_id") else [])
                peer_contact = family_contacts[0] if family_contacts else None
                identity, conversation_id, display = self._message_identity(
                    UUID(str(channel)), dict(customer, contact_id=peer_contact) if peer_contact else customer, branch
                )
                client = {"name": display, "contact_id": str(customer["contact_id"]) if customer and customer.get("contact_id") else None,
                          "cards": [{"customer_id": str(c["customer_id"]), "branch_id": str(c["branch_id"]),
                                     "name": c.get("name") or ""} for c in records if c]}
                detail = {"client": client, "record_id": str(item["record_id"]),
                          "position": item_index + 1, "position_total": len(rows),
                          "occurred_at": item["occurred_at"].isoformat(), "message_direction": item["direction"]}
                def notify(result=None):
                    if on_progress:
                        event = {"kind": "message", **detail, "result": result} if result else None
                        on_progress(detail | {"operation": "message_checked" if result else "save_message", "event": event})
                notify()
                external = identity
                saved_conversation = db.execute(
                    text(
                        "INSERT INTO conversations(workspace_id,id,connection_id,contact_id,"
                        "external_chat_id,peer_name) VALUES (:w,:id,:c,:contact,:external,:name) "
                        "ON CONFLICT("
                        "workspace_id,connection_id,external_chat_id,external_topic_id) "
                        "DO UPDATE SET contact_id=CASE WHEN conversations.crm_link_manual THEN conversations.contact_id ELSE coalesce("
                        "EXCLUDED.contact_id,conversations.contact_id) END,"
                        "peer_name=CASE WHEN EXCLUDED.peer_name<>'' THEN EXCLUDED.peer_name "
                        "ELSE conversations.peer_name END RETURNING id,crm_link_manual"
                    ),
                    {
                        "w": workspace,
                        "id": conversation_id,
                        "c": channel,
                        "contact": peer_contact,
                        "external": external,
                        "name": display,
                    },
                ).mappings().one()
                conversation_id = saved_conversation["id"]
                merge_key = (peer_contact, tuple(family_contacts))
                if not saved_conversation["crm_link_manual"] and peer_contact and merge_key not in merged_contacts:
                    self._merge_contact_conversations(
                        db, workspace, channel, peer_contact, conversation_id,
                        family_contacts=family_contacts,
                    )
                    merged_contacts.add(merge_key)
                if peer_contact:
                    verified_conversations.add(conversation_id)
                touched.add(conversation_id)
                base_source_id = _source_record_id(item, branch)
                prefix = base_source_id + "|recipient|"
                source_keys = [base_source_id, prefix + "unlinked"] + [
                    prefix + f"{seen_branch}:{r['customer_id']}"
                    for group_records in customers.values() for r in group_records if r
                    for seen_branch in {r["branch_id"], r.get("observed_branch_id", r["branch_id"])}
                ]
                candidates = list(
                    db.execute(
                        text(
                            "SELECT m.*,s.external_record_id AS source_key FROM source_records s JOIN messages m ON "
                            "m.workspace_id=s.workspace_id AND m.id=s.message_id "
                            "WHERE s.workspace_id=:w AND s.connection_id=:c "
                            "AND s.namespace='alfa_max' "
                            "AND s.external_record_id=ANY(CAST(:keys AS text[])) "
                            "ORDER BY s.external_record_id"
                        ),
                        {"w": workspace, "c": channel, "keys": source_keys},
                    )
                    .mappings()
                )
                # A bulk journal row can name several unrelated parents. Its
                # stable source identity must have one projection per recipient,
                # while multiple cards of one verified parent share one bubble.
                recipient_cards = {(seen_branch, r["customer_id"]) for r in records if r
                                   for seen_branch in {r["branch_id"], r.get("observed_branch_id", r["branch_id"])}}
                matching = [m for m in candidates if (
                    m["payload"].get("customer_branch_id"), m["payload"].get("customer_id")
                ) in recipient_cards or m["conversation_id"] == conversation_id]
                matching.sort(key=lambda m: (m["send_command_id"] is None, m["source_key"] != base_source_id))
                existing = matching[0] if matching else None
                if existing and customer and customer.get("contact_id"):
                    for duplicate in {m["id"]: m for m in matching}.values():
                        if duplicate["id"] != existing["id"] and not duplicate["send_command_id"]:
                            self._supersede(db, workspace, duplicate["id"], existing["id"])
                            changed(db, workspace, duplicate["conversation_id"], duplicate["id"])
                            touched.add(duplicate["conversation_id"])
                if existing is None and len(customers) == 1:
                    existing = next((m for m in candidates if m["source_key"] == base_source_id
                        and (m["payload"].get("customer_id") is None or
                             (m["payload"].get("customer_branch_id"), m["payload"].get("customer_id"))
                             in recipient_cards)), None)
                if existing:
                    source_id = existing["source_key"]
                elif not any(m["source_key"] == base_source_id for m in candidates):
                    source_id = base_source_id
                else:
                    source_id = prefix + (f"{customer['branch_id']}:{customer['customer_id']}" if customer else "unlinked")

                # Undo aliases created by the old text/time heuristic. Distinct
                # CRM row keys must survive even when their content is identical.
                same_source = existing and (
                    existing["payload"].get("crm_record_id") == str(item["record_id"])
                    and str(existing["payload"].get("branch_id")) == str(branch)
                    and existing["direction"] == item["direction"]
                )
                if existing and not same_source and existing["provider_message_id"] != f"alfa:{source_id}":
                    aliases = db.execute(
                        text("SELECT count(*) FROM source_records WHERE workspace_id=:w "
                             "AND connection_id=:c AND namespace='alfa_max' AND message_id=:m"),
                        {"w": workspace, "c": channel, "m": existing["id"]},
                    ).scalar_one()
                    if aliases > 1:
                        db.execute(
                            text("DELETE FROM source_records WHERE workspace_id=:w "
                                 "AND connection_id=:c AND namespace='alfa_max' "
                                 "AND external_record_id=:source"),
                            {"w": workspace, "c": channel, "source": source_id},
                        )
                        existing = None
                if existing and existing["conversation_id"] != conversation_id:
                    old_conversation = existing["conversation_id"]
                    db.execute(
                        text("UPDATE messages SET conversation_id=:conv,version=version+1 "
                             "WHERE workspace_id=:w AND id=:m"),
                        {"w": workspace, "conv": conversation_id, "m": existing["id"]},
                    )
                    if existing["send_command_id"]:
                        db.execute(
                            text("UPDATE send_commands SET conversation_id=:conv "
                                 "WHERE workspace_id=:w AND id=:id"),
                            {"w": workspace, "conv": conversation_id, "id": existing["send_command_id"]},
                        )
                    # Keep the old change too: clients remove a moved message
                    # from their old chat cache when it no longer belongs there.
                    changed(db, workspace, old_conversation, existing["id"])
                    changed(db, workspace, conversation_id, existing["id"])
                    touched.add(old_conversation)
                    if item["direction"] == "in":
                        db.execute(
                            text("UPDATE conversations SET inbound_seq=inbound_seq+1 "
                                 "WHERE workspace_id=:w AND id=:conv"),
                            {"w": workspace, "conv": conversation_id},
                        )
                    existing = dict(existing) | {"conversation_id": conversation_id}
                if (existing and same_source and not existing["payload"].get("possible_send_command_id")
                        and not existing['payload'].get('crm_recipient_stale')
                        and existing['payload'].get('customer_id') == (customer['customer_id'] if customer else None)
                        and existing['payload'].get('customer_branch_id') == (customer['branch_id'] if customer else None)
                        and existing["payload"].get("crm_customer_ids") == sorted({r["customer_id"] for r in records if r})
                        and existing['payload'].get('crm_observed_customers') == [list(p) for p in observed_customers(item)]
                        and existing["payload"].get("crm_family_contacts") == [str(c) for c in family_contacts]
                        and (not item.get("max_user_id")
                             or existing["payload"].get("max_user_id") == item["max_user_id"])):
                    notify("checked")
                    continue
                payload = {
                    "source": "alfa_max",
                    "crm_record_id": str(item["record_id"]),
                    "crm_identity_restored": False,
                    "branch_id": branch,
                    "crm_order": item.get("crm_order"),
                    "customer_branch_id": customer["branch_id"] if customer else None,
                    "customer_id": customer["customer_id"] if customer else None,
                    "crm_global_verified": bool(customer) and all(r.get("global_id_verified", False) for r in records if r),
                    "crm_customer_ids": sorted({r["customer_id"] for r in records if r}),
                    "crm_observed_customers": [list(pair) for pair in observed_customers(item)],
                    "crm_recipient_stale": False,
                    "crm_family_contacts": [str(c) for c in family_contacts],
                    "crm_primary_branch": min((r["branch_id"] for r in records if r), default=None),
                    "attachments": item["attachments"],
                    "actor_name": item["actor_name"],
                    "actor_type": item["actor_type"],
                    "actor_id": item.get("actor_id"),
                    "actor_evidence": item.get("actor_evidence", {"reason": "source_classification"}),
                    "max_user_id": item.get("max_user_id") or (
                        existing["payload"].get("max_user_id") if existing and same_source else None
                    ),
                    "failed": item["failed"],
                    "crm_status": customer.get("status") if customer else "unknown",
                }
                if item["actor_type"] == "automation":
                    template_name = self._match_sms_template(item["text"], self._sms_templates)
                    if template_name:
                        payload["template_name"] = template_name
                if (
                    (existing is None or existing["send_command_id"] is None)
                    and item["direction"] == "out" and customer
                    and item["actor_type"] in {"human", "unknown"} and not item["failed"]
                ):
                    matches = list(
                        db.execute(
                            text(
                                "SELECT m.id,m.occurred_at,m.payload,s.id AS command_id,s.state AS command_state,"
                                "abs(extract(epoch FROM (m.occurred_at-:at))) AS distance "
                                "FROM messages m "
                                "JOIN send_commands s "
                                "ON s.workspace_id=m.workspace_id AND s.id=m.send_command_id "
                                "WHERE m.workspace_id=:w AND m.conversation_id=:conv "
                                "AND m.direction='out' "
                                "AND s.state IN ('accepted','delivered','read','unknown','sending') "
                                "AND m.text=:text AND m.payload->>'crm_branch'=:branch "
                                "AND m.payload->>'customer_id'=:customer "
                                "AND (:peer='' OR m.payload->>'max_user_id'=:peer) "
                                "AND abs(extract(epoch FROM (m.occurred_at-:at)))<=300 "
                                "AND NOT EXISTS (SELECT 1 FROM source_records x WHERE "
                                "x.workspace_id=m.workspace_id AND x.connection_id=:c "
                                "AND x.namespace='alfa_max' AND x.message_id=m.id) "
                                "ORDER BY abs(extract(epoch FROM (m.occurred_at-:at)))"
                            ),
                            {
                                "w": workspace,
                                "conv": conversation_id,
                                "text": item["text"],
                                # Match the actual sending journal independently
                                # of the preferred branch for future replies.
                                "branch": str(branch),
                                "customer": str(customer["customer_id"]),
                                "peer": str(item.get("max_user_id") or ""),
                                "at": item["occurred_at"],
                                "c": channel,
                            },
                        ).mappings()
                    )
                    # Internal user UUIDs and CRM employee IDs are different
                    # namespaces. Match only the recipient and a unique closest
                    # send, and never reuse a send already linked to a CRM row.
                    if matches and (len(matches) == 1 or matches[0]["distance"] < matches[1]["distance"]):
                        best = matches[0]
                        if best["command_state"] == "sending":
                            # An echo observed during the external call must not
                            # act as an unrelated reply covering newer arrivals.
                            # Keep its source row until the command is confirmed.
                            payload["possible_send_command_id"] = str(best["command_id"])
                        else:
                            matched_payload = dict(best["payload"])
                            matched_payload.update(payload)
                            matched_payload["source"] = "center_messenger_alfa_max"
                            matched_payload["actor_type"] = "human"
                            matched_payload["actor_evidence"] = {"version": 1, "reason": "authorized_send"}
                            if existing:
                                # Release the provider's unique key before assigning
                                # it to the local bubble. Retain the old row as a
                                # delta tombstone so open clients can remove it.
                                superseded = dict(existing["payload"]) | {"superseded_by": str(best["id"])}
                                db.execute(
                                    text("UPDATE messages SET provider_message_id=NULL,"
                                         "payload=CAST(:p AS jsonb),version=version+1 "
                                         "WHERE workspace_id=:w AND id=:id"),
                                    {"w": workspace, "id": existing["id"], "p": json.dumps(superseded)},
                                )
                                changed(db, workspace, conversation_id, existing["id"])
                            db.execute(
                                text(
                                    "UPDATE messages SET provider_message_id=:provider,occurred_at=:at,"
                                    "payload=CAST(:payload AS jsonb),version=version+1 "
                                    "WHERE workspace_id=:w AND id=:id"
                                ),
                                {
                                    "w": workspace,
                                    "id": best["id"],
                                    "provider": f"alfa:{source_id}",
                                    "at": item["occurred_at"],
                                    "payload": json.dumps(matched_payload, ensure_ascii=False),
                                },
                            )
                            db.execute(
                                text(
                                    "INSERT INTO source_records("
                                    "workspace_id,id,connection_id,message_id,"
                                    "namespace,external_record_id) "
                                    "VALUES (:w,:id,:c,:m,'alfa_max',:source) "
                                    "ON CONFLICT(workspace_id,connection_id,namespace,external_record_id) "
                                    "DO UPDATE SET message_id=EXCLUDED.message_id"
                                ),
                                {
                                    "w": workspace,
                                    "id": uuid4(),
                                    "c": channel,
                                    "m": best["id"],
                                    "source": source_id,
                                },
                            )
                            db.execute(
                                text("UPDATE send_commands SET state='accepted',last_error_code=NULL "
                                     "WHERE workspace_id=:w AND id=:id AND state='unknown'"),
                                {"w": workspace, "id": best["command_id"]},
                            )
                            changed(db, workspace, conversation_id, best["id"])
                            notify("matched_send")
                            continue
                if existing:
                    merged = dict(existing["payload"]) | payload
                    saved_text = existing['text'] if item.get('source_metadata_refresh') else item['text']
                    saved_at = existing['occurred_at'] if item.get('source_metadata_refresh') else item['occurred_at']
                    if merged.get("crm_mirror") and not payload["crm_global_verified"]:
                        merged.pop("crm_mirror", None)
                        merged.pop("superseded_by", None)
                    actor = item["actor_type"]
                    if existing["send_command_id"]:
                        merged["source"] = existing["payload"].get("source", "center_messenger_alfa_max")
                        actor = merged["actor_type"] = "human"
                        merged["actor_evidence"] = {"version": 1, "reason": "authorized_send"}
                    if (
                        existing["text"] != saved_text
                        or existing["payload"] != merged
                        or existing["actor_type"] != actor
                        or existing["occurred_at"] != saved_at
                    ):
                        db.execute(
                            text(
                                "UPDATE messages SET text=:text,actor_type=:actor,"
                                "payload=CAST(:payload AS jsonb),"
                                "occurred_at=:at,version=version+1 WHERE workspace_id=:w AND id=:id"
                            ),
                            {
                                "w": workspace,
                                "id": existing["id"],
                                "text": saved_text,
                                "actor": actor,
                                "payload": json.dumps(merged, ensure_ascii=False),
                                "at": saved_at,
                            },
                        )
                        changed(db, workspace, conversation_id, existing["id"])
                    notify("checked")
                    continue
                message_id = uuid4()
                provider_id = f"alfa:{source_id}"
                db.execute(
                    text(
                        "INSERT INTO messages(workspace_id,id,conversation_id,provider_message_id,"
                        "direction,actor_type,text,payload,occurred_at) "
                        "VALUES (:w,:m,:conv,:provider,"
                        ":direction,:actor,:body,CAST(:payload AS jsonb),:at)"
                    ),
                    {
                        "w": workspace,
                        "m": message_id,
                        "conv": conversation_id,
                        "provider": provider_id,
                        "direction": item["direction"],
                        "actor": item["actor_type"],
                        "body": item["text"] or None,
                        "payload": json.dumps(payload, ensure_ascii=False),
                        "at": item["occurred_at"],
                    },
                )
                db.execute(
                    text(
                        "INSERT INTO source_records(workspace_id,id,connection_id,message_id,"
                        "namespace,external_record_id) VALUES (:w,:id,:c,:m,'alfa_max',:source)"
                    ),
                    {
                        "w": workspace,
                        "id": uuid4(),
                        "c": channel,
                        "m": message_id,
                        "source": source_id,
                    },
                )
                happened_recently = (
                    item["direction"] == "in"
                    and item["occurred_at"] >= datetime.now(UTC) - timedelta(minutes=2)
                    and item["occurred_at"] <= datetime.now(UTC) + timedelta(seconds=30)
                )
                changed(
                    db,
                    workspace,
                    conversation_id,
                    message_id,
                    new_inbound=happened_recently and item["actor_type"] == "contact",
                )
                if item["direction"] == "in":
                    db.execute(
                        text(
                            "UPDATE conversations SET inbound_seq=inbound_seq+1 "
                            "WHERE workspace_id=:w AND id=:conv AND NOT EXISTS ("
                            "SELECT 1 FROM messages i WHERE i.workspace_id=:w AND i.conversation_id=:conv "
                            "AND i.direction='in' AND i.actor_type='contact' AND " + visible_message("i") + " "
                            "AND (i.occurred_at," + source_order("i") + ",i.id)>(:at,:order,:message))"
                        ),
                        {"w": workspace, "conv": conversation_id, "at": item["occurred_at"],
                         "order": item.get("crm_order") or 0, "message": message_id},
                    )
                notify("imported")
        for conversation_id in touched:
            if conversation_id in verified_conversations:
                reconcile_messages(self, db, workspace, channel, conversation_id)
            else:
                refresh_unread(db, workspace, conversation_id)
        if refreshed_families and snapshot and snapshot.get("version") is not None:
            from center_messenger import card_preference
            card_preference.refresh(db, workspace, snapshot, refreshed_families)

        logging.getLogger(__name__).debug(
            "AlfaCRM MAX history projected workspace=%s rows=%d touched_convs=%d",
            workspace,
            len(rows),
            len(touched),
        )

    def send_target(self, workspace, connection, conversation):
        with self.accounts.transaction(workspace_id=workspace) as db:
            return self._send_target_in_transaction(db, workspace, connection, conversation)

    def _send_target_in_transaction(self, db, workspace, connection, conversation):
        row = self._row(db, workspace)
        if row and row['state'] == 'not_configured' and control(row).get('event') == 'manual_disconnect':
            raise AlfaBrowserError('disconnected')
        if (
            row is None
            or row["connection_id"] != connection
            or row["state"] != "active"
            or row["current_crm_connection_id"] is None
            or row["session_ciphertext"] is None
        ):
            raise AlfaBrowserError("reauth_required")
        target = db.execute(
            text(
                "SELECT m.payload FROM conversations c JOIN messages m ON "
                "m.workspace_id=c.workspace_id AND m.conversation_id=c.id "
                "WHERE c.workspace_id=:w AND c.id=:conv AND m.direction='in' "
                "AND c.connection_id=:connection AND m.actor_type='contact' "
                "AND m.payload->>'customer_id' IS NOT NULL "
                "AND m.payload->>'customer_branch_id' IS NOT NULL "
                "AND " + visible_message('m') + " "
                "AND coalesce(m.payload->>'crm_mirror','false')<>'true' "
                "ORDER BY " + message_order(descending=True) + " LIMIT 1"
            ),
            {"w": workspace, "conv": conversation, "connection": connection},
        ).scalar_one_or_none()
        if not target:
            raise AlfaBrowserError("history_unavailable")
        peer = target.get("max_user_id")
        if peer is not None and (
            not isinstance(peer, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", peer)
        ):
            raise AlfaBrowserError("invalid_recipient")
        if peer is None:
            # AlfaCRM need not repeat explicit peer metadata in every journal
            # row. Siblings may have separate CRM Customer cards. Reuse only
            # explicit incoming evidence from this card or the same proven CRM
            # family contact; a matching phone number is never sufficient.
            family = target.get("crm_family_contacts") or []
            if not isinstance(family, list) or any(not isinstance(value, str) for value in family):
                family = []
            peers = set(db.execute(
                text(
                    "SELECT DISTINCT m.payload->>'max_user_id' "
                    "FROM conversations c JOIN messages m ON "
                    "m.workspace_id=c.workspace_id AND m.conversation_id=c.id "
                    "WHERE c.workspace_id=:w AND c.id=:conv AND c.connection_id=:connection "
                    "AND m.direction='in' AND m.actor_type='contact' "
                    "AND ((m.payload->>'customer_id'=:customer "
                    "AND m.payload->>'customer_branch_id'=:branch) "
                    "OR (cardinality(CAST(:family AS text[]))>0 "
                    "AND jsonb_typeof(m.payload->'crm_family_contacts')='array' "
                    "AND jsonb_exists_any(m.payload->'crm_family_contacts',CAST(:family AS text[])))) "
                    "AND " + visible_message('m') + " "
                    "AND coalesce(m.payload->>'crm_mirror','false')<>'true' "
                    "AND m.payload->>'max_user_id' IS NOT NULL"
                ),
                {
                    "w": workspace,
                    "conv": conversation,
                    "customer": str(target["customer_id"]),
                    "branch": str(target["customer_branch_id"]),
                    "family": family,
                    "connection": connection,
                },
            ).scalars())
            if any(not re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in peers):
                raise AlfaBrowserError("invalid_recipient")
            if len(peers) > 1:
                raise AlfaBrowserError("multiple_recipients")
            peer = next(iter(peers), None)
        phone = None
        snapshot = DirectoryReader._published(db, workspace) if row.get('crm_connection_id') else None
        if snapshot and snapshot['connection_id'] == row['current_crm_connection_id']:
            directory = customer_directory(db, workspace, snapshot, [int(target['customer_id'])])
            original_id = target['customer_id']
            target = native_relink.reply_target(db, workspace, connection, snapshot, target, directory)
            if target['customer_id'] != original_id:
                directory = customer_directory(db, workspace, snapshot, [int(target['customer_id'])])
            if peer is None:
                # Use this exact CRM contact's native phone row. A different
                # Customer ID is allowed only by an exact-source relink witness
                # above; a display preference or matching phone is insufficient.
                resolved = resolve_customer({'customer_id': int(target['customer_id']),
                    'branch_id': int(target['customer_branch_id'])}, directory)
                family = target.get('crm_family_contacts') or []
                if (resolved.get('contact_id') and resolved['branch_id'] == int(target['customer_branch_id'])
                        and (resolved.get('global_id_verified') or str(resolved['contact_id']) in family)):
                    phone = resolved.get('primary_phone')
        if target.get('crm_recipient_stale'):
            raise AlfaBrowserError('invalid_recipient')
        session = self.vault.open_browser_state(workspace, connection, row["session_ciphertext"])
        return {
            "origin": row["crm_origin"],
            "state": session,
            "branch": int(target["customer_branch_id"]),
            "customer": int(target["customer_id"]),
            "max_user_id": peer,
            "contact_phone": phone,
            "session_snapshot": dict(row),
        }

    def send_native(self, workspace, connection, conversation, message, *, target=None,
                    before_post=None, on_accepted=None, heartbeat=None, stopping=None):
        target = target or self.send_target(workspace, connection, conversation)
        with self.sessions.operation(workspace, connection, target.get('session_snapshot'), purpose='send') as operation:
            if control(operation['row']).get('needs_check'):
                raise AlfaBrowserError('session_check_pending')
            def guard(callback=None):
                if callback:
                    callback(operation)
                else:
                    with self.accounts.transaction(workspace_id=workspace) as db:
                        self.sessions.renew(db, operation, force=True)
            try:
                result = self.browser.send(target["origin"], target["state"], target["branch"], target["customer"], message,
                    expected_peer=target.get("max_user_id"),
                    expected_phone=target.get('contact_phone'),
                    before_post=lambda: guard(before_post), on_accepted=on_accepted,
                    heartbeat=lambda: guard(heartbeat), stopping=stopping)
            except AlfaBrowserError as error:
                if error.code in {'reauth_required', 'otp_required'}:
                    partial = getattr(error, 'browser_state', None)
                    if isinstance(partial, dict) and isinstance(partial.get('cookies'), list):
                        self.sessions.finish(operation, partial, state='active', phase='checking', needs_check=True)
                    with self.accounts.transaction(workspace_id=workspace) as db:
                        self.sessions.suspect(db, workspace, operation['row'], 'send')
                    raise AlfaBrowserError('session_check_pending') from None
                raise
            if isinstance(result.get('browser_state'), dict):
                try:
                    self.sessions.finish(operation, result['browser_state'], state='active')
                except Exception:
                    logging.getLogger(__name__).warning('Native send accepted; session save deferred workspace=%s', workspace)
            return result


class AlfaMaxWorker:
    def __init__(self, service):
        self.service, self.accounts = service, service.accounts
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="alfa-max-sync", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def _run(self):
        while not self.stopping.is_set():
            try:
                if not self.run_next():
                    self.stopping.wait(1.5)
            except Exception:
                logging.getLogger(__name__).warning("AlfaCRM MAX sync worker iteration failed")
                self.stopping.wait(3)

    def _claim(self):
        lease = uuid4()
        with self.accounts.transaction() as db:
            row = (
                db.execute(
                    text(
                        "WITH due AS (SELECT workspace_id FROM alfa_max_sync_queue "
                        "WHERE due_at<=clock_timestamp() AND lease_until<clock_timestamp() "
                        "ORDER BY due_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                        "UPDATE alfa_max_sync_queue q SET lease_owner=:lease,"
                        "lease_until=clock_timestamp()+interval '300 seconds' FROM due "
                        "WHERE q.workspace_id=due.workspace_id "
                        "RETURNING q.workspace_id,q.connection_id"
                    ),
                    {"lease": lease},
                )
                .mappings()
                .one_or_none()
            )
        return (dict(row), lease) if row else (None, None)

    @contextmanager
    def _transaction(self, workspace, connection, lease):
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            queue = db.execute(
                text(
                    "SELECT workspace_id FROM alfa_max_sync_queue WHERE workspace_id=:w "
                    "AND connection_id=:c AND lease_owner=:lease "
                    "AND lease_until>clock_timestamp() FOR UPDATE"
                ),
                {"w": workspace, "c": connection, "lease": lease},
            ).scalar_one_or_none()
            if queue is None:
                raise AlfaBrowserError("settings_changed")
            yield db

    @staticmethod
    def _release(db, workspace, connection, lease, delay):
        db.execute(
            text(
                "UPDATE alfa_max_sync_queue SET lease_owner=NULL,lease_until='-infinity',"
                "due_at=now()+(:delay*interval '1 second') WHERE workspace_id=:w AND "
                "connection_id=:c AND lease_owner=:lease"
            ),
            {"w": workspace, "c": connection, "lease": lease, "delay": delay},
        )

    @staticmethod
    def _activity(db, workspace, connection, value):
        value = dict(value, updated_at=datetime.now(UTC).isoformat())
        db.execute(text(
            "UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,'{activity}',CAST(:value AS jsonb)) "
            "WHERE workspace_id=:w AND connection_id=:c"
        ), {"w": workspace, "c": connection, "value": json.dumps(value)})

    def _heartbeat(self, workspace, connection, lease, activity=None):
        # Many branches/pages can take longer than one lease. Renew between
        # browser navigations without keeping a database transaction open.
        with self._transaction(workspace, connection, lease) as db:
            db.execute(
                text("UPDATE alfa_max_sync_queue SET lease_until=clock_timestamp()+interval '300 seconds' "
                     "WHERE workspace_id=:w AND connection_id=:c AND lease_owner=:lease"),
                {"w": workspace, "c": connection, "lease": lease},
            )
            if activity is not None:
                self.service.monitor.update(workspace, connection, lease, activity)
                self._activity(db, workspace, connection, self.service.monitor.snapshot(workspace, connection, lease) or activity)

    def run_next(self):
        import time

        claim, lease = self._claim()
        if not claim:
            return False
        workspace, connection = claim["workspace_id"], claim["connection_id"]
        delay, code = 90, None
        io_lease = None
        session_snapshot = None
        phase = "load_settings"
        sync_start = time.monotonic()
        activity = {"started_at": datetime.now(UTC).isoformat(), "phase": "repair"}
        observe = lambda value: self.service.monitor.update(workspace, connection, lease, value)
        snapshot = lambda committed=False: self.service.monitor.snapshot(workspace, connection, lease, committed=committed) or dict(activity)
        try:
            with self._transaction(workspace, connection, lease) as db:
                pending = self.service._row(db, workspace)
                if pending and control(pending).get('lease') and stamp(control(pending).get('until')) > datetime.now(UTC):
                    # Busy polling must not repeat directory repair or decryption.
                    self._release(db, workspace, connection, lease, BUSY_RETRY)
                    return True
                recovery_due = bool(pending and pending["session_ciphertext"] and
                    (pending["state"] == "reauth_required" or control(pending).get("needs_check")))
            if recovery_due:
                delay = self.service.sessions.recover(workspace, connection)
                with self._transaction(workspace, connection, lease) as db:
                    self._release(db, workspace, connection, lease, delay)
                return True
            with self._transaction(workspace, connection, lease) as db:
                row = self.service._row(db, workspace)
                needs_recovery = bool(row and (row["state"] == "reauth_required" or control(row).get("needs_check")))
                if (
                    row is None
                    or row["connection_id"] != connection
                    or (row["state"] != "active" and not needs_recovery)
                    or row["session_ciphertext"] is None
                    or row["crm_origin"] is None
                ):
                    self._release(db, workspace, connection, lease, 300)
                    return True
                crm_connection = row["crm_connection_id"]
                branches = list(db.execute(text(
                    "SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                    "AND connection_id=:crm AND available ORDER BY external_id"),
                    {"w":workspace,"crm":crm_connection}).scalars())
                self.service.monitor.begin(workspace, connection, lease, (row["history_cursor"] or {}).get("activity"), activity)
                phase = "repair_saved_history"
                repair_cursor = adopt_history(row["history_cursor"] or {}, branches)
                identity_jobs = list(db.execute(text("SELECT id,payload FROM outbox_events WHERE workspace_id=:w "
                    "AND topic='crm.identity.repair' AND state='pending' AND payload->>'connection'=:crm "
                    "ORDER BY created_at LIMIT 20 FOR UPDATE SKIP LOCKED"), {"w": workspace, "crm": str(crm_connection)}).mappings())
                if identity_jobs:
                    repair_batch(self.service, db, workspace, connection, dict(repair_cursor), on_progress=observe,
                        only_contacts=[UUID(j["payload"]["contact"]) for j in identity_jobs])
                    db.execute(text("UPDATE outbox_events SET state='published' WHERE workspace_id=:w "
                        "AND id=ANY(CAST(:ids AS uuid[]))"), {"w": workspace, "ids": [j["id"] for j in identity_jobs]})
                if repair_batch(self.service, db, workspace, connection, repair_cursor, on_progress=observe):
                    repair_cursor["activity"] = snapshot(True)
                    db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) "
                                    "WHERE workspace_id=:w"), {"w": workspace, "cursor": json.dumps(repair_cursor)})
                    row = dict(row) | {"history_cursor": repair_cursor}
                    repair = repair_cursor["client_repair"]
                    # Repair saved dialogs promptly, but interleave live CRM
                    # reads so existing message cleanup cannot starve new ones.
                    if not repair["complete"] and repair["processed"] % (REPAIR_BATCH * 3):
                        self._release(db, workspace, connection, lease, 1)
                        return True
                phase = "open_session"
                session = self.service.vault.open_browser_state(
                    workspace, connection, row["session_ciphertext"]
                )
                # Read cursor early (needed to build requests before browser IO),
                # but re-read + version-check in final transaction.
                original_cursor = dict(row["history_cursor"] or {})
                if original_cursor.get("retry_requests"):
                    original_cursor = retry_history(original_cursor, list(original_cursor["retry_requests"]))
                    original_cursor.pop("retry_requests", None)
                    db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) "
                                    "WHERE workspace_id=:w"), {"w": workspace, "cursor": json.dumps(original_cursor)})
                # Preserve completed imports; only unfinished pages and new records are planned.
                cursor, requests = plan_history(original_cursor, branches)
                # Publish durable due times before arbitration, without moving pages.
                db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) "
                                "WHERE workspace_id=:w"), {"w": workspace, "cursor": json.dumps(cursor)})
                expected_cursor = dict(cursor)
                origin = row["crm_origin"]
                settings_version = row["version"]
                session_snapshot = dict(row)
                crm_settings_version = row.get("crm_settings_version")
                activity.update(phase="reading", operation="read_page", flow="live", client=None, record_id=None,
                                position=None, position_total=None, completed_pages=0, total_pages=len(requests))
                observe(activity)
                self._activity(db, workspace, connection, snapshot(True))
            self.service.monitor.committed(workspace, connection, lease)
            if not branches:
                raise AlfaBrowserError("settings_changed")
            phase = "read_history"
            read_step = 0
            processed = set()
            saved_pages = 0
            controls = {"archive_paused", "archive_pause_changed_at", "retry_requests", CONTROL}
            semantic = lambda value: {k: v for k, v in value.items() if k not in controls | {"activity"}}
            cycle = dict(cursor.get('live_cycle') or {})
            # Old releases did not persist per-journal membership of a cycle.
            if not cycle.get('targets'):
                cycle = {}
            if not cycle and any(r[2] == 0 for r in requests):
                cycle = {"id": str(uuid4()), "started_at": datetime.now(UTC).isoformat(),
                         "journals": len(branches) * 2, "checked": 0, "failed": 0, "added_records": 0,
                         "targets": list(cursor['streams']), "checked_keys": [], "failed_keys": []}
            if cycle:
                cycle['targets'] = [k for k in cycle['targets'] if k in cursor['streams']]
                cycle['journals'] = len(cycle['targets'])
            yielded = False
            def flow_for(request):
                return history_flow(cursor, request)

            def before_read(request):
                nonlocal read_step, yielded
                read_step = requests.index(request)
                if yielded:
                    return False
                with self._transaction(workspace, connection, lease) as db:
                    current = self.service._row(db, workspace)
                    if (not current or current["version"] != settings_version or current["state"] != "active"
                        or current["current_crm_connection_id"] != crm_connection or current["crm_origin"] != origin
                        or current.get("crm_settings_version") != crm_settings_version):
                        raise AlfaBrowserError("settings_changed")
                    paused = bool((current["history_cursor"] or {}).get("archive_paused"))
                    if paused and flow_for(request) == 'archive':
                        return False
                    if io_lease and (saved_pages >= 16 or not self.service.sessions.can_read(db, io_lease, flow_for(request))):
                        yielded = True
                        return False
                return True

            def heartbeat():
                nonlocal read_step
                current_activity = activity | {"completed_pages": min(read_step, len(requests))}
                if read_step < len(requests):
                    branch, direction, page = requests[read_step]
                    current_activity.update(branch_id=str(branch), direction=direction, page=page,
                                            flow=flow_for(requests[read_step]))
                else:
                    current_activity["phase"] = "saving"
                self._heartbeat(workspace, connection, lease, current_activity)
                read_step += 1
            def merge_controls(current):
                nonlocal cursor
                for key in controls:
                    if key in current:
                        cursor[key] = current[key]
                    else:
                        cursor.pop(key, None)

            def check_current(current):
                if (current is None or current["connection_id"] != connection or current["state"] != "active"
                    or current["session_ciphertext"] is None or current["version"] != settings_version
                    or current["current_crm_connection_id"] != crm_connection or current["crm_origin"] != origin
                    or current.get("crm_settings_version") != crm_settings_version
                    or (session_snapshot is not None and not same_session(current, session_snapshot))
                    or semantic(current["history_cursor"] or {}) != semantic(expected_cursor)):
                    raise AlfaBrowserError("settings_changed")
                merge_controls(current["history_cursor"] or {})

            def persist_page(request, result, browser_state):
                nonlocal expected_cursor, phase, saved_pages
                processed.add(request)
                if result.get("skipped"):
                    return
                branch, direction, page = request
                flow = flow_for(request)
                phase = "persist_history_page"
                records = []
                if not result.get("error"):
                    with self.accounts.transaction(workspace_id=workspace, snapshot=True) as db:
                        records = new_records(db, workspace, connection, branch, result["messages"])
                    new_ids = {(m["direction"], str(m["record_id"])) for m in records}
                    result["known_ids"] = [str(m["record_id"]) for m in result["messages"]
                                           if (m["direction"], str(m["record_id"])) not in new_ids]
                if any(m["actor_type"] == "automation" for m in records):
                    observe({"phase": "templates", "operation": "templates", "flow": flow, "client": None})
                    self.service.sms_templates(origin, browser_state, branches[0],
                        scope=(str(workspace), str(crm_connection), settings_version, branches[0]))
                added = 0
                def page_event(value):
                    nonlocal added
                    if (value.get("event") or {}).get("result") == "imported":
                        added += 1
                    observe(value)
                observe({"phase": "waiting_db", "operation": "waiting_db", "flow": flow, "client": None})
                with self._transaction(workspace, connection, lease) as db:
                    if io_lease:
                        self.service.sessions.renew(db, io_lease)
                    check_current(self.service._row(db, workspace))
                    previous = dict(cursor)
                    observe({"phase": "projecting", "branch_id": str(branch), "direction": direction,
                             "page": page, "flow": flow, "client": None, "record_id": None, "position": None})
                    if not result.get("error"):
                        self.service._project_page(db, workspace, connection, crm_connection, branch, records,
                                                   on_progress=page_event)
                        verify_saved_pages(db, workspace, connection, [request], [result])
                    record_pages(db, workspace, connection, previous, cursor, [request], [result])
                    advance_history(cursor, [request], [result],
                                    allow_archive=bool(cursor.get(f"scan_id:{branch}:{direction}")))
                    finish_pages(db, workspace, connection, previous, cursor)
                    if cycle and page == 0:
                        key = f'{branch}:{direction}'
                        for name in ('checked_keys', 'failed_keys'):
                            cycle[name] = [k for k in cycle.get(name, []) if k != key and k in cycle['targets']]
                        if key in cycle['targets']:
                            cycle['failed_keys' if result.get('error') else 'checked_keys'].append(key)
                        cycle['checked'] = len(cycle['checked_keys'])
                        cycle['failed'] = len(cycle['failed_keys'])
                    if cycle and flow != "archive":
                        cycle["added_records"] += added
                    if cycle:
                        cursor["live_cycle"] = dict(cycle)
                    self.service.sessions.page_finished(cursor, flow)
                    cursor["activity"] = snapshot(True)
                    status_error = next((cursor.get(f"{prefix}:{key}") for key in cursor["streams"]
                        for prefix in ("live_error", "error") if cursor.get(f"{prefix}:{key}")), None)
                    encrypted = self.service.vault.seal_browser_state(workspace, connection, merge_state(session, browser_state))
                    db.execute(text(
                        "UPDATE alfa_max_settings SET session_ciphertext=:session,history_cursor=CAST(:cursor AS jsonb),"
                        "last_sync_at=CASE WHEN :success THEN now() ELSE last_sync_at END,"
                        "last_error_code=:error,updated_at=now() WHERE workspace_id=:w"),
                        {"w": workspace, "session": encrypted,
                         "cursor": json.dumps(cursor), "success": not result.get("error"), "error": status_error})
                session_snapshot['session_ciphertext'] = encrypted
                if io_lease:
                    io_lease['row']['session_ciphertext'] = encrypted
                    io_lease['pages'] = saved_pages + 1
                expected_cursor = json.loads(json.dumps(cursor))
                saved_pages += 1
                self.service.monitor.committed(workspace, connection, lease)
                if records:
                    self.service._progress_cache.clear()
                if result.get("missing_ids"):
                    logging.getLogger(__name__).error(
                        "AlfaCRM MAX projection gap workspace=%s branch=%s direction=%s page=%s missing_ids=%s",
                        workspace, *request, result["missing_ids"])

            if requests:
                io_lease = self.service.sessions.claim(workspace, connection, session_snapshot, purpose='history')
                # An aged background page may be granted a bounded fair turn.
                requests.sort(key=lambda request: flow_for(request) != io_lease['kind'])
                initial_session = session
                results, renewed_session = self.service.browser.read_histories(
                    origin, session, requests, on_progress=heartbeat, continue_on_error=True,
                    before_read=before_read, on_result=persist_page)
                session = merge_state(initial_session, renewed_session)
                # Adapters without streaming callbacks still use the same fenced
                # per-page persistence; the browser implementation streams them.
                for request, result in zip(requests, results, strict=True):
                    if request not in processed and before_read(request):
                        persist_page(request, result, session)
            phase = "finish_cycle"
            observe({"phase": "waiting", "operation": "waiting", "client": None,
                     "completed_pages": saved_pages})
            with self._transaction(workspace, connection, lease) as db:
                check_current(self.service._row(db, workspace))
                if cycle and set(cycle['targets']) <= set(cycle.get('checked_keys', [])) | set(cycle.get('failed_keys', [])):
                    cycle["completed_at"] = datetime.now(UTC).isoformat()
                    cursor["last_live_cycle"] = dict(cycle)
                    cursor.pop("live_cycle", None)
                cursor["activity"] = snapshot(True) | {"phase": "waiting", "operation": "waiting", "client": None}
                if io_lease:
                    self.service.sessions.renew(db, io_lease)
                    db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb),"
                                    "session_ciphertext=:session WHERE workspace_id=:w"),
                        {"w": workspace, "cursor": json.dumps(cursor),
                         "session": self.service.vault.seal_browser_state(workspace, connection, session)})
                else:
                    # No browser operation: do not rotate cookies under an active sender.
                    db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) WHERE workspace_id=:w"),
                               {"w": workspace, "cursor": json.dumps(cursor)})
                delay = 0 if cursor.get("retry_requests") else next_history_delay(cursor)
                self._release(db, workspace, connection, lease, delay)
        except AlfaBrowserError as error:
            code = error.code
            delay = (
                BUSY_RETRY
                if code in {"browser_busy", "browser_priority_wait"}
                else 30
                if code == "settings_changed"
                else 60
                if code in {"network", "crm_unavailable"}
                else 300
            )
            try:
                self.service.monitor.rollback(workspace, connection, lease)
                waiting = code in {'browser_busy', 'browser_priority_wait'}
                observe({"phase": "waiting" if waiting else "error", "operation": "waiting" if waiting else "error",
                         "client": None, "error_code": None if waiting else code})
                with self._transaction(workspace, connection, lease) as db:
                    current = self.service._row(db, workspace)
                    if session_snapshot is not None and not same_session(current, session_snapshot):
                        self._release(db, workspace, connection, lease, 0)
                        return True
                    self._activity(db, workspace, connection, snapshot())
                    if code in {"reauth_required", "otp_required"} and session_snapshot is not None:
                        if self.service.sessions.suspect(db, workspace, session_snapshot, 'history'):
                            delay = 0
                    elif not waiting and (session_snapshot is None or same_session(current, session_snapshot)):
                        db.execute(text("UPDATE alfa_max_settings SET last_error_code=:code,updated_at=now() "
                                        "WHERE workspace_id=:w AND connection_id=:c"),
                                   {"w": workspace, "c": connection, "code": code})
                    self._release(db, workspace, connection, lease, delay)
            except Exception:
                pass
        except Exception as error:
            import traceback
            code, delay = "internal", 120
            try:
                self.service.monitor.rollback(workspace, connection, lease)
                observe({"phase": "error", "operation": "error", "client": None, "error_code": code})
                with self._transaction(workspace, connection, lease) as db:
                    current = self.service._row(db, workspace)
                    if session_snapshot is not None and not same_session(current, session_snapshot):
                        self._release(db, workspace, connection, lease, 0)
                        return True
                    self._activity(db, workspace, connection, snapshot())
                    db.execute(
                        text(
                            "UPDATE alfa_max_settings SET last_error_code=:code,updated_at=now() "
                            "WHERE workspace_id=:w"
                        ),
                        {"w": workspace, "code": code},
                    )
                    self._release(db, workspace, connection, lease, delay)
            except Exception:
                pass
            if isinstance(error, SQLAlchemyError):
                log_database_error(logging.getLogger(__name__), error, operation=f"alfa_max_sync:{phase}")
            else:
                logging.getLogger(__name__).error(
                    "AlfaCRM MAX history sync failed phase=%s exception=%s delay=%d\n%s",
                    phase, type(error).__name__, delay, traceback.format_exc(),
                )
        finally:
            self.service.sessions.release(io_lease)
            self.service.monitor.close(workspace, connection, lease)
        duration = time.monotonic() - sync_start
        logging.getLogger(__name__).info(
            "AlfaCRM MAX sync completed workspace=%s connection=%s duration=%.1fs",
            workspace,
            connection,
            duration,
        )
        return True
