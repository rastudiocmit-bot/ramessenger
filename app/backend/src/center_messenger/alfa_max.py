"""Independent AlfaCRM native-MAX connection and durable inbox synchronization."""

import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser
from center_messenger.auth_security import AuthError
from center_messenger.chat_store import changed, refresh_unread
from center_messenger.crm_api import DirectoryReader
from center_messenger.message_order import message_order, source_order, visible_message

ERRORS = {
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
    "settings_changed": "Настройки AlfaCRM изменились. Подключите веб-сессию заново.",
    "internal": "Не удалось обновить историю AlfaCRM MAX. Повторите позже.",
}


def _failure(code, status=502):
    return AuthError(status, ERRORS.get(code, ERRORS["internal"]))


def _source_record_id(item, branch):
    return f"{item['direction']}:{branch}:{item['record_id']}"


def _history_page_batch(current, target_page, older=None):
    """Return pages to import and the next older page cursor."""
    pages = [current]
    next_page = 1 if current["has_more"] else 0
    if target_page > 0:
        if older is None:
            raise ValueError("Older page result is required")
        pages.append(older)
        next_page = target_page + 1 if older["has_more"] else 0
    return pages, next_page


class AlfaMaxConnections:
    def __init__(self, alfa, browser=None):
        self.alfa, self.accounts, self.vault = alfa, alfa.accounts, alfa.vault
        self.browser = browser or AlfaMaxBrowser()
        self._sms_templates = []
        self._sms_templates_at = None

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

    def sms_templates(self, origin, state, branch):
        now = datetime.now(UTC)
        if (
            self._sms_templates
            and self._sms_templates_at
            and now - self._sms_templates_at < timedelta(minutes=10)
        ):
            return self._sms_templates
        try:
            self._sms_templates = self.browser.read_sms_templates(origin, state, branch)
            self._sms_templates_at = now
        except AlfaBrowserError:
            # Keep the previous cache when the CRM is briefly unavailable.
            pass
        return self._sms_templates

    @contextmanager
    def _transaction(self, actor, workspace, *, lock=False):
        with self.alfa._transaction(actor, workspace, lock=lock) as db:
            yield db

    @staticmethod
    def _row(db, workspace):
        return (
            db.execute(
                text(
                    "SELECT m.*,c.name,c.status AS channel_status,a.origin AS crm_origin,"
                    "a.connection_id AS current_crm_connection_id "
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
                and row["session_ciphertext"] is not None
                and row["current_crm_connection_id"] is not None
            ),
            "kind": "alfa_max_browser",
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

    def progress(self, actor, workspace):
        with self._transaction(actor, workspace) as db:
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
            queued = db.execute(
                text(
                    "SELECT 1 FROM alfa_max_sync_queue WHERE workspace_id=:w "
                    "AND lease_owner IS NULL AND due_at<=clock_timestamp() LIMIT 1"
                ),
                {"w": workspace},
            ).scalar_one_or_none()
            counts = db.execute(
                text(
                    "SELECT (SELECT count(*) FROM conversations WHERE workspace_id=:w) "
                    "AS conversations, (SELECT count(*) FROM messages WHERE workspace_id=:w) "
                    "AS messages"
                ),
                {"w": workspace},
            ).mappings().one()
            return {
                "configured": True,
                "state": row["state"],
                "syncing": queued is not None,
                "messages": counts["messages"],
                "conversations": counts["conversations"],
                "cursor": cursor,
                "last_sync_at": row["last_sync_at"],
            }

    def _ensure_channel(self, actor, workspace, email, crm_connection):
        with self._transaction(actor, workspace, lock=True) as db:
            existing = self._row(db, workspace)
            if existing:
                db.execute(
                    text("DELETE FROM alfa_max_sync_queue WHERE workspace_id=:w"),
                    {"w": workspace},
                )
                connection = existing["connection_id"]
                version = db.execute(
                    text(
                        "UPDATE alfa_max_settings SET email=:e,crm_connection_id=:crm,"
                        "state='not_configured',session_ciphertext=NULL,version=version+1,"
                        "last_error_code=NULL,history_cursor='{}'::jsonb,updated_at=now() "
                        "WHERE workspace_id=:w AND connection_id=:c RETURNING version"
                    ),
                    {"w": workspace, "c": connection, "e": email, "crm": crm_connection},
                ).scalar_one()
                db.execute(
                    text(
                        "UPDATE connections SET status='not_configured',name='MAX через AlfaCRM' "
                        "WHERE workspace_id=:w AND id=:c"
                    ),
                    {"w": workspace, "c": connection},
                )
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

    def connect(self, actor, workspace, email, password):
        self.accounts.throttle(f"alfa-max-login:{workspace}:{actor.user_id}", limit=8, minutes=10)
        if not self.vault.ready:
            raise AuthError(503, "Ключ хранилища веб-сессий недоступен на сервере.")
        crm, _ = self._crm(actor, workspace)
        connection, version = self._ensure_channel(actor, workspace, email, crm["connection_id"])
        try:
            state, browser_state = self.browser.login(
                crm["origin"], email, password.get_secret_value()
            )
        except AlfaBrowserError as error:
            self._set_error(actor, workspace, connection, version, error.code)
            raise _failure(
                error.code, 401 if error.code == "credentials_rejected" else 502
            ) from None
        encrypted = self.vault.seal_browser_state(workspace, connection, browser_state)
        with self._transaction(actor, workspace, lock=True) as db:
            current = self._row(db, workspace)
            if (
                current is None
                or current["connection_id"] != connection
                or current["version"] != version
            ):
                raise AuthError(409, "Подключение изменилось. Начните настройку заново.")
            db.execute(
                text(
                    "UPDATE alfa_max_settings SET state=:state,session_ciphertext=:session,"
                    "version=version+1,last_error_code=NULL,updated_at=now() WHERE workspace_id=:w"
                ),
                {"w": workspace, "state": state, "session": encrypted},
            )
            db.execute(
                text("UPDATE connections SET status=:status WHERE workspace_id=:w AND id=:c"),
                {
                    "w": workspace,
                    "c": connection,
                    "status": "active" if state == "active" else "connecting",
                },
            )
            if state == "active":
                self._schedule(db, workspace, connection)
            audit(
                db,
                "alfa_max_web_connected" if state == "active" else "alfa_max_otp_requested",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )
        return self.status(actor, workspace)

    def verify_otp(self, actor, workspace, code):
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
            raise _failure(error.code, 422 if error.code == "otp_rejected" else 502) from None
        encrypted = self.vault.seal_browser_state(workspace, row["connection_id"], browser_state)
        with self._transaction(actor, workspace, lock=True) as db:
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
            audit(
                db,
                "alfa_max_web_connected",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=row["connection_id"],
            )
        return self.status(actor, workspace)

    def _set_error(self, actor, workspace, connection, version, code):
        with self._transaction(actor, workspace, lock=True) as db:
            changed_row = db.execute(
                text(
                    "UPDATE alfa_max_settings SET state='error',session_ciphertext=NULL,"
                    "last_error_code=:code,version=version+1,updated_at=now() "
                    "WHERE workspace_id=:w AND connection_id=:c AND version=:v"
                ),
                {"w": workspace, "c": connection, "code": code, "v": version},
            ).rowcount
            if not changed_row:
                return
            db.execute(
                text("UPDATE connections SET status='error' WHERE workspace_id=:w AND id=:c"),
                {"w": workspace, "c": connection},
            )

    @staticmethod
    def _schedule(db, workspace, connection, delay=0):
        db.execute(
            text(
                "INSERT INTO alfa_max_sync_queue(workspace_id,connection_id,due_at) "
                "VALUES (:w,:c,now()+(:delay*interval '1 second')) ON CONFLICT(workspace_id) "
                "DO UPDATE SET connection_id=EXCLUDED.connection_id,due_at=EXCLUDED.due_at,"
                "lease_owner=NULL,lease_until='-infinity'"
            ),
            {"w": workspace, "c": connection, "delay": delay},
        )

    def sync(self, actor, workspace):
        with self._transaction(actor, workspace, lock=True) as db:
            row = self._row(db, workspace)
            if row is None or row["state"] != "active":
                raise AuthError(409, "Сначала подключите веб-сессию AlfaCRM MAX.")
            self._schedule(db, workspace, row["connection_id"])
            audit(
                db,
                "alfa_max_sync_requested",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=row["connection_id"],
            )
        return {"queued": True}

    def disconnect(self, actor, workspace):
        with self._transaction(actor, workspace, lock=True) as db:
            row = self._row(db, workspace)
            if row is None:
                return
            db.execute(
                text(
                    "UPDATE alfa_max_settings SET state='not_configured',session_ciphertext=NULL,"
                    "last_error_code=NULL,version=version+1,updated_at=now() WHERE workspace_id=:w"
                ),
                {"w": workspace},
            )
            db.execute(
                text(
                    "UPDATE connections SET status='not_configured' WHERE workspace_id=:w AND id=:c"
                ),
                {"w": workspace, "c": row["connection_id"]},
            )
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
            identity = f"alfa-customer:{branch}:{customer['customer_id']}"
        else:
            identity = f"alfa-unlinked:{branch}"
        conversation_id = uuid5(connection, identity)
        display = customer["name"] if customer and customer.get("name") else "Клиент не определён"
        return identity, conversation_id, display

    @staticmethod
    def _supersede(db, workspace, old, canonical):
        db.execute(
            text("UPDATE messages SET provider_message_id=NULL,"
                 "payload=jsonb_set(payload,'{superseded_by}',to_jsonb(CAST(:canonical AS text))),"
                 "version=version+1 WHERE workspace_id=:w AND id=:old"),
            {"w": workspace, "old": old, "canonical": str(canonical)},
        )
        db.execute(
            text("UPDATE source_records SET message_id=:canonical "
                 "WHERE workspace_id=:w AND message_id=:old"),
            {"w": workspace, "old": old, "canonical": canonical},
        )

    def _merge_contact_conversations(self, db, workspace, channel, contact, canonical):
        """Unify only an already verified CRM contact in this one connection.

        Retain the old conversation IDs as aliases. All messages, pending sends
        and manual handling state survive; only proven provider-ID echoes hide.
        The worker holds the workspace lock throughout this operation.
        """
        old = list(db.execute(
            text("SELECT c.id FROM conversations c WHERE c.workspace_id=:w "
                 "AND c.connection_id=:channel AND c.contact_id=:contact AND c.id<>:canonical "
                 "AND (EXISTS(SELECT 1 FROM messages m WHERE m.workspace_id=c.workspace_id "
                 "AND m.conversation_id=c.id) OR EXISTS(SELECT 1 FROM send_commands s "
                 "WHERE s.workspace_id=c.workspace_id AND s.conversation_id=c.id)) "
                 "ORDER BY c.id FOR UPDATE"),
            {"w": workspace, "channel": channel, "contact": contact, "canonical": canonical},
        ).scalars())
        if not old:
            return
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
                     "AND n.provider_message_id=m.provider_message_id AND n.conversation_id=:canonical "
                     "WHERE m.workspace_id=:w AND m.conversation_id=:old"),
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
                text("UPDATE conversations SET handled_inbound_id=:message,handled_at=:at,handled_by=:user "
                     "WHERE workspace_id=:w AND id=:id"),
                {"w": workspace, "id": canonical, "message": handled["handled_inbound_id"],
                 "at": handled["handled_at"], "user": handled["handled_by"]},
            )

    def _project_page(self, db, workspace, channel, crm_connection, branch, rows):
        snapshot = DirectoryReader._published(db, workspace)
        touched = set()
        merged_contacts = set()
        for item in rows:
            customers = {}
            for raw_customer in item.get("customers") or []:
                customer = dict(raw_customer) if raw_customer else None
                if customer and snapshot:
                    customer["contact_id"] = db.execute(
                        text(
                            "SELECT contact_id FROM alfa_customers WHERE workspace_id=:w "
                            "AND run_id=:r AND branch_id=:branch AND external_id=:id "
                            "AND contact_id IS NOT NULL"
                        ),
                        {"w": workspace, "r": snapshot["run"], "branch": customer["branch_id"],
                         "id": customer["customer_id"]},
                    ).scalar_one_or_none()
                customers.setdefault("single", []).append(customer)
            if not customers:
                customers["single"] = [None]
            for group, records in customers.items():
                # One conversation per parent: prefer a linked card, else the same branch.
                customer = min(
                    records,
                    key=lambda value: (
                        value is None,
                        (value.get("contact_id") is None) if value else True,
                        value["branch_id"] != int(branch) if value else True,
                        value["branch_id"] if value else 0,
                        value["customer_id"] if value else 0,
                    ),
                )
                identity, conversation_id, display = self._message_identity(
                    UUID(str(channel)), customer, branch
                )
                external = identity
                conversation_id = db.execute(
                    text(
                        "INSERT INTO conversations(workspace_id,id,connection_id,contact_id,"
                        "external_chat_id,peer_name) VALUES (:w,:id,:c,:contact,:external,:name) "
                        "ON CONFLICT("
                        "workspace_id,connection_id,external_chat_id,external_topic_id) "
                        "DO UPDATE SET contact_id=coalesce("
                        "EXCLUDED.contact_id,conversations.contact_id),"
                        "peer_name=CASE WHEN EXCLUDED.peer_name<>'' THEN EXCLUDED.peer_name "
                        "ELSE conversations.peer_name END RETURNING id"
                    ),
                    {
                        "w": workspace,
                        "id": conversation_id,
                        "c": channel,
                        "contact": customer.get("contact_id") if customer else None,
                        "external": external,
                        "name": display,
                    },
                ).scalar_one()
                if customer and customer.get("contact_id") and customer["contact_id"] not in merged_contacts:
                    self._merge_contact_conversations(
                        db, workspace, channel, customer["contact_id"], conversation_id
                    )
                    merged_contacts.add(customer["contact_id"])
                touched.add(conversation_id)
                source_id = _source_record_id(item, branch)
                existing = (
                    db.execute(
                        text(
                            "SELECT m.* FROM source_records s JOIN messages m ON "
                            "m.workspace_id=s.workspace_id AND m.id=s.message_id "
                            "WHERE s.workspace_id=:w AND s.connection_id=:c "
                            "AND s.namespace='alfa_max' "
                            "AND s.external_record_id=:id"
                        ),
                        {"w": workspace, "c": channel, "id": source_id},
                    )
                    .mappings()
                    .one_or_none()
                )

                # Undo aliases created by the old text/time heuristic. Distinct
                # CRM row keys must survive even when their content is identical.
                if existing and existing["provider_message_id"] != f"alfa:{source_id}":
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
                payload = {
                    "source": "alfa_max",
                    "branch_id": branch,
                    "crm_order": item.get("crm_order"),
                    "customer_branch_id": customer["branch_id"] if customer else None,
                    "customer_id": customer["customer_id"] if customer else None,
                    "attachments": item["attachments"],
                    "actor_name": item["actor_name"],
                    "actor_type": item["actor_type"],
                    "actor_id": item.get("actor_id"),
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
                                "SELECT m.id,m.occurred_at,m.payload,s.id AS command_id,"
                                "abs(extract(epoch FROM (m.occurred_at-:at))) AS distance "
                                "FROM messages m "
                                "JOIN send_commands s "
                                "ON s.workspace_id=m.workspace_id AND s.id=m.send_command_id "
                                "WHERE m.workspace_id=:w AND m.conversation_id=:conv "
                                "AND m.direction='out' "
                                "AND s.state IN ('accepted','delivered','read','unknown') "
                                "AND m.text=:text AND m.payload->>'crm_branch'=:branch "
                                "AND m.payload->>'customer_id'=:customer "
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
                                "branch": str(customer["branch_id"]),
                                "customer": str(customer["customer_id"]),
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
                        matched_payload = dict(best["payload"])
                        matched_payload.update(payload)
                        matched_payload["source"] = "center_messenger_alfa_max"
                        matched_payload["actor_type"] = "human"
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
                        continue
                if existing:
                    merged = dict(existing["payload"]) | payload
                    actor = item["actor_type"]
                    if existing["send_command_id"]:
                        merged["source"] = existing["payload"].get("source", "center_messenger_alfa_max")
                        actor = merged["actor_type"] = "human"
                    if (
                        existing["text"] != item["text"]
                        or existing["payload"] != merged
                        or existing["actor_type"] != actor
                        or existing["occurred_at"] != item["occurred_at"]
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
                                "text": item["text"],
                                "actor": actor,
                                "payload": json.dumps(merged, ensure_ascii=False),
                                "at": item["occurred_at"],
                            },
                        )
                        changed(db, workspace, conversation_id, existing["id"])
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
        for conversation_id in touched:
            refresh_unread(db, workspace, conversation_id)

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
                "AND m.payload->>'customer_id' IS NOT NULL "
                "AND m.payload->>'customer_branch_id' IS NOT NULL "
                "AND coalesce(m.payload->>'deleted','false')<>'true' "
                "ORDER BY " + message_order(descending=True) + " LIMIT 1"
            ),
            {"w": workspace, "conv": conversation},
        ).scalar_one_or_none()
        if not target:
            raise AlfaBrowserError("history_unavailable")
        session = self.vault.open_browser_state(workspace, connection, row["session_ciphertext"])
        return {
            "origin": row["crm_origin"],
            "state": session,
            "branch": int(target["customer_branch_id"]),
            "customer": int(target["customer_id"]),
        }

    def send_native(self, workspace, connection, conversation, message, *, target=None):
        target = target or self.send_target(workspace, connection, conversation)
        result = self.browser.send(
            target["origin"], target["state"], target["branch"], target["customer"], message
        )
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

    def _heartbeat(self, workspace, connection, lease):
        # Many branches/pages can take longer than one lease. Renew between
        # browser navigations without keeping a database transaction open.
        with self._transaction(workspace, connection, lease) as db:
            db.execute(
                text("UPDATE alfa_max_sync_queue SET lease_until=clock_timestamp()+interval '300 seconds' "
                     "WHERE workspace_id=:w AND connection_id=:c AND lease_owner=:lease"),
                {"w": workspace, "c": connection, "lease": lease},
            )

    def run_next(self):
        import time

        claim, lease = self._claim()
        if not claim:
            return False
        workspace, connection = claim["workspace_id"], claim["connection_id"]
        delay, code = 90, None
        phase = "load_settings"
        sync_start = time.monotonic()
        try:
            with self._transaction(workspace, connection, lease) as db:
                row = self.service._row(db, workspace)
                if (
                    row is None
                    or row["connection_id"] != connection
                    or row["state"] != "active"
                    or row["session_ciphertext"] is None
                    or row["crm_origin"] is None
                ):
                    self._release(db, workspace, connection, lease, 300)
                    return True
                crm_connection = row["crm_connection_id"]
                phase = "open_session"
                session = self.service.vault.open_browser_state(
                    workspace, connection, row["session_ciphertext"]
                )
                phase = "load_branches"
                branches = (
                    db.execute(
                        text(
                            "SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                            "AND connection_id=:crm AND available ORDER BY external_id"
                        ),
                        {"w": workspace, "crm": crm_connection},
                    )
                    .scalars()
                    .all()
                )
                # Read cursor early (needed to build requests before browser IO),
                # but re-read + version-check in final transaction.
                original_cursor = dict(row["history_cursor"] or {})
                # Replay existing history once after this projection correction,
                # including rows outside page one that were previously aliased.
                cursor = dict(original_cursor) if original_cursor.get("projection_version") == 2 else {}
                cursor["projection_version"] = 2
                origin = row["crm_origin"]
                settings_version = row["version"]
            if not branches:
                raise AlfaBrowserError("settings_changed")
            phase = "read_history"
            updates, requests, pages = [], [], {}
            # Always refresh page one for every branch and direction. The cursor
            # adds a single older page per poll, deduped by CRM's stable row key.
            for branch in branches:
                for direction in ("receive", "sent"):
                    key = f"{branch}:{direction}"
                    target_page = max(0, int(cursor.get(key, 0)))
                    requests.append((branch, direction, 0))
                    pages[(branch, direction)] = target_page
                    if target_page > 0:
                        requests.append((branch, direction, target_page))
            heartbeat = lambda: self._heartbeat(workspace, connection, lease)
            results, session = self.service.browser.read_histories(
                origin, session, requests, on_progress=heartbeat
            )
            pages_by_request = dict(zip(requests, results, strict=True))
            for branch in branches:
                for direction in ("receive", "sent"):
                    key = (branch, direction)
                    current = pages_by_request[(branch, direction, 0)]
                    target_page = pages[key]
                    older = (
                        pages_by_request[(branch, direction, target_page)]
                        if target_page > 0
                        else None
                    )
                    page_results, next_page = _history_page_batch(current, target_page, older)
                    cursor_key = f"{branch}:{direction}"
                    previous_head = cursor.get(f"head:{cursor_key}")
                    identities = [str(m["record_id"]) for m in current["messages"]]
                    gap = bool(previous_head and current["has_more"] and previous_head not in identities)
                    if gap and target_page != 1:
                        # More than a page arrived since the last poll. Restart
                        # the backfill near the head so none of that burst is skipped.
                        next_page = 1
                    elif target_page == 0 and cursor.get(f"complete:{cursor_key}"):
                        next_page = 0
                    updates.extend((branch, page["messages"]) for page in page_results)
                    cursor[cursor_key] = next_page
                    cursor[f"complete:{cursor_key}"] = next_page == 0
                    if identities:
                        cursor[f"head:{cursor_key}"] = identities[0]
            heartbeat()
            self.service.sms_templates(origin, session, branches[0])
            phase = "persist_history"
            encrypted = self.service.vault.seal_browser_state(workspace, connection, session)
            with self._transaction(workspace, connection, lease) as db:
                current = self.service._row(db, workspace)
                # Re-check version after browser IO — settings may have changed.
                if (
                    current is None
                    or current["connection_id"] != connection
                    or current["state"] != "active"
                    or current["session_ciphertext"] is None
                    or current["version"] != settings_version
                    or dict(current["history_cursor"] or {}) != original_cursor
                ):
                    raise AlfaBrowserError("settings_changed")
                for branch, records in updates:
                    self.service._project_page(
                        db, workspace, connection, crm_connection, branch, records
                    )
                db.execute(
                    text(
                        "UPDATE alfa_max_settings SET session_ciphertext=:session,"
                        "history_cursor=CAST(:cursor AS jsonb),last_sync_at=now(),"
                        "last_error_code=NULL,updated_at=now() WHERE workspace_id=:w"
                    ),
                    {
                        "w": workspace,
                        "session": encrypted,
                        "cursor": json.dumps(cursor),
                    },
                )
                # Keep backfilling older pages quickly; once every branch and
                # direction reaches its end (cursor value 0) settle to the normal
                # polling interval.
                delay = 2 if any(
                    cursor.get(f"{branch}:{direction}", 0) > 0
                    for branch in branches for direction in ("receive", "sent")
                ) else 15
                self._release(db, workspace, connection, lease, delay)
        except AlfaBrowserError as error:
            code = error.code
            delay = (
                30
                if code == "settings_changed"
                else 60
                if code in {"network", "crm_unavailable"}
                else 300
            )
            try:
                with self._transaction(workspace, connection, lease) as db:
                    if code == "reauth_required":
                        db.execute(
                            text(
                                "UPDATE alfa_max_settings SET state='reauth_required',"
                                "last_error_code=:code,updated_at=now() WHERE workspace_id=:w"
                            ),
                            {"w": workspace, "code": code},
                        )
                        db.execute(
                            text(
                                "UPDATE connections SET status='reauth_required' "
                                "WHERE workspace_id=:w AND id=:c"
                            ),
                            {"w": workspace, "c": connection},
                        )
                    else:
                        db.execute(
                            text(
                                "UPDATE alfa_max_settings SET "
                                "last_error_code=:code,updated_at=now() "
                                "WHERE workspace_id=:w"
                            ),
                            {"w": workspace, "code": code},
                        )
                    self._release(db, workspace, connection, lease, delay)
            except Exception:
                pass
        except Exception as error:
            import traceback
            code, delay = "internal", 120
            try:
                with self._transaction(workspace, connection, lease) as db:
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
            tb = traceback.format_exc()
            logging.getLogger(__name__).error(
                "AlfaCRM MAX history sync failed phase=%s exception=%s delay=%d\n%s",
                phase,
                type(error).__name__,
                delay,
                tb,
            )
        duration = time.monotonic() - sync_start
        logging.getLogger(__name__).info(
            "AlfaCRM MAX sync completed workspace=%s connection=%s duration=%.1fs",
            workspace,
            connection,
            duration,
        )
        return True
