"""Workspace-scoped chat API with membership checks and read-only observers."""

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from center_messenger.accounts import audit
from center_messenger.accounts_api import identity, service
from center_messenger.ai_voice import AiVoice
from center_messenger.alfa_max import ERRORS as ALFA_MAX_ERRORS
from center_messenger.alfa_history import history_pending
from center_messenger.auth_security import AuthError, InputModel
from center_messenger.chat_store import changed, refresh_unread
from center_messenger.chat_queue import PROJECTION_VERSION, queue_state_sql, cached_state_sql, preview as queue_preview, materialize_legacy_handled
from center_messenger.crm_api import DirectoryReader, search_query
from center_messenger import card_preference
from center_messenger.chat_list_window import ListWindows
from center_messenger import chat_preferences
from center_messenger import bulk_handled, chat_revision
from center_messenger.max_client import ERRORS as MAX_ERRORS
from center_messenger.max_client import MaxChannelInput, MaxSettingsInput
from center_messenger.max_connections import fingerprint
from center_messenger.message_order import message_order, source_order, visible_message
from center_messenger.database import log_database_error

router = APIRouter(prefix="/api/workspaces/{workspace_id}")
hooks = APIRouter()
ERRORS = {**MAX_ERRORS, **ALFA_MAX_ERRORS}
logger = logging.getLogger(__name__)


@contextmanager
def family_read_stage(stage):
    started = perf_counter()
    try:
        yield
    finally:
        elapsed = (perf_counter() - started) * 1000
        if elapsed >= 250:
            # Same searchable prefix as the request timing; never include IDs,
            # query parameters, customer names or underlying exception text.
            logger.warning("Slow API read route=/api/workspaces/{workspace_id}/chats/{conversation_id}/family "
                           "stage=%s duration_ms=%.0f", stage, elapsed)

# Cached columns are used only after the resumable repair has completed.
# During repair the same policy supplies live values, so no stale zero is shown.
QUEUE_READY = f"(cn.capabilities->'chat_queue_projection'->>'version' IN ('1','{PROJECTION_VERSION}') AND cn.capabilities->'chat_queue_projection'->>'complete'='true')"
EFFECTIVE_UNREAD = "coalesce(q.needs_reply,c.needs_reply)"
PRIORITY_AT = "CASE WHEN q.unanswered_count IS NOT NULL THEN q.priority_at ELSE c.last_message_at END"
QUEUE_COLUMNS = ",".join("q." + field for field in (
    "unanswered_count", "last_unanswered_message_id", "last_unanswered_at",
    "last_human_activity_at", "last_event_at", "priority_at", "preview_message_id",
    "preview_text", "preview_attachments", "last_direction", "last_state"))


class SeenCheckpoint(InputModel):
    conversation_id: UUID
    revision: int = Field(ge=1)
    change_seq: int = Field(ge=0)
    latest_inbound_id: UUID | None


class HandleInput(InputModel):
    seen_checkpoint: SeenCheckpoint


class BulkHandleInput(InputModel):
    operation_id: UUID
    confirmed: Literal[True]


