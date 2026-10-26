"""Durable webhook intake and leased sends. Ambiguous sends are never repeated."""

import hmac
import json
import logging
import threading
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import text

from center_messenger.accounts import context
from center_messenger.alfa_max_browser import AlfaBrowserError
from center_messenger.auth_security import AuthError
from center_messenger.chat_store import changed, project_event, refresh_unread, source
from center_messenger.max_client import MaxError, timestamp
from center_messenger.max_connections import fingerprint


class LostLease(Exception):
    pass


class Messaging:
    def __init__(self, max_connections, alfa_max=None):
        self.max = max_connections
        self.accounts = max_connections.accounts
        self.alfa_max = alfa_max

    @staticmethod
    def _sender_allowed(db, workspace, user):
        from center_messenger.auth_security import can_send_messages
        member = db.execute(text(
            "SELECT m.role,m.sections FROM workspace_memberships m JOIN auth_users u ON u.id=m.user_id "
            "WHERE m.workspace_id=:w AND m.user_id=:u AND m.active AND u.active"
        ), {"w": workspace, "u": user}).mappings().one_or_none()
        return bool(member and can_send_messages(member["role"], member["sections"]))

    @staticmethod
    def enqueue(db, workspace, connection, subject, kind, channel_kind="max_bot"):
        db.execute(
            text(
                "INSERT INTO message_jobs("
                "id,workspace_id,connection_id,subject_id,kind,channel_kind) "
                "VALUES (:id,:w,:c,:s,:k,:channel_kind) ON CONFLICT DO NOTHING"
            ),
            {
                "id": uuid4(),
                "w": workspace,
                "c": connection,
                "s": subject,
                "k": kind,
                "channel_kind": channel_kind,
            },
        )

    def receive(self, connection, secret, event):
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("update_type"), str)
            or len(event["update_type"]) > 100
        ):
            raise AuthError(400, "Некорректное событие MAX.")
        try:
            timestamp(event.get("timestamp"))
        except MaxError:
            raise AuthError(400, "Некорректное время события MAX.") from None
        if not 5 <= len(secret) <= 256:
            raise AuthError(403, "Неверный секрет webhook.")
        with self.accounts.transaction() as db:
            route = (
                db.execute(
                    text("SELECT * FROM max_routes WHERE connection_id=:c"), {"c": connection}
                )
                .mappings()
                .one_or_none()
            )
            if route is None:
                raise AuthError(404, "Подключение недоступно.")
            workspace = route["workspace_id"]
            context(db, workspace_id=workspace)
            settings = (
                db.execute(
                    text(
                        "SELECT * FROM max_settings WHERE workspace_id=:w "
                        "AND connection_id=:c FOR SHARE"
                    ),
                    {"w": workspace, "c": connection},
                )
                .mappings()
                .one()
            )
            route = (
                db.execute(
                    text("SELECT * FROM max_routes WHERE connection_id=:c"), {"c": connection}
                )
                .mappings()
                .one()
            )
            if not hmac.compare_digest(fingerprint(secret), route["secret_hash"]):
                raise AuthError(403, "Неверный секрет webhook.")
            if (
                not settings["enabled"]
                or not route["accept_events"]
                or settings["bot_user_id"] is None
            ):
                raise AuthError(409, "Приём отключён.")
            raw = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            event_id = db.execute(
                text(
                    "INSERT INTO "
                    "inbound_events(workspace_id,id,connection_id,event_key,event_type,payload) "
                    "VALUES (:w,:id,:c,:key,:type,CAST(:p AS jsonb)) ON CONFLICT DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "w": workspace,
                    "id": uuid4(),
                    "c": connection,
                    "key": fingerprint(raw),
                    "type": event["update_type"],
                    "p": raw,
                },
            ).scalar_one_or_none()
            if event_id:
                self.enqueue(db, workspace, connection, event_id, "event")
                db.execute(
                    text(
                        "UPDATE max_settings SET last_inbound_at=now() WHERE workspace_id=:w "
                        "AND connection_id=:c"
                    ),
                    {"w": workspace, "c": connection},
                )
        return {"success": True}

    def _claim(self, kind, channel_kind=None):
        lease = uuid4()
        lease_seconds = 180 if kind == "send" else 60
        with self.accounts.transaction() as db:
            extra = " AND message_jobs.kind=:kind" if kind else ""
            if channel_kind:
                extra += " AND message_jobs.channel_kind=:channel_kind"
            job = (
                db.execute(
                    text(
                        "SELECT message_jobs.* FROM message_jobs "
                        "WHERE message_jobs.due_at<=now() "
                        "AND message_jobs.lease_until<now() "
                        + extra
                        + " ORDER BY message_jobs.due_at,message_jobs.kind,message_jobs.id "
                        "LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ),
                    {"kind": kind, "channel_kind": channel_kind},
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                return None, None
            job = dict(job)
            db.execute(
                text(
                    "UPDATE message_jobs SET lease_owner=:lease,"
                    "lease_until=now()+(:seconds*interval '1 second') "
                    "WHERE id=:id"
                ),
                {"id": job["id"], "lease": lease, "seconds": lease_seconds},
            )
        return job, lease

    @contextmanager
    def _transaction(self, job, lease):
        with self.accounts.transaction(workspace_id=job["workspace_id"]) as db:
            db.execute(
                text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": job["workspace_id"]}
            )
            owned = db.execute(
                text(
                    "SELECT id FROM message_jobs WHERE id=:id AND lease_owner=:lease "
                    "AND lease_until>clock_timestamp() FOR UPDATE"
                ),
                {"id": job["id"], "lease": lease},
            ).scalar_one_or_none()
            if owned is None:
                raise LostLease
            yield db

    @staticmethod
    def _remove(db, job):
        db.execute(text("DELETE FROM message_jobs WHERE id=:id"), {"id": job["id"]})

    def _event(self, job, lease):
        with self._transaction(job, lease) as db:
            event = (
                db.execute(
                    text(
                        "SELECT * FROM inbound_events WHERE workspace_id=:w AND id=:id FOR UPDATE"
                    ),
                    {"w": job["workspace_id"], "id": job["subject_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if event and event["state"] == "pending":
                setting = self.max.row(db, job["workspace_id"], job["connection_id"])
                project_event(
                    db,
                    job["workspace_id"],
                    job["connection_id"],
                    setting["bot_user_id"],
                    event["payload"],
                )
                db.execute(
                    text(
                        "UPDATE inbound_events SET state='processed',error_code=NULL WHERE "
                        "workspace_id=:w AND id=:id"
                    ),
                    {"w": job["workspace_id"], "id": event["id"]},
                )
            self._remove(db, job)

    @staticmethod
    def _command(db, job):
        return (
            db.execute(
                text("SELECT * FROM send_commands WHERE workspace_id=:w AND id=:id FOR UPDATE"),
                {"w": job["workspace_id"], "id": job["subject_id"]},
            )
            .mappings()
            .one_or_none()
        )

    def _state(self, db, job, command, state, code=None):
        db.execute(
            text(
                "UPDATE send_commands SET state=:state,last_error_code=:code,updated_at=now() "
                "WHERE workspace_id=:w AND id=:id"
            ),
            {"w": job["workspace_id"], "id": command["id"], "state": state, "code": code},
        )
        identifier = db.execute(
            text(
                "UPDATE messages SET version=version+1 "
                "WHERE workspace_id=:w AND send_command_id=:id RETURNING id"
            ),
            {"w": job["workspace_id"], "id": command["id"]},
        ).scalar_one()
        changed(db, job["workspace_id"], command["conversation_id"], identifier)
        db.execute(
            text("UPDATE conversations SET revision=revision+1 WHERE workspace_id=:w AND id=:c"),
            {"w": job["workspace_id"], "c": command["conversation_id"]},
        )
        refresh_unread(db, job["workspace_id"], command["conversation_id"])
        return identifier

    def _send(self, job, lease):
        with self.accounts.transaction(workspace_id=job["workspace_id"]) as db:
            connection_kind = db.execute(
                text("SELECT kind FROM connections WHERE workspace_id=:w AND id=:c"),
                {"w": job["workspace_id"], "c": job["connection_id"]},
            ).scalar_one_or_none()
        if connection_kind == "alfa_max_browser":
            return self._send_alfa_max(job, lease)
        with self._transaction(job, lease) as db:
            command = self._command(db, job)
            if command is None or command["state"] not in {"queued", "sending"}:
                self._remove(db, job)
                return
            db.execute(
                text("SELECT id FROM conversations WHERE workspace_id=:w AND id=:c FOR UPDATE"),
                {"w": job["workspace_id"], "c": command["conversation_id"]},
            )
            if command["state"] == "sending":
                self._state(db, job, command, "unknown", "unknown")
                self._remove(db, job)
                return
            settings = self.max.row(db, job["workspace_id"], job["connection_id"])
            if (
                not settings["enabled"]
                or not settings["subscription_confirmed"]
                or settings["status"] != "active"
            ):
                self._state(db, job, command, "cancelled", "disconnected")
                self._remove(db, job)
                return
            context(db, user_id=command["requested_by"])
            if not self._sender_allowed(db, job["workspace_id"], command["requested_by"]):
                self._state(db, job, command, "cancelled", "access_changed")
                self._remove(db, job)
                return
            wait = db.execute(
                text(
                    "SELECT greatest(extract(epoch FROM (send_after-now())),0) "
                    "FROM max_settings WHERE workspace_id=:w AND connection_id=:c"
                ),
                {"w": job["workspace_id"], "c": job["connection_id"]},
            ).scalar_one()
            if wait > 0:
                db.execute(
                    text(
                        "UPDATE message_jobs SET due_at=now()+(:wait*interval '1 second'),"
                        "lease_owner=NULL,lease_until='2000-01-01T00:00:00Z' WHERE id=:id"
                    ),
                    {"id": job["id"], "wait": float(wait)},
                )
                return
            credentials = self.max.vault.open(
                job["workspace_id"], job["connection_id"], settings["secret_ciphertext"]
            )
            peer = db.execute(
                text("SELECT peer_user_id FROM conversations WHERE workspace_id=:w AND id=:c"),
                {"w": job["workspace_id"], "c": command["conversation_id"]},
            ).scalar_one()
            db.execute(
                text(
                    "UPDATE max_settings SET send_after=now()+interval '0.6 second' "
                    "WHERE workspace_id=:w AND connection_id=:c"
                ),
                {"w": job["workspace_id"], "c": job["connection_id"]},
            )
            db.execute(
                text(
                    "UPDATE send_commands SET attempts=attempts+1 WHERE workspace_id=:w AND id=:id"
                ),
                {"w": job["workspace_id"], "id": command["id"]},
            )
            self._state(db, job, command, "sending")
            command = dict(command)
        # No transaction is open during the external send; no implicit retries.
        try:
            sent = self.max.client_factory(credentials["api_key"]).send(
                peer, command["payload"]["text"]
            )
        except MaxError as error:
            self._send_error(job, lease, error)
            return
        with self._transaction(job, lease) as db:
            command = self._command(db, job)
            if command is None or command["state"] != "sending":
                raise LostLease
            identifier = self._state(db, job, command, "accepted")
            db.execute(
                text(
                    "UPDATE messages SET provider_message_id=:mid,occurred_at=:at WHERE "
                    "workspace_id=:w AND id=:id"
                ),
                {
                    "w": job["workspace_id"],
                    "id": identifier,
                    "mid": sent["id"],
                    "at": sent["timestamp"],
                },
            )
            source(db, job["workspace_id"], job["connection_id"], identifier, sent["id"])
            refresh_unread(db, job["workspace_id"], command["conversation_id"])
            self._remove(db, job)

    def _send_alfa_max(self, job, lease):
        if self.alfa_max is None:
            raise LostLease
        with self._transaction(job, lease) as db:
            command = self._command(db, job)
            if command is None or command["state"] not in {"queued", "sending"}:
                self._remove(db, job)
                return
            db.execute(
                text("SELECT id FROM conversations WHERE workspace_id=:w AND id=:c FOR UPDATE"),
                {"w": job["workspace_id"], "c": command["conversation_id"]},
            )
            if command["state"] == "sending":
                self._state(db, job, command, "unknown", "unknown")
                self._remove(db, job)
                return
            context(db, user_id=command["requested_by"])
            if not self._sender_allowed(db, job["workspace_id"], command["requested_by"]):
                self._state(db, job, command, "cancelled", "access_changed")
                self._remove(db, job)
                return
            try:
                target = self.alfa_max._send_target_in_transaction(
                    db, job["workspace_id"], job["connection_id"], command["conversation_id"]
                )
            except AlfaBrowserError as error:
                if error.code in {'browser_busy', 'session_check_pending', 'reauth_required'}:
                    self._state(db, job, command, 'queued', 'session_check_pending')
                    db.execute(text("UPDATE message_jobs SET due_at=now()+interval '10 seconds',"
                                    "lease_owner=NULL,lease_until='-infinity' WHERE id=:id"), {'id': job['id']})
                    return
                self._state(db, job, command, "cancelled" if error.code == "disconnected" else "failed", error.code)
                self._remove(db, job)
                return
            db.execute(
                text(
                    "UPDATE send_commands SET attempts=attempts+1 WHERE workspace_id=:w AND id=:id"
                ),
                {"w": job["workspace_id"], "id": command["id"]},
            )
            self._state(db, job, command, "sending")
            command = dict(command)
            # The outgoing bubble is correlated to AlfaCRM's sent journal only by
            # exact text, recipient, branch and a narrow time window.
            db.execute(
                text(
                    "UPDATE messages SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w "
                    "AND send_command_id=:id"
                ),
                {
                    "w": job["workspace_id"],
                    "id": command["id"],
                    "p": json.dumps(
                        {
                            "source": "center_messenger_alfa_max",
                            "crm_branch": target["branch"],
                            "customer_id": target["customer"],
                        }
                    ),
                },
            )
        try:
            self.alfa_max.send_native(
                job["workspace_id"],
                job["connection_id"],
                command["conversation_id"],
                command["payload"]["text"],
                target=target,
            )
        except AlfaBrowserError as error:
            with self._transaction(job, lease) as db:
                command = self._command(db, job)
                if command is None or command["state"] != "sending":
                    raise LostLease from None
                if error.code in {'browser_busy', 'settings_changed', 'session_check_pending'}:
                    self._state(db, job, command, 'queued', 'session_check_pending')
                    db.execute(text("UPDATE message_jobs SET due_at=now()+interval '10 seconds',"
                                    "lease_owner=NULL,lease_until='-infinity' WHERE id=:id"), {'id': job['id']})
                    return
                state = "unknown" if error.code == "send_unknown" else "failed"
                public_code = "unknown" if state == "unknown" else error.code
                self._state(db, job, command, state, public_code)
                self._remove(db, job)
            return
        with self._transaction(job, lease) as db:
            command = self._command(db, job)
            if command is None or command["state"] != "sending":
                raise LostLease
            identifier = self._state(db, job, command, "accepted")
            db.execute(
                text(
                    "UPDATE messages SET occurred_at=now(),version=version+1 WHERE "
                    "workspace_id=:w AND id=:id"
                ),
                {"w": job["workspace_id"], "id": identifier},
            )
            changed(db, job["workspace_id"], command["conversation_id"], identifier)
            refresh_unread(db, job["workspace_id"], command["conversation_id"])
            self._remove(db, job)

    def _send_error(self, job, lease, error):
        with self._transaction(job, lease) as db:
            command = self._command(db, job)
            if command is None or command["state"] != "sending":
                raise LostLease
            if error.code == "rate_limited" and command["attempts"] < 4:
                self._state(db, job, command, "queued", error.code)
                db.execute(
                    text(
                        "UPDATE message_jobs SET due_at=now()+(:delay*interval '1 second'),"
                        "lease_owner=NULL,lease_until='2000-01-01T00:00:00Z' WHERE id=:id"
                    ),
                    {"id": job["id"], "delay": error.retry_after},
                )
                db.execute(
                    text(
                        "UPDATE max_settings SET "
                        "send_after=greatest(send_after,now()+(:delay*interval '1 second')) "
                        "WHERE workspace_id=:w AND connection_id=:c"
                    ),
                    {
                        "w": job["workspace_id"],
                        "c": job["connection_id"],
                        "delay": error.retry_after,
                    },
                )
                return
            definite = {"unauthorized", "forbidden", "rejected", "tls", "rate_limited"}
            state = "failed" if error.code in definite else "unknown"
            self._state(db, job, command, state, error.code if state == "failed" else "unknown")
            if error.code in {"unauthorized", "tls"}:
                db.execute(
                    text(
                        "UPDATE max_settings SET last_error_code=:code WHERE "
                        "workspace_id=:w AND connection_id=:c"
                    ),
                    {"w": job["workspace_id"], "c": job["connection_id"], "code": error.code},
                )
                db.execute(
                    text("UPDATE connections SET status='error' WHERE workspace_id=:w AND id=:c"),
                    {"w": job["workspace_id"], "c": job["connection_id"]},
                )
            self._remove(db, job)

    def _recover_error(self, job, lease, code):
        try:
            with self._transaction(job, lease) as db:
                if job["kind"] == "event":
                    if code == "invalid_response":
                        db.execute(
                            text(
                                "UPDATE inbound_events SET state='failed',error_code=:code "
                                "WHERE workspace_id=:w AND id=:id"
                            ),
                            {"w": job["workspace_id"], "id": job["subject_id"], "code": code},
                        )
                        self._remove(db, job)
                    else:
                        db.execute(
                            text(
                                "UPDATE message_jobs SET due_at=now()+interval '5 seconds',"
                                "lease_owner=NULL,lease_until='2000-01-01T00:00:00Z' WHERE id=:id"
                            ),
                            {"id": job["id"]},
                        )
                else:
                    command = self._command(db, job)
                    if command and command["state"] == "sending":
                        self._state(db, job, command, "unknown", "unknown")
                        self._remove(db, job)
                    else:
                        db.execute(
                            text(
                                "UPDATE message_jobs SET due_at=now()+interval '5 seconds',"
                                "lease_owner=NULL,lease_until='2000-01-01T00:00:00Z' WHERE id=:id"
                            ),
                            {"id": job["id"]},
                        )
        except LostLease:
            pass

    def run_next(self, kind=None, channel_kind=None):
        job, lease = self._claim(kind, channel_kind)
        if job is None:
            return False
        try:
            if job["kind"] == "event":
                self._event(job, lease)
            else:
                self._send(job, lease)
        except LostLease:
            pass
        except MaxError as error:
            self._recover_error(job, lease, error.code)
        except Exception:
            self._recover_error(job, lease, "internal")
            logging.getLogger(__name__).warning("MAX work interrupted; inspect channel status")
        return True


class MessagingWorker:
    def __init__(self, messaging):
        self.messaging, self.stopping = messaging, threading.Event()
        self.threads = [
            threading.Thread(
                target=self._run, args=(kind, channel), name=f"{channel}-{kind}", daemon=True
            )
            for kind, channel in (
                ("event", "max_bot"),
                ("send", "max_bot"),
                ("send", "alfa_max_browser"),
            )
        ]

    def _run(self, kind, channel):
        while not self.stopping.is_set():
            try:
                worked = self.messaging.run_next(kind, channel)
            except Exception:
                worked = False
                logging.getLogger(__name__).warning("MAX queue temporarily unavailable")
            self.stopping.wait(0.1 if worked else 0.5)

    def start(self):
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=15)