class SendInput(InputModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    seen_inbound_seq: int = Field(ge=0)
    seen_checkpoint: SeenCheckpoint | None = None

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Empty message")
        return value.strip()


class LinkInput(InputModel):
    contact_id: UUID | None


class ChatPreferencesInput(InputModel):
    name_order: Literal["parent_first", "child_first"]


class ChatReader:
    def __init__(self, messaging):
        self.messaging, self.max = messaging, messaging.max
        self.alfa_max = messaging.alfa_max
        self.list_windows = ListWindows()

    def _can_write(self, db, actor, workspace):
        from center_messenger.auth_security import can_send_messages
        member = self.max.accounts._workspace(db, actor, workspace)
        return can_send_messages(member["role"], member.get("sections"))

    def _require_write(self, db, actor, workspace):
        if not self._can_write(db, actor, workspace):
            raise AuthError(403, "Роль «Наблюдатель» открывает переписку только для чтения.")

    def context(self, actor, workspace):
        # Chat metadata is available to members granted messages. Connector
        # setup and credentials retain their administrator-only endpoints.
        with self.max.chat_transaction(actor, workspace, snapshot=True) as db:
            writable = self._can_write(db, actor, workspace)
            rows = db.execute(text(
                "SELECT cn.id,cn.name,cn.kind,cn.status,cn.status='active' AND ("
                "(cn.kind='max_bot' AND EXISTS(SELECT 1 FROM max_settings active WHERE active.workspace_id=cn.workspace_id "
                "AND active.connection_id=cn.id AND active.enabled AND active.subscription_confirmed)) OR "
                "(cn.kind='alfa_max_browser' AND EXISTS(SELECT 1 FROM alfa_max_settings active WHERE active.workspace_id=cn.workspace_id "
                "AND active.connection_id=cn.id AND active.state='active' AND active.session_ciphertext IS NOT NULL))) AS enabled FROM connections cn "
                "WHERE cn.workspace_id=:w AND ((cn.kind='max_bot' AND EXISTS(SELECT 1 FROM max_settings s "
                "WHERE s.workspace_id=cn.workspace_id AND s.connection_id=cn.id)) OR "
                "(cn.kind='alfa_max_browser' AND EXISTS(SELECT 1 FROM alfa_max_settings s "
                "WHERE s.workspace_id=cn.workspace_id AND s.connection_id=cn.id))) ORDER BY cn.created_at,cn.id"
            ), {"w": workspace}).mappings().all()
            return {"version": chat_revision.fingerprint(db, workspace),
                    "preferences": chat_preferences.read(db, workspace, actor.user_id),
                    "channels": [{"id": str(row["id"]), "name": row["name"], "kind": row["kind"],
                                  "status": row["status"], "enabled": bool(row["enabled"])} for row in rows],
                    "branches": DirectoryReader._branches(db, workspace), "can_write": writable,
                    "send_attachments": False,
                    "voice_input": writable and AiVoice.voice_available(db, workspace),
                    "voice_max_seconds": AiVoice.voice_duration(db, workspace) if writable else 180}

    def bootstrap(self, actor, workspace, **filters):
        context = self.context(actor, workspace)
        listing = self.listing(actor, workspace, through_page=0, window=90, **filters)
        first = listing["conversations"][0] if listing["conversations"] else None
        history = None
        retry_history = False
        if first:
            try:
                history = self.history(actor, workspace, UUID(first["id"]), limit=40)
            except AuthError as error:
                if error.status != 404:
                    raise
            except SQLAlchemyError as error:
                # A failed timeline transaction must not discard a completed
                # authorized list. The client retries history independently.
                log_database_error(logger, error, operation="chat-bootstrap-history")
                retry_history = True
        return {"context": context, "listing": listing, "history": history,
                "history_pending": retry_history}

    def event_revision(self, actor, workspace):
        with self.max.chat_transaction(actor, workspace, snapshot=True) as db:
            revision, card_revision = chat_revision.versions(db, workspace)
            return {"version": revision + ":" + datetime.now(UTC).strftime("%Y-%m-%d"),
                    "card_version": card_revision, "write_access": self._can_write(db, actor, workspace)}

    def updates(self, actor, workspace, after=None):
        with self.max.chat_transaction(actor, workspace, snapshot=True) as db:
            # Writers serialize sequence allocation and commit under the
            # workspace lock. One snapshot sees a complete committed prefix
            # without waiting for an in-flight archive import.
            end = db.execute(
                text("SELECT coalesce(max(seq),0) FROM message_changes WHERE workspace_id=:w"),
                {"w": workspace},
            ).scalar_one()
            if after is None:
                return {"next_seq": end, "updates": [], "has_more": False}
            snapshot = DirectoryReader._published(db, workspace)
            update_params = {"w": workspace, "after": min(after, end), **card_preference.params(snapshot)}
            eligible_channel = ("(cn.kind='max_bot' AND s.connection_id IS NOT NULL) OR "
                                "(cn.kind='alfa_max_browser' AND am.connection_id IS NOT NULL)")
            if snapshot:
                update_params.update(run=snapshot["run"], crm=snapshot["connection_id"])
                eligible_channel = (
                    "(cn.kind='max_bot' AND s.connection_id IS NOT NULL) OR (cn.kind='alfa_max_browser' "
                    "AND am.crm_connection_id=:crm)"
                )
            rows = list(
                db.execute(
                    text(
                        "SELECT ch.seq,c.id,c.inbound_seq,c.contact_id,"
                        "c.crm_link_manual,cp.payload AS card_selection," + card_preference.contact_sql() + " AS family_contact_id,"
                        "coalesce(cp.payload->>'name',p.display_name,c.peer_name) AS name,m.direction,"
                        "(ch.new_inbound AND " + visible_message() + ") AS new_inbound,"
                        "CASE WHEN m.payload->>'deleted'='true' THEN NULL ELSE m.text END AS preview,"
                        + EFFECTIVE_UNREAD
                        + " AS effective_unread,q.unanswered_count,q.last_unanswered_at "
                        "FROM message_changes ch JOIN messages m ON m.workspace_id=ch.workspace_id "
                        "AND m.id=ch.message_id JOIN conversations c ON "
                        "c.workspace_id=m.workspace_id "
                        "AND c.id=m.conversation_id JOIN connections cn ON "
                        "cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
                        "LEFT JOIN max_settings s ON s.workspace_id=c.workspace_id "
                        "AND s.connection_id=c.connection_id LEFT JOIN alfa_max_settings am ON "
                        "am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
                        + card_preference.join_sql(snapshot) +
                        "LEFT JOIN contacts p ON "
                        "p.workspace_id=c.workspace_id AND p.id=" + card_preference.contact_sql() + " "
                        "CROSS JOIN LATERAL (" + cached_state_sql() + ") q "
                        "WHERE ch.workspace_id=:w AND ch.seq>:after AND c.kind='direct' AND ("
                        + eligible_channel
                        + ") ORDER BY ch.seq LIMIT 101"
                    ),
                    update_params,
                ).mappings()
            )
            more = len(rows) > 100
            rows = rows[:100]
            latest = {}
            new_inbounds = set()
            inbound_previews = {}
            for row in rows:
                latest[row["id"]] = row
                if row["direction"] == "in" and row["new_inbound"]:
                    new_inbounds.add(row["id"])
                    inbound_previews[row["id"]] = (row["preview"] or "")[:180]
            weights = {}
            contacts = list({r["family_contact_id"] for r in latest.values() if r["family_contact_id"]})
            if snapshot and contacts:
                weights = dict(
                    db.execute(
                        text(
                            "SELECT a.contact_id,max(CASE a.status WHEN 'client' THEN 3 "
                            "WHEN 'lead' THEN 2 ELSE 1 END) FROM alfa_customers a WHERE "
                            "a.workspace_id=:w AND a.run_id=:run AND "
                            "a.contact_id=ANY(CAST(:ids AS uuid[])) AND EXISTS(SELECT 1 "
                            "FROM alfa_branches b "
                            "WHERE b.workspace_id=a.workspace_id AND b.connection_id=:crm "
                            "AND b.external_id=a.branch_id AND b.available) GROUP BY a.contact_id"
                        ),
                        {
                            "w": workspace,
                            "run": snapshot["run"],
                            "crm": snapshot["connection_id"],
                            "ids": contacts,
                        },
                    ).all()
                )
            return {
                "next_seq": rows[-1]["seq"] if more else end,
                "has_more": more,
                "updates": [
                    {
                        "id": str(r["id"]),
                        "name": r["name"],
                        "family_contact_id": str(r["family_contact_id"]) if r["family_contact_id"] else None,
                        "card_selection": card_preference.public(r["card_selection"], manual=r["crm_link_manual"]),
                        "inbound_seq": r["inbound_seq"],
                        "unread": r["effective_unread"],
                        "unanswered_count": r["unanswered_count"],
                        "unread_count": r["unanswered_count"],
                        "needs_reply": r["effective_unread"],
                        "new_inbound": r["id"] in new_inbounds,
                        "preview": inbound_previews.get(r["id"]),
                        "status": {3: "client", 2: "lead", 1: "archive"}.get(
                            weights.get(r["family_contact_id"]), "unlinked"
                        ),
                    }
                    for r in latest.values()
                ],
            }

    @staticmethod
    def _family_conversation(db, workspace, conversation):
        """Resolve the authorized CRM link without reading message history.

        Family reads don't use unread counters, send eligibility or previews.
        In particular, a missing queue projection must not trigger a full
        history calculation before an already cached card can be returned.
        """
        row = db.execute(text(
            "SELECT c.id,c.connection_id,c.contact_id,c.external_chat_id,c.crm_link_manual,cn.kind AS channel_kind "
            "FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
            "LEFT JOIN max_settings s ON s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id "
            "LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
            "WHERE c.workspace_id=:w AND c.id=:id AND c.kind='direct' AND "
            "((cn.kind='max_bot' AND s.connection_id IS NOT NULL) OR "
            "(cn.kind='alfa_max_browser' AND am.connection_id IS NOT NULL))"),
            {"w": workspace, "id": conversation}).mappings().one_or_none()
        if row is None:
            raise AuthError(404, "Диалог недоступен.")
        if row["channel_kind"] == "alfa_max_browser" and row["contact_id"] and not row["crm_link_manual"]:
            external = f"alfa-contact:{row['contact_id']}"
            if row["external_chat_id"] != external:
                canonical = db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w "
                    "AND connection_id=:connection AND contact_id=:contact "
                    "AND external_chat_id=:external AND external_topic_id=''"),
                    {"w": workspace, "connection": row["connection_id"], "contact": row["contact_id"],
                     "external": external}).scalar_one_or_none()
                if canonical:
                    return ChatReader._family_conversation(db, workspace, canonical)
        return row

    @staticmethod
    def _conversation(db, workspace, conversation, *, lock=None, cached=False):
        snapshot = DirectoryReader._published(db, workspace)
        row = (
            db.execute(
                text(
                    "SELECT c.*,cn.name AS channel_name,cn.kind AS channel_kind,"
                    "cp.payload AS card_selection," + card_preference.contact_sql() + " AS family_contact_id,"
                    "coalesce(cp.payload->>'name',p.display_name,c.peer_name) AS display_name,"
                    "am.crm_connection_id,am.history_cursor AS crm_history_cursor,am.state AS crm_history_state,"
                    "am.last_error_code AS crm_history_error,"
                    "CASE WHEN cn.kind='alfa_max_browser' THEN "
                    "(am.state='active' AND am.session_ciphertext IS NOT NULL AND coalesce(am.history_cursor#>>'{session_control,needs_check}','false')<>'true' AND EXISTS("
                    "SELECT 1 FROM alfa_settings ax WHERE ax.workspace_id=c.workspace_id "
                    "AND ax.connection_id=am.crm_connection_id "
                    "AND ax.secret_ciphertext IS NOT NULL) AND c.contact_id IS NOT NULL "
                    "AND c.last_inbound_at IS NOT NULL) "
                    "ELSE (s.enabled AND s.subscription_confirmed) END AS can_send,cn.status AS "
                    "channel_status,"
                    + EFFECTIVE_UNREAD
                    + " AS effective_unread,"
                    + "q.latest_inbound_id," + QUEUE_COLUMNS + ","
                    "(SELECT coalesce(max(seq),0) FROM message_changes ch WHERE "
                    "ch.workspace_id=c.workspace_id AND ch.conversation_id=c.id) AS seen_change_seq "
                    "FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id "
                    "AND cn.id=c.connection_id "
                    "LEFT JOIN max_settings s ON s.workspace_id=c.workspace_id "
                    "AND s.connection_id=c.connection_id LEFT JOIN alfa_max_settings am ON "
                    "am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
                    + card_preference.join_sql(snapshot) +
                    "LEFT JOIN contacts p ON p.workspace_id=c.workspace_id AND p.id=" + card_preference.contact_sql() + " "
                    "CROSS JOIN LATERAL (" + (cached_state_sql() if cached and not lock else queue_state_sql()) + ") q "
                    "WHERE c.workspace_id=:w AND c.id=:id AND c.kind='direct' AND "
                    "((cn.kind='max_bot' AND s.connection_id IS NOT NULL) OR "
                    "(cn.kind='alfa_max_browser' AND am.connection_id IS NOT NULL)) "
                    + (f"FOR {lock} OF c" if lock else "")
                ),
                {"w": workspace, "id": conversation, **card_preference.params(snapshot)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuthError(404, "Диалог недоступен.")
        if row["channel_kind"] == "alfa_max_browser" and row["contact_id"] and not row["crm_link_manual"]:
            external = f"alfa-contact:{row['contact_id']}"
            if row["external_chat_id"] != external:
                canonical = db.execute(
                    text("SELECT id FROM conversations WHERE workspace_id=:w "
                         "AND connection_id=:connection AND contact_id=:contact "
                         "AND external_chat_id=:external AND external_topic_id=''"),
                    {"w": workspace, "connection": row["connection_id"],
                     "contact": row["contact_id"], "external": external},
                ).scalar_one_or_none()
                if canonical:
                    return ChatReader._conversation(db, workspace, canonical, lock=lock, cached=cached)
        from center_messenger.customer_lookup import enrich
        return enrich(db, workspace, [row])[0]

    @staticmethod
    def _meta(row):
        cursor = row.get("crm_history_cursor") or {}
        loading = (row.get("channel_kind") == "alfa_max_browser"
                   and row.get("crm_history_state") == "active" and history_pending(cursor))
        preview, preview_kind = queue_preview(row)
        priority = row["priority_at"]
        checkpoint = ({"conversation_id": str(row["id"]), "revision": row["revision"],
                       "change_seq": row["seen_change_seq"],
                       "latest_inbound_id": str(row["latest_inbound_id"]) if row["latest_inbound_id"] else None}
                      if "seen_change_seq" in row else None)
        return {
            "needs_reply": row["effective_unread"],
            "unanswered_count": row["unanswered_count"], "unread_count": row["unanswered_count"],
            "last_unanswered_message_id": str(row["last_unanswered_message_id"]) if row["last_unanswered_message_id"] else None,
            "last_unanswered_at": row["last_unanswered_at"], "waiting_since": row["last_unanswered_at"],
            "last_human_activity_at": row["last_human_activity_at"], "last_event_at": row["last_event_at"],
            "priority_at": priority, "last_message_at": priority,
            "last_message_sort_time": str((priority-datetime(1970,1,1,tzinfo=UTC)) // timedelta(microseconds=1)) if priority else None,
            "preview_message_id": str(row["preview_message_id"]) if row["preview_message_id"] else None,
            "preview": preview, "preview_kind": preview_kind,
            "last_direction": row["last_direction"], "last_state": row["last_state"],
            "queue_seq": str(row["seen_change_seq"]) if "seen_change_seq" in row else None,
            "seen_checkpoint": checkpoint,
            "id": str(row["id"]),
            "connection_id": str(row["connection_id"]),
            "contact_id": str(row["contact_id"]) if row["contact_id"] else None,
            "family_contact_id": str(row.get("family_contact_id") or row["contact_id"]) if row["contact_id"] else None,
            "card_selection": card_preference.public(row.get("card_selection"), manual=row.get("crm_link_manual", False)),
            "name": row.get("display_name") or row["peer_name"],
            "peer_name": row["peer_name"],
            "channel_name": row["channel_name"],
            "unread": row["effective_unread"],
            "inbound_seq": row["inbound_seq"],
            "revision": row["revision"],
            "can_send": bool(row["can_send"]) and row["channel_status"] == "active",
            "history_loading": bool(loading),
            "history_error": ALFA_MAX_ERRORS.get(row.get("crm_history_error")),
            "identity_resolution": row.get("identity_resolution"),
        }

    @staticmethod
    def _message(row):
        payload = row["payload"]
        attachments = payload.get("attachments", [])
        return {
            "id": str(row["id"]),
            "direction": row["direction"],
            "actor_type": row["actor_type"],
            "text": row["text"],
            "occurred_at": row["occurred_at"],
            "branch_id": payload.get("branch_id"),
            "customer_branch_id": payload.get("customer_branch_id") or payload.get("crm_branch"),
            # CRM bigint keys and timestamps with microseconds must not lose
            # precision when the response is parsed by JavaScript.
            "crm_order": str(payload["crm_order"]) if payload.get("crm_order") is not None else None,
            "sort_time": str((row["occurred_at"] - datetime(1970, 1, 1, tzinfo=UTC))
                             // timedelta(microseconds=1)),
            "version": row["version"],
            "attachments": [item if isinstance(item, str) else "file" for item in attachments],
            "attachment_names": [item.get("name", "") if isinstance(item, dict) else ""
                                 for item in attachments],
            "edited": payload.get("edited", False),
            "deleted": payload.get("deleted", False),
            "superseded": bool(payload.get("superseded_by")),
            "forwarded": payload.get("forwarded", False),
            "state": "failed" if payload.get("failed") else row["send_state"] or (
                      "accepted" if row["direction"] == "out" else "received"),
            "error": ERRORS.get(row["last_error_code"]),
            "request_id": row["client_request_id"],
            "template_name": payload.get("template_name"),
        }

    @staticmethod
    def _messages(db, where, params, suffix=""):
        return list(
            db.execute(
                text(
                    "SELECT m.*,s.state AS send_state,s.last_error_code,s.client_request_id "
                    "FROM messages m "
                    "LEFT JOIN send_commands s ON s.workspace_id=m.workspace_id AND "
                    "s.id=m.send_command_id WHERE " + where + " " + suffix
                ),
                params,
            ).mappings()
        )

    def listing(
        self,
        actor,
        workspace,
        *,
        connection=None,
        view="all",
        query="",
        branch=None,
        status=None,
        page=0,
        through_page=None,
        window=None,
        window_token=None,
    ):
        with self.max.chat_transaction(actor, workspace, snapshot=True) as db:
            # Avoid per-poll JIT compilation of the bounded correlated policy.
            db.execute(text("SET LOCAL jit=off"))
            if through_page is not None:
                page = through_page
            snapshot = DirectoryReader._published(db, workspace)
            writable = self._can_write(db, actor, workspace)
            params = {
                "w": workspace,
                "offset": 0 if through_page is not None else page * 30,
                "limit": (page + 1) * 30 if through_page is not None else 30,
                "connection": connection,
                "branch": branch,
                "status": status,
                "query": search_query(query),
                "like": "%"
                + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%",
            }
            params.update(card_preference.params(snapshot))
            previous, window_scope, fingerprint = None, None, None
            if window is not None:
                params.update(offset=0, limit=window)
                window_scope = (str(workspace), str(getattr(actor, "user_id", None)),
                         str(connection), view, query, branch, status, writable)
                # Check authorization on every request, even unchanged views.
                # This is bounded to lightweight indexed/derived rows; no message
                # policy or finance requests run on an unchanged poll.
                fingerprint = chat_revision.fingerprint(db, workspace)
                previous = self.list_windows.previous(window_token, window_scope)
                unchanged = self.list_windows.unchanged(previous, fingerprint, window)
                if unchanged:
                    return unchanged
            where = "c.workspace_id=:w AND c.kind='direct' AND "
            where += ("(c.peer_user_id IS NOT NULL OR (cn.kind='alfa_max_browser' "
                      "AND am.connection_id IS NOT NULL AND EXISTS(SELECT 1 FROM messages available WHERE available.workspace_id=c.workspace_id AND available.conversation_id=c.id)))")
            if connection:
                where += " AND c.connection_id=:connection"
            if snapshot:
                params.update(run=snapshot["run"], crm=snapshot["connection_id"])
                scope = (
                    "a.workspace_id=c.workspace_id AND a.run_id=:run AND a.contact_id=ANY(" + card_preference.family_sql() + ") "
                    "AND EXISTS (SELECT 1 FROM alfa_branches b WHERE b.workspace_id=a.workspace_id "
                    "AND b.connection_id=:crm AND b.external_id=a.branch_id AND b.available)"
                )
                if branch:
                    scope += " AND a.branch_id=:branch"
                if branch:
                    where += (" AND (EXISTS (SELECT 1 FROM alfa_customers a WHERE " + scope + ") OR "
                        "(cn.kind='alfa_max_browser' AND c.contact_id IS NULL "
                        "AND c.external_chat_id LIKE concat('alfa-customer:',CAST(:branch AS text),':%') "
                        "AND EXISTS(SELECT 1 FROM alfa_branches known WHERE known.workspace_id=c.workspace_id "
                        "AND known.connection_id=:crm AND known.external_id=:branch AND known.available)))")
                where += (
                    " AND (cn.kind <> 'alfa_max_browser' OR am.crm_connection_id=:crm)"
                )
            else:
                if branch:
                    where += " AND false"
            if query.strip():
                where += " AND (c.peer_name ILIKE :like OR p.display_name ILIKE :like"
                if snapshot and params["query"]:
                    where += (
                        " OR EXISTS(SELECT 1 FROM alfa_directory d WHERE "
                        "d.workspace_id=c.workspace_id "
                        "AND d.run_id=:run AND d.contact_id=ANY(" + card_preference.family_sql() + ") "
                        "AND d.search_vector @@ to_tsquery('simple',:query))"
                    )
                where += ")"
            joins = (
                "FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id "
                "AND cn.id=c.connection_id LEFT JOIN max_settings s ON "
                "s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id "
                "LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id "
                "AND am.connection_id=c.connection_id " + card_preference.join_sql(snapshot) + " LEFT JOIN contacts p ON "
                "p.workspace_id=c.workspace_id AND p.id=" + card_preference.contact_sql() + " "
            )
            live_repair = db.execute(text("SELECT EXISTS(SELECT 1 FROM connections cn WHERE "
                "cn.workspace_id=:w AND cn.kind IN ('max_bot','alfa_max_browser') AND NOT coalesce("
                + QUEUE_READY + ",false))"), {"w": workspace}).scalar_one()
            queue_unread, queue_priority = "c.needs_reply", "c.last_message_at"
            fallback_joins = joins
            if live_repair:
                queue_unread, queue_priority = EFFECTIVE_UNREAD, PRIORITY_AT
                fallback_joins += " LEFT JOIN LATERAL (" + queue_state_sql(enabled="NOT coalesce(" + QUEUE_READY + ",false)") + ") q ON NOT coalesce(" + QUEUE_READY + ",false) "
            if status:
                where += (" AND EXISTS (SELECT 1 FROM alfa_customers a WHERE " + scope + " AND a.status=:status)"
                          if snapshot else " AND false")
            if view == "unread":
                where += " AND " + queue_unread
            if view == "unlinked":
                where += " AND c.contact_id IS NULL"
            counts = (
                db.execute(
                    text(
                        "SELECT count(*) AS total,count(*) FILTER(WHERE "
                        + queue_unread
                        + ") AS unread "
                        + fallback_joins
                        + " WHERE "
                        + where
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            # Choose a complete ordered prefix using cached dates. The repair
            # fallback reads live policy only for connections not yet repaired.
            selected_sql = ("SELECT c.id," + queue_unread + " AS effective_unread,"
                + queue_priority + " AS priority_at " + fallback_joins + " WHERE " + where
                + " ORDER BY effective_unread DESC,priority_at DESC NULLS LAST,c.id LIMIT :limit OFFSET :offset")
            rows = list(db.execute(text(
                "WITH selected AS MATERIALIZED (" + selected_sql + ") SELECT c.*,cn.name AS channel_name,"
                "coalesce(cp.payload->>'name',p.display_name,c.peer_name) AS display_name,cn.kind AS channel_kind,"
                "cp.payload AS card_selection," + card_preference.contact_sql() + " AS family_contact_id,"
                "am.crm_connection_id,am.history_cursor AS crm_history_cursor,am.state AS crm_history_state,"
                "am.last_error_code AS crm_history_error,"
                "CASE WHEN cn.kind='alfa_max_browser' THEN (am.state='active' "
                "AND am.session_ciphertext IS NOT NULL AND c.contact_id IS NOT NULL "
                "AND c.last_inbound_at IS NOT NULL AND EXISTS(SELECT 1 FROM alfa_settings ax "
                "WHERE ax.workspace_id=c.workspace_id AND ax.connection_id=am.crm_connection_id "
                "AND ax.secret_ciphertext IS NOT NULL)) ELSE (s.enabled AND s.subscription_confirmed) END AS can_send,"
                "cn.status AS channel_status,q.needs_reply AS effective_unread,q.latest_inbound_id,"
                "(SELECT coalesce(max(seq),0) FROM message_changes ch WHERE ch.workspace_id=c.workspace_id "
                "AND ch.conversation_id=c.id) AS seen_change_seq,"
                + QUEUE_COLUMNS + " " + joins + " JOIN selected ON selected.id=c.id "
                "CROSS JOIN LATERAL (" + cached_state_sql() + ") q "
                "ORDER BY selected.effective_unread DESC,selected.priority_at DESC NULLS LAST,c.id"
            ), params).mappings())
            from center_messenger.customer_lookup import enrich
            rows = enrich(db, workspace, rows)
            families = {}
            contacts = list({UUID(value) for row in rows if row["contact_id"] for value in
                (row["card_selection"]["contacts"] if row["card_selection"] else [str(row["contact_id"])])})
            if snapshot and contacts:
                params["ids"] = contacts
                families = {
                    row["contact_id"]: row
                    for row in db.execute(
                        text(
                            "SELECT a.contact_id,(array_agg(DISTINCT a.name ORDER BY "
                            "a.name))[1:50] AS names,"
                            "bool_or(nullif(a.legal_name,'') IS NOT NULL) AS has_parent,"
                            "jsonb_agg(DISTINCT jsonb_build_object('id',coalesce(a.student_id::text,a.branch_id::text||':'||a.external_id::text),'name',a.name,'crm_id',a.external_id::text)) AS children,"
                            "max(CASE a.status WHEN 'client' THEN 3 WHEN 'lead' THEN 2 ELSE "
                            "1 END) AS weight "
                            "FROM alfa_customers a WHERE a.workspace_id=:w AND a.run_id=:run "
                            "AND a.contact_id=ANY(CAST(:ids AS uuid[])) "
                            "AND EXISTS(SELECT 1 FROM alfa_branches b WHERE "
                            "b.workspace_id=a.workspace_id "
                            "AND b.connection_id=:crm AND b.external_id=a.branch_id AND "
                            "b.available) "
                            + ("AND a.branch_id=:branch " if branch else "")
                            + "GROUP BY a.contact_id"
                        ),
                        params,
                    ).mappings()
                }
            result = []
            for row in rows:
                choice = row["card_selection"]
                targets = choice["contacts"] if choice else [str(row["contact_id"])] if row["contact_id"] else []
                parts = [families.get(UUID(value), {}) for value in targets]
                hidden = set(choice.get("hidden_customers", [])) if choice else set()
                children = {child["id"]: child for part in parts for child in part.get("children", []) if child.get("crm_id") not in hidden}
                family = {"names": sorted({child["name"] for child in children.values()}), "children": list(children.values()),
                          "has_parent": any(part.get("has_parent") for part in parts),
                          "weight": max((part.get("weight", 0) for part in parts), default=0)}
                result.append(
                    self._meta(row)
                    | {
                        "name": row["display_name"],

                        "students": family.get("names", []),
                        "parent_name": row["display_name"] if family.get("has_parent") else None,
                        "children": family.get("children", []),
                        "unread": row["effective_unread"],
                        "unread_count": row["unanswered_count"],
                        "handled": row["handled_inbound_id"] == row["latest_inbound_id"]
                        and bool(row["needs_reply"]),
                        "status": {3: "client", 2: "lead", 1: "archive"}.get(
                            family.get("weight"), "unlinked"
                        ),

                        "can_send": bool(self._meta(row)["can_send"] and writable),
                    }
                )
            payload = {
                "version": fingerprint,
                "conversations": result,
                "total": counts["total"],
                "unread": counts["unread"],
                "lead_unread": 0,
                "page": page,
                "has_more": (window if window is not None else (page + 1) * 30) < counts["total"],
            }
            return self.list_windows.publish(previous, window_scope, fingerprint, window, payload) if window is not None else payload

    def history(
        self, actor, workspace, conversation, *, before_time=None, before_id=None, after_seq=None, limit=20
    ):
        if not 20 <= limit <= 60:
            raise AuthError(422, "Размер страницы истории должен быть от 20 до 60 сообщений.")
        with self.max.chat_transaction(actor, workspace, snapshot=True) as db:
            # Message writers update this policy projection in the same
            # transaction. Do not recalculate the entire reply history on every
            # read. The SQL falls back to live policy when a projection is absent;
            # mutations retain their locked live-policy authorization below.
            row = self._conversation(db, workspace, conversation, cached=True)
            conversation = row["id"]
            params = {"w": workspace, "c": conversation}
            end = db.execute(
                text(
                    "SELECT coalesce(max(seq),0) FROM message_changes WHERE workspace_id=:w "
                    "AND conversation_id=:c"
                ),
                params,
            ).scalar_one()
            where = "m.workspace_id=:w AND m.conversation_id=:c"
            total = row["stored_message_count"]
            removed_ids = []
            reset = after_seq is not None and after_seq > end
            if reset:
                after_seq = None
            has_more, more_changes = False, False
            if after_seq is not None:
                params["after"] = min(after_seq, end)
                changes = list(
                    db.execute(
                        text(
                            "SELECT seq,message_id FROM message_changes WHERE workspace_id=:w "
                            "AND conversation_id=:c AND seq>:after ORDER BY seq LIMIT 101"
                        ),
                        params,
                    ).mappings()
                )
                more_changes = len(changes) > 100
                changes = changes[:100]
                params["ids"] = list({c["message_id"] for c in changes})
                records = (
                    self._messages(db, where + " AND m.id=ANY(CAST(:ids AS uuid[]))", params)
                    if changes
                    else []
                )
                present = {r["id"] for r in records if not r["payload"].get("superseded_by")}
                removed_ids = [str(identifier) for identifier in params["ids"] if identifier not in present]
                records = [r for r in records if not r["payload"].get("superseded_by")]
                next_seq = changes[-1]["seq"] if more_changes else end
            else:
                if (before_time is None) != (before_id is None):
                    raise AuthError(422, "Для загрузки истории нужен полный указатель страницы.")
                if before_time is not None:
                    anchor = db.execute(
                        text("SELECT m.occurred_at," + source_order() + " AS source_order "
                             "FROM messages m WHERE m.workspace_id=:w AND m.conversation_id=:c "
                             "AND m.id=:id"), params | {"id": before_id},
                    ).mappings().one_or_none()
                    if anchor is None:
                        # A background repair may move the boundary message to
                        # its correct client. Restart with a consistent snapshot.
                        reset = True
                    else:
                        params.update(before_time=anchor["occurred_at"], before_id=before_id,
                                      before_order=anchor["source_order"])
                        where += (" AND (m.occurred_at," + source_order() + ",m.id)"
                                  "<(:before_time,:before_order,:before_id)")
                where += " AND m.payload->>'superseded_by' IS NULL"
                records = self._messages(
                    db, where, params | {"limit": limit + 1}, "ORDER BY " + message_order(descending=True) + " LIMIT :limit"
                )
                has_more = len(records) > limit
                records = list(reversed(records[:limit]))
                next_seq = end
            return {
                "conversation": self._meta(row) | {"can_send": bool(self._meta(row)["can_send"] and self._can_write(db, actor, workspace))},
                "messages": [self._message(r) for r in records],
                "total": total,
                "removed_ids": removed_ids,
                "reset": reset,
                "next_seq": next_seq,
                "has_more": has_more,
                "more_changes": more_changes,
            }

    def send(self, actor, workspace, conversation, model):
        self.max.accounts.throttle(f"chat-send:{workspace}:{actor.user_id}", limit=60, minutes=1)
        with self.max.chat_transaction(actor, workspace, lock=True) as db:
            self._require_write(db, actor, workspace)
            row = self._conversation(db, workspace, conversation, lock="UPDATE")
            conversation = row["id"]
            payload = {"text": model.text, "conversation_id": str(conversation)}
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            old = (
                db.execute(
                    text(
                        "SELECT * FROM send_commands WHERE workspace_id=:w AND "
                        "client_request_id=:request"
                    ),
                    {"w": workspace, "request": str(model.request_id)},
                )
                .mappings()
                .one_or_none()
            )
            if old:
                if old["conversation_id"] != conversation or old["payload"].get("text") != model.text:
                    raise AuthError(409, "Этот запрос уже использован для другого сообщения.")
                return {
                    "command_id": str(old["id"]),
                    "state": old["state"],
                    "request_id": str(model.request_id),
                }
            checkpoint = self._validate_checkpoint(db, workspace, row, model.seen_checkpoint)
            payload["reply_checkpoint_version"] = 1
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if row["inbound_seq"] != model.seen_inbound_seq or checkpoint.latest_inbound_id != row["latest_inbound_id"]:
                raise AuthError(409, "Поступило новое сообщение. Прочитайте его перед ответом.")
            if not self._meta(row)["can_send"]:
                raise AuthError(
                    409, "Проверьте подключение выбранного канала в разделе «Подключения»."
                )
            command, message = uuid4(), uuid4()
            db.execute(
                text(
                    "INSERT INTO "
                    "send_commands(workspace_id,id,conversation_id,client_request_id,payload,payload_hash,"
                    "requested_by,reply_through_seq) "
                    "VALUES (:w,:id,:c,:request,CAST(:p AS jsonb),:hash,:u,:seen)"
                ),
                {
                    "w": workspace,
                    "id": command,
                    "c": conversation,
                    "request": str(model.request_id),
                    "p": encoded,
                    "hash": fingerprint(encoded),
                    "u": actor.user_id,
                    "seen": checkpoint.change_seq,
                },
            )
            db.execute(
                text(
                    "INSERT INTO "
                    "messages(workspace_id,id,conversation_id,direction,actor_type,"
                    "text,payload,occurred_at,send_command_id) "
                    "VALUES (:w,:id,:c,'out','human',:text,CAST(:p AS jsonb),now(),:cmd)"
                ),
                {
                    "w": workspace,
                    "id": message,
                    "c": conversation,
                    "text": model.text,
                    "p": json.dumps({
                        "source": "center_messenger",
                        "actor_evidence": {"version": 1, "reason": "authorized_send"},
                        "actor_id": str(actor.user_id),
                    }),
                    "cmd": command,
                },
            )
            changed(db, workspace, conversation, message)
            db.execute(
                text(
                    "UPDATE conversations SET revision=revision+1 "
                    "WHERE workspace_id=:w AND id=:c"
                ),
                {"w": workspace, "c": conversation},
            )
            self.messaging.enqueue(
                db,
                workspace,
                row["connection_id"],
                command,
                "send",
                row["channel_kind"],
            )
            audit(
                db,
                "chat_send_queued",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=command,
            )
        return {"command_id": str(command), "state": "queued", "request_id": str(model.request_id)}

    def link(self, actor, workspace, conversation, model):
        with self.max.chat_transaction(actor, workspace, lock=True) as db:
            self._require_write(db, actor, workspace)
            row = self._conversation(db, workspace, conversation, lock="UPDATE")
            conversation = row["id"]
            if model.contact_id:
                snapshot = DirectoryReader._published(db, workspace)
                exists = (
                    snapshot
                    and db.execute(
                        text(
                            "SELECT contact_id FROM alfa_directory WHERE workspace_id=:w "
                            "AND run_id=:r AND contact_id=:id"
                        ),
                        {"w": workspace, "r": snapshot["run"], "id": model.contact_id},
                    ).scalar_one_or_none()
                )
                if not exists:
                    raise AuthError(404, "Контакт недоступен в текущем справочнике CRM.")
            db.execute(
                text(
                    "UPDATE conversations SET contact_id=:id,crm_link_manual=true,revision=revision+1 WHERE "
                    "workspace_id=:w AND id=:c"
                ),
                {"w": workspace, "c": conversation, "id": model.contact_id},
            )
            audit(
                db,
                "chat_contact_linked",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=conversation,
            )

    @staticmethod
    def _validate_checkpoint(db, workspace, row, checkpoint):
        if checkpoint is None or checkpoint.conversation_id != row["id"] or checkpoint.revision > row["revision"] or checkpoint.change_seq > row["seen_change_seq"]:
            raise AuthError(409, "Обновите переписку перед ответом или обработкой.")
        latest = db.execute(text(
            "SELECT m.id FROM messages m WHERE m.workspace_id=:w AND m.conversation_id=:c "
            "AND m.direction='in' AND m.actor_type='contact' AND " + visible_message() +
            " AND EXISTS(SELECT 1 FROM message_changes ch WHERE ch.workspace_id=m.workspace_id "
            "AND ch.message_id=m.id AND ch.conversation_id=:c AND ch.seq<=:seen) ORDER BY " +
            message_order(descending=True) + " LIMIT 1"
        ), {"w": workspace, "c": row["id"], "seen": checkpoint.change_seq}).scalar_one_or_none()
        if latest != checkpoint.latest_inbound_id:
            raise AuthError(409, "Граница просмотренных сообщений изменилась. Обновите переписку.")
        return checkpoint

    def handle(self, actor, workspace, conversation, model):
        with self.max.chat_transaction(actor, workspace, lock=True) as db:
            self._require_write(db, actor, workspace)
            row = self._conversation(db, workspace, conversation, lock="UPDATE")
            conversation = row["id"]
            checkpoint = self._validate_checkpoint(db, workspace, row, model.seen_checkpoint)
            if checkpoint.latest_inbound_id is None:
                raise AuthError(409, "Нет просмотренных входящих для обработки.")
            self._apply_handled(db, workspace, row, checkpoint, actor.user_id)
            audit(db, "chat_marked_handled", user_id=actor.user_id, workspace_id=workspace, subject_id=conversation)
            meta = self._meta(self._conversation(db, workspace, conversation))
            # An acknowledgement carries current counters, not newly displayed
            # history. Retrying it must keep the original seen boundary.
            meta["seen_checkpoint"] = checkpoint.model_dump(mode="json")
            return {"conversation": meta}

    @staticmethod
    def _apply_handled(db, workspace, row, checkpoint, user, *, known_ids=None):
        conversation = row["id"]
        materialize_legacy_handled(db, workspace, conversation)
        # Receipts belong to the actual known messages. They survive verified
        # family merges without granting coverage over another dialog.
        identifiers = list(db.execute(text(
            "UPDATE messages m SET payload=m.payload||jsonb_build_object('handled_receipt',"
            "jsonb_build_object('version',1,'through_seq',CAST(:seen AS bigint),'user_id',CAST(:user AS text))),"
            "version=m.version+1 WHERE m.workspace_id=:w AND m.conversation_id=:c "
            "AND m.direction='in' AND m.actor_type='contact' AND " + visible_message() +
            " AND m.payload->>'handled_receipt' IS NULL AND (:bounded=false OR m.id=ANY(CAST(:known_ids AS uuid[]))) "
            "AND EXISTS(SELECT 1 FROM message_changes ch "
            "WHERE ch.workspace_id=m.workspace_id AND ch.message_id=m.id "
            "AND ch.conversation_id=:c AND ch.seq<=:seen) RETURNING m.id"
        ), {"w": workspace, "c": conversation, "seen": checkpoint.change_seq, "user": user,
            "bounded": known_ids is not None, "known_ids": known_ids or []}).scalars())
        db.execute(text("UPDATE conversations SET handled_inbound_id=:id,handled_at=now(),"
            "handled_by=:user,answered_seq=greatest(answered_seq,:seen,1),revision=revision+1 "
            "WHERE workspace_id=:w AND id=:c"), {"w": workspace, "c": conversation,
            "id": checkpoint.latest_inbound_id, "seen": checkpoint.change_seq, "user": user})
        if identifiers:
            db.execute(text("INSERT INTO message_changes(workspace_id,conversation_id,message_id,new_inbound) "
                "SELECT :w,:c,changed.id,false FROM unnest(CAST(:ids AS uuid[])) AS changed(id)"),
                {"w": workspace, "c": conversation, "ids": identifiers})
        refresh_unread(db, workspace, conversation)
        return len(identifiers)

    def prepare_bulk_handled(self, actor, workspace):
        return bulk_handled.prepare(self, actor, workspace)

    def confirm_bulk_handled(self, actor, workspace, model):
        return bulk_handled.confirm(self, actor, workspace, model)


def components(request):
    service(request)
    return request.app.state.max_connections, request.app.state.chats


@router.get("/integrations/max")
def channels(workspace_id: UUID, request: Request):
    connections, _ = components(request)
    result = connections.listing(identity(request), workspace_id)
    if request.app.state.alfa_max:
        result["channels"].extend(
            request.app.state.alfa_max.channels(identity(request), workspace_id)
        )
    return result


@router.post("/integrations/max", status_code=201)
def new_channel(workspace_id: UUID, model: MaxChannelInput, request: Request):
    connections, _ = components(request)
    return {"id": connections.create(identity(request, mutation=True), workspace_id, model)}


@router.post("/integrations/max/{connection_id}/check")
def check_channel(workspace_id: UUID, connection_id: UUID, request: Request):
    connections, _ = components(request)
    return connections.check(identity(request, mutation=True), workspace_id, connection_id)


@router.post("/integrations/max/{connection_id}/enable")
def enable_channel(workspace_id: UUID, connection_id: UUID, request: Request):
    connections, _ = components(request)
    return connections.enable(identity(request, mutation=True), workspace_id, connection_id)


@router.post("/integrations/max/{connection_id}/disable", status_code=204)
def disable_channel(workspace_id: UUID, connection_id: UUID, request: Request):
    connections, _ = components(request)
    connections.disable(identity(request, mutation=True), workspace_id, connection_id)


@router.put("/integrations/max/{connection_id}", status_code=204)
def update_channel(
    workspace_id: UUID, connection_id: UUID, model: MaxSettingsInput, request: Request
):
    connections, _ = components(request)
    connections.rotate(identity(request, mutation=True), workspace_id, connection_id, model)


@router.get("/chats")
def chats(
    workspace_id: UUID,
    request: Request,
    connection: UUID | None = None,
    view: Literal["all", "unread", "unlinked"] = "all",
    q: str = Query(default="", max_length=200),
    branch: int | None = Query(default=None, gt=0, le=9223372036854775807),
    status: Literal["client", "lead", "archive"] | None = None,
    page: int = Query(default=0, ge=0, le=2000),
    through_page: int | None = Query(default=None, ge=0, le=2000),
    window: int | None = Query(default=None, ge=30, le=60000),
    window_token: UUID | None = None,
):
    _, reader = components(request)
    return reader.listing(
        identity(request),
        workspace_id,
        connection=connection,
        view=view,
        query=q,
        branch=branch,
        status=status,
        page=page,
        through_page=through_page,
        window=window,
        window_token=str(window_token) if window_token else None,
    )


@router.get("/chats/context")
def chat_context(workspace_id: UUID, request: Request):
    _, reader = components(request)
    return reader.context(identity(request), workspace_id)


@router.get("/chats/bootstrap")
def chat_bootstrap(workspace_id: UUID, request: Request,
                   connection: UUID | None = None, q: str = Query(default="", max_length=200),
                   branch: int | None = Query(default=None, gt=0, le=9223372036854775807),
                   status: Literal["client", "lead", "archive"] | None = None):
    _, reader = components(request)
    return reader.bootstrap(identity(request), workspace_id, connection=connection, query=q, branch=branch, status=status)


@router.put("/chats/preferences")
def chat_display_preferences(workspace_id: UUID, model: ChatPreferencesInput, request: Request):
    _, reader = components(request)
    actor = identity(request, mutation=True)
    with reader.max.chat_transaction(actor, workspace_id) as db:
        return chat_preferences.save(db, workspace_id, actor.user_id, model.name_order)


@router.get("/summary")
def cabinet_summary(workspace_id: UUID, request: Request):
    from center_messenger.cabinet_summary import summary
    _, reader = components(request)
    return summary(reader.max, identity(request), workspace_id)


@router.get("/chat-updates")
def updates(
    workspace_id: UUID, request: Request, after_seq: int | None = Query(default=None, ge=0)
):
    _, reader = components(request)
    return reader.updates(identity(request), workspace_id, after_seq)


@router.get("/chat-events")
async def chat_events(workspace_id: UUID, request: Request):
    from center_messenger.chat_events import response
    _, reader = components(request)
    actor = await run_in_threadpool(identity, request)
    return await response(request, reader, actor, workspace_id)


def _chat_contact_role(request, workspace):
    _, reader = components(request)
    actor = identity(request)
    with reader.max.chat_transaction(actor, workspace, snapshot=True) as db:
        if reader.max.accounts._workspace(db, actor, workspace)["role"] == "operator":
            raise AuthError(403, "Откройте карточку семьи из её диалога.")
    return reader, actor


@router.get("/chat-contacts/sync")
def chat_contacts_status(workspace_id: UUID, request: Request):
    reader, actor = _chat_contact_role(request, workspace_id)
    with reader.max.chat_transaction(actor, workspace_id, snapshot=True) as db:
        snapshot = DirectoryReader._published(db, workspace_id)
        return {"configured": bool(snapshot), "state": "succeeded" if snapshot else "not_started",
                "run_id": str(snapshot["run"]) if snapshot else None,
                "last_success_at": snapshot["finished_at"] if snapshot else None,
                "stats": {}, "phase": None, "error": None, "auto_refresh": False, "interval_minutes": 30}


@router.get("/chat-contacts")
def chat_contacts(workspace_id: UUID, request: Request,
                  q: str = Query(default="", max_length=200),
                  branch: int | None = Query(default=None, gt=0, le=9223372036854775807),
                  status: str | None = Query(default=None, pattern="^(client|lead|archive)$"),
                  max_bot_status: str = Query(default="connected_first", pattern="^(all|connected|disconnected|connected_first|disconnected_first)$"),
                  page: int = Query(default=0, ge=0, le=2000)):
    _, actor = _chat_contact_role(request, workspace_id)
    return request.app.state.directory.listing(actor, workspace_id, query=q, branch=branch,
                                              status=status, max_bot_status=max_bot_status,
                                              page=page, chat_only=True)


@router.get("/chat-contacts/contacts/{contact_id}")
def chat_contact_detail(workspace_id: UUID, contact_id: UUID, request: Request,
                        branch: int | None = Query(default=None, gt=0, le=9223372036854775807)):
    reader, actor = _chat_contact_role(request, workspace_id)
    with reader.max.chat_transaction(actor, workspace_id, snapshot=True) as db:
        conversation = db.execute(text(
            "SELECT c.id FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
            "WHERE c.workspace_id=:w AND c.contact_id=:contact AND c.kind='direct' AND "
            "((cn.kind='max_bot' AND EXISTS(SELECT 1 FROM max_settings s WHERE s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id)) "
            "OR (cn.kind='alfa_max_browser' AND EXISTS(SELECT 1 FROM alfa_max_settings s WHERE s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id))) "
            "ORDER BY c.created_at,c.id LIMIT 1"
        ), {"w": workspace_id, "contact": contact_id}).scalar_one_or_none()
        if conversation is None:
            raise AuthError(404, "Контакт недоступен в ваших диалогах.")
    return request.app.state.directory.detail(actor, workspace_id, contact_id, branch=branch, conversation=conversation)


@router.get("/chats/{conversation_id}/messages")
def history(
    workspace_id: UUID,
    conversation_id: UUID,
    request: Request,
    before_time: datetime | None = None,
    before_id: UUID | None = None,
    after_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=20, le=60),
):
    _, reader = components(request)
    if after_seq is not None and (before_time is not None or before_id is not None):
        raise AuthError(422, "Выберите один способ загрузки сообщений.")
    return reader.history(
        identity(request),
        workspace_id,
        conversation_id,
        before_time=before_time,
        before_id=before_id,
        after_seq=after_seq,
        limit=limit,
    )


@router.get("/chats/{conversation_id}/family")
def chat_family(workspace_id: UUID, conversation_id: UUID, request: Request, auto: bool = False,
                cached_only: bool = False, compact: bool = False,
                known_version: str | None = Query(default=None, pattern="^[0-9a-f]{64}$")):
    _, reader = components(request)
    actor = identity(request)
    with family_read_stage("family.resolve"):
        with reader.max.chat_transaction(actor, workspace_id, snapshot=True) as db:
            row = reader._family_conversation(db, workspace_id, conversation_id)
            contact, conversation = row["contact_id"], row["id"]
            if contact is None:
                raise AuthError(404, "Диалог пока не связан с карточкой CRM.")
    with family_read_stage("family.prepare"):
        return request.app.state.directory.prepare(actor, workspace_id, contact, conversation=conversation, auto=auto,
                                                   cached_only=cached_only, compact=compact, known_version=known_version)


@router.post("/chats/{conversation_id}/family/refresh", status_code=202)
def refresh_chat_family(workspace_id: UUID, conversation_id: UUID, request: Request, compact: bool = False):
    _, reader = components(request)
    actor = identity(request, mutation=True)
    with reader.max.chat_transaction(actor, workspace_id, snapshot=True) as db:
        row = reader._family_conversation(db, workspace_id, conversation_id)
        contact, conversation = row["contact_id"], row["id"]
        if contact is None:
            raise AuthError(404, "Диалог пока не связан с карточкой CRM.")
    return request.app.state.directory.prepare(actor, workspace_id, contact, conversation=conversation, force=True, compact=compact)


@router.post("/chats/{conversation_id}/messages", status_code=202)
def send(workspace_id: UUID, conversation_id: UUID, model: SendInput, request: Request):
    _, reader = components(request)
    return reader.send(identity(request, mutation=True), workspace_id, conversation_id, model)


@router.post("/chats/handled/prepare")
def prepare_bulk_handled(workspace_id: UUID, request: Request):
    _, chats = components(request)
    return chats.prepare_bulk_handled(identity(request, mutation=True), workspace_id)


@router.post("/chats/handled/confirm")
def confirm_bulk_handled(workspace_id: UUID, request: Request, model: BulkHandleInput):
    _, chats = components(request)
    return chats.confirm_bulk_handled(identity(request, mutation=True), workspace_id, model)


@router.post("/chats/{conversation_id}/handled")
def mark_handled(workspace_id: UUID, conversation_id: UUID, request: Request, model: HandleInput | None = None):
    if model is None:
        raise AuthError(409, "Обновите страницу перед обработкой сообщений.")
    _, reader = components(request)
    return reader.handle(identity(request, mutation=True), workspace_id, conversation_id, model)


@router.put("/chats/{conversation_id}/contact", status_code=204)
def link(workspace_id: UUID, conversation_id: UUID, model: LinkInput, request: Request):
    _, reader = components(request)
    reader.link(identity(request, mutation=True), workspace_id, conversation_id, model)


@hooks.post("/hooks/max/{connection_id}")
async def webhook(connection_id: UUID, request: Request):
    service(request)
    secret = request.headers.get("x-max-bot-api-secret", "")
    if not 5 <= len(secret) <= 256:
        raise AuthError(403, "Неверный секрет webhook.")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise AuthError(415, "Ожидается JSON.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 262144:
            raise AuthError(413, "Слишком большое событие.")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        raise AuthError(400, "Некорректный JSON.") from None
    # Database IO runs in a thread, not the ASGI event loop.
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(
        request.app.state.messaging.receive, connection_id, secret, payload
    )
