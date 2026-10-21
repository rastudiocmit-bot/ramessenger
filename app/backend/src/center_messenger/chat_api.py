"""Workspace-scoped chat API. Operator access waits for branch/channel ACL."""

import json
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.accounts_api import identity, service
from center_messenger.alfa_max import ERRORS as ALFA_MAX_ERRORS
from center_messenger.auth_security import AuthError, InputModel
from center_messenger.chat_store import changed
from center_messenger.crm_api import DirectoryReader, search_query
from center_messenger.max_client import ERRORS as MAX_ERRORS
from center_messenger.max_client import MaxChannelInput, MaxSettingsInput
from center_messenger.max_connections import fingerprint

router = APIRouter(prefix="/api/workspaces/{workspace_id}")
hooks = APIRouter()
ERRORS = {**MAX_ERRORS, **ALFA_MAX_ERRORS}

LATEST_INBOUND_ID = """(
  SELECT mi.id FROM messages mi
  WHERE mi.workspace_id=c.workspace_id AND mi.conversation_id=c.id
    AND mi.direction='in' AND mi.actor_type='contact'
    AND coalesce(mi.payload->>'deleted','false')<>'true'
  ORDER BY mi.occurred_at DESC,
    CASE WHEN mi.payload->>'source_ms' ~ '^[0-9]{1,19}$'
      THEN (mi.payload->>'source_ms')::numeric END DESC NULLS LAST,
    CASE WHEN mi.payload->>'crm_order' ~ '^[0-9]{1,19}$'
      THEN (mi.payload->>'crm_order')::numeric END DESC NULLS LAST,
    mi.id DESC LIMIT 1
)"""
EFFECTIVE_UNREAD = (
    "(c.needs_reply AND c.handled_inbound_id IS DISTINCT FROM " + LATEST_INBOUND_ID + ")"
)


class SendInput(InputModel):
    request_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    seen_inbound_seq: int = Field(ge=0)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Empty message")
        return value.strip()


class LinkInput(InputModel):
    contact_id: UUID | None


class ChatReader:
    def __init__(self, messaging):
        self.messaging, self.max = messaging, messaging.max
        self.alfa_max = messaging.alfa_max

    def updates(self, actor, workspace, after=None):
        with self.max.chat_transaction(actor, workspace) as db:
            # Every projector/send takes the workspace lock before allocating a
            # change sequence. The read lock prevents skipping an uncommitted one.
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR SHARE"), {"w": workspace})
            end = db.execute(
                text("SELECT coalesce(max(seq),0) FROM message_changes WHERE workspace_id=:w"),
                {"w": workspace},
            ).scalar_one()
            if after is None:
                return {"next_seq": end, "updates": [], "has_more": False}
            snapshot = DirectoryReader._published(db, workspace)
            update_params = {"w": workspace, "after": min(after, end)}
            eligible_channel = "cn.kind='max_bot'"
            if snapshot:
                update_params.update(run=snapshot["run"], crm=snapshot["connection_id"])
                eligible_channel = (
                    "cn.kind='max_bot' OR (cn.kind='alfa_max_browser' "
                    "AND c.contact_id IS NOT NULL AND c.last_inbound_at IS NOT NULL "
                    "AND am.crm_connection_id=:crm AND EXISTS(SELECT 1 FROM alfa_directory d "
                    "WHERE d.workspace_id=c.workspace_id AND d.run_id=:run "
                    "AND d.contact_id=c.contact_id))"
                )
            rows = list(
                db.execute(
                    text(
                        "SELECT ch.seq,c.id,c.inbound_seq,c.contact_id,"
                        "coalesce(p.display_name,c.peer_name) AS name,m.direction,ch.new_inbound,"
                        + EFFECTIVE_UNREAD
                        + " AS effective_unread "
                        "FROM message_changes ch JOIN messages m ON m.workspace_id=ch.workspace_id "
                        "AND m.id=ch.message_id JOIN conversations c ON "
                        "c.workspace_id=m.workspace_id "
                        "AND c.id=m.conversation_id JOIN connections cn ON "
                        "cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
                        "LEFT JOIN max_settings s ON s.workspace_id=c.workspace_id "
                        "AND s.connection_id=c.connection_id LEFT JOIN alfa_max_settings am ON "
                        "am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
                        "LEFT JOIN contacts p ON "
                        "p.workspace_id=c.workspace_id AND p.id=c.contact_id "
                        "WHERE ch.workspace_id=:w AND ch.seq>:after AND ("
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
            for row in rows:
                latest[row["id"]] = row
                if row["direction"] == "in" and row["new_inbound"]:
                    new_inbounds.add(row["id"])
            weights = {}
            contacts = list({r["contact_id"] for r in latest.values() if r["contact_id"]})
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
                        "inbound_seq": r["inbound_seq"],
                        "unread": r["effective_unread"],
                        "new_inbound": r["id"] in new_inbounds,
                        "status": {3: "client", 2: "lead", 1: "archive"}.get(
                            weights.get(r["contact_id"]), "unlinked"
                        ),
                    }
                    for r in latest.values()
                ],
            }

    @staticmethod
    def _conversation(db, workspace, conversation, *, lock="SHARE"):
        row = (
            db.execute(
                text(
                    "SELECT c.*,cn.name AS channel_name,cn.kind AS channel_kind,"
                    "CASE WHEN cn.kind='alfa_max_browser' THEN "
                    "(am.state='active' AND am.session_ciphertext IS NOT NULL AND EXISTS("
                    "SELECT 1 FROM alfa_settings ax WHERE ax.workspace_id=c.workspace_id "
                    "AND ax.connection_id=am.crm_connection_id "
                    "AND ax.secret_ciphertext IS NOT NULL) AND c.contact_id IS NOT NULL "
                    "AND c.last_inbound_at IS NOT NULL) "
                    "ELSE (s.enabled AND s.subscription_confirmed) END AS can_send,cn.status AS "
                    "channel_status,"
                    + EFFECTIVE_UNREAD
                    + " AS effective_unread,"
                    + LATEST_INBOUND_ID
                    + " AS latest_inbound_id "
                    "FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id "
                    "AND cn.id=c.connection_id "
                    "LEFT JOIN max_settings s ON s.workspace_id=c.workspace_id "
                    "AND s.connection_id=c.connection_id LEFT JOIN alfa_max_settings am ON "
                    "am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
                    "WHERE c.workspace_id=:w AND c.id=:id AND c.kind='direct' AND "
                    "((cn.kind='max_bot' AND s.connection_id IS NOT NULL) OR "
                    "(cn.kind='alfa_max_browser' AND am.connection_id IS NOT NULL)) "
                    f"FOR {lock} OF c"
                ),
                {"w": workspace, "id": conversation},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuthError(404, "Диалог недоступен.")
        return row

    @staticmethod
    def _meta(row):
        return {
            "id": str(row["id"]),
            "connection_id": str(row["connection_id"]),
            "contact_id": str(row["contact_id"]) if row["contact_id"] else None,
            "peer_name": row["peer_name"],
            "channel_name": row["channel_name"],
            "unread": row["effective_unread"],
            "inbound_seq": row["inbound_seq"],
            "revision": row["revision"],
            "can_send": bool(row["can_send"]) and row["channel_status"] == "active",
        }

    @staticmethod
    def _message(row):
        payload = row["payload"]
        return {
            "id": str(row["id"]),
            "direction": row["direction"],
            "actor_type": row["actor_type"],
            "text": row["text"],
            "occurred_at": row["occurred_at"],
            "branch_id": payload.get("branch_id"),
            "crm_order": payload.get("crm_order"),
            "version": row["version"],
            "attachments": payload.get("attachments", []),
            "edited": payload.get("edited", False),
            "deleted": payload.get("deleted", False),
            "forwarded": payload.get("forwarded", False),
            "state": row["send_state"] or "received",
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
    ):
        with self.max.chat_transaction(actor, workspace) as db:
            snapshot = DirectoryReader._published(db, workspace)
            params = {
                "w": workspace,
                "offset": page * 30,
                "connection": connection,
                "branch": branch,
                "status": status,
                "query": search_query(query),
                "like": "%"
                + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%",
            }
            where = "c.workspace_id=:w AND c.kind='direct' AND "
            where += "(c.peer_user_id IS NOT NULL OR cn.kind='alfa_max_browser')"
            if connection:
                where += " AND c.connection_id=:connection"
            if view == "unread":
                where += " AND " + EFFECTIVE_UNREAD
            if view == "unlinked":
                where += " AND c.contact_id IS NULL"
            if snapshot:
                params.update(run=snapshot["run"], crm=snapshot["connection_id"])
                scope = (
                    "a.workspace_id=c.workspace_id AND a.run_id=:run AND a.contact_id=c.contact_id "
                    "AND EXISTS (SELECT 1 FROM alfa_branches b WHERE b.workspace_id=a.workspace_id "
                    "AND b.connection_id=:crm AND b.external_id=a.branch_id AND b.available)"
                )
                if branch:
                    scope += " AND a.branch_id=:branch"
                if status:
                    scope += " AND a.status=:status"
                if branch or status:
                    where += " AND EXISTS (SELECT 1 FROM alfa_customers a WHERE " + scope + ")"
                where += (
                    " AND (cn.kind <> 'alfa_max_browser' OR (c.contact_id IS NOT NULL "
                    "AND c.last_inbound_at IS NOT NULL AND am.crm_connection_id=:crm "
                    "AND EXISTS(SELECT 1 FROM alfa_directory d WHERE "
                    "d.workspace_id=c.workspace_id AND d.run_id=:run "
                    "AND d.contact_id=c.contact_id)))"
                )
            else:
                where += " AND cn.kind <> 'alfa_max_browser'"
                if branch or status:
                    where += " AND false"
            if query.strip():
                where += " AND (c.peer_name ILIKE :like OR p.display_name ILIKE :like"
                if snapshot and params["query"]:
                    where += (
                        " OR EXISTS(SELECT 1 FROM alfa_directory d WHERE "
                        "d.workspace_id=c.workspace_id "
                        "AND d.run_id=:run AND d.contact_id=c.contact_id "
                        "AND d.search_vector @@ to_tsquery('simple',:query))"
                    )
                where += ")"
            joins = (
                "FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id "
                "AND cn.id=c.connection_id LEFT JOIN max_settings s ON "
                "s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id "
                "LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id "
                "AND am.connection_id=c.connection_id LEFT JOIN contacts p ON "
                "p.workspace_id=c.workspace_id AND p.id=c.contact_id "
            )
            counts = (
                db.execute(
                    text(
                        "SELECT count(*) AS total,count(*) FILTER(WHERE "
                        + EFFECTIVE_UNREAD
                        + ") AS unread "
                        + joins
                        + " WHERE "
                        + where
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            rows = list(
                db.execute(
                    text(
                        "SELECT c.*,cn.name AS "
                        "channel_name,coalesce(p.display_name,c.peer_name) AS display_name,"
                        "CASE WHEN cn.kind='alfa_max_browser' THEN "
                        "(am.state='active' AND am.session_ciphertext IS NOT NULL AND EXISTS("
                        "SELECT 1 FROM alfa_settings ax WHERE ax.workspace_id=c.workspace_id "
                        "AND ax.connection_id=am.crm_connection_id "
                        "AND ax.secret_ciphertext IS NOT NULL) AND c.contact_id IS NOT NULL "
                        "AND c.last_inbound_at IS NOT NULL) "
                        "ELSE (s.enabled AND s.subscription_confirmed) END AS can_send,"
                        "cn.status AS channel_status,"
                        + EFFECTIVE_UNREAD
                        + " AS effective_unread,"
                        + LATEST_INBOUND_ID
                        + " AS latest_inbound_id,"
                        "(SELECT count(*) FROM messages i WHERE i.workspace_id=c.workspace_id "
                        "AND i.conversation_id=c.id AND i.direction='in' "
                        "AND i.actor_type='contact' "
                        "AND coalesce(i.payload->>'deleted','false')<>'true' "
                        "AND (c.handled_inbound_id IS NULL OR i.occurred_at > "
                        "(SELECT h.occurred_at FROM messages h WHERE h.workspace_id=c.workspace_id "
                        "AND h.id=c.handled_inbound_id)) "
                        "AND NOT EXISTS (SELECT 1 FROM messages r LEFT JOIN send_commands s ON "
                        "s.workspace_id=r.workspace_id AND s.id=r.send_command_id "
                        "WHERE r.workspace_id=i.workspace_id AND r.conversation_id=i.conversation_id "
                        "AND r.direction='out' AND r.actor_type='human' "
                        "AND coalesce(r.payload->>'deleted','false')<>'true' "
                        "AND coalesce(r.payload->>'failed','false')<>'true' "
                        "AND (r.send_command_id IS NULL OR s.state IN "
                        "('accepted','delivered','read')) "
                        "AND (r.occurred_at>i.occurred_at OR (r.occurred_at=i.occurred_at "
                        "AND i.payload->>'branch_id' IS NOT NULL "
                        "AND i.payload->>'branch_id'=r.payload->>'branch_id' "
                        "AND CASE WHEN r.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
                        "AND i.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
                        "THEN (r.payload->>'crm_order')::numeric"
                        ">(i.payload->>'crm_order')::numeric ELSE false END)))) AS unread_count,"
                        "coalesce((SELECT m.text FROM messages m WHERE c.needs_reply AND "
                        "m.workspace_id=c.workspace_id AND m.conversation_id=c.id "
                        "AND m.direction='in' AND m.actor_type='contact' AND "
                        "coalesce(m.payload->>'deleted','false')<>'true' AND NOT EXISTS("
                        "SELECT 1 FROM messages r LEFT JOIN send_commands sc ON "
                        "sc.workspace_id=r.workspace_id AND sc.id=r.send_command_id "
                        "WHERE r.workspace_id=m.workspace_id "
                        "AND r.conversation_id=m.conversation_id "
                        "AND r.direction='out' AND r.actor_type='human' AND "
                        "coalesce(r.payload->>'deleted','false')<>'true' AND "
                        "coalesce(r.payload->>'failed','false')<>'true' AND "
                        "(r.send_command_id IS NULL OR "
                        "sc.state IN ('accepted','delivered','read')) "
                        "AND (r.occurred_at>m.occurred_at OR (r.occurred_at=m.occurred_at "
                        "AND m.payload->>'branch_id' IS NOT NULL AND "
                        "m.payload->>'branch_id'=r.payload->>'branch_id' AND "
                        "CASE WHEN m.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
                        "AND r.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
                        "THEN (r.payload->>'crm_order')::numeric "
                        "> (m.payload->>'crm_order')::numeric ELSE false END))) "
                        "ORDER BY m.occurred_at DESC, CASE WHEN m.payload->>'crm_order' ~ "
                        "'^[0-9]{1,19}$' "
                        "THEN (m.payload->>'crm_order')::numeric END DESC NULLS LAST, "
                        "m.id DESC LIMIT 1),(SELECT m.text FROM messages m WHERE "
                        "m.workspace_id=c.workspace_id "
                        "AND m.conversation_id=c.id ORDER BY m.occurred_at DESC,m.id DESC "
                        "LIMIT 1)) AS preview "
                        + joins
                        + " WHERE "
                        + where
                        + " ORDER BY "
                        + EFFECTIVE_UNREAD
                        + " DESC,c.last_message_at DESC NULLS LAST,"
                        "c.id LIMIT 30 OFFSET :offset"
                    ),
                    params,
                ).mappings()
            )
            families = {}
            contacts = list({row["contact_id"] for row in rows if row["contact_id"]})
            if snapshot and contacts:
                params["ids"] = contacts
                families = {
                    row["contact_id"]: row
                    for row in db.execute(
                        text(
                            "SELECT a.contact_id,(array_agg(DISTINCT a.name ORDER BY "
                            "a.name))[1:4] AS names,"
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
                family = families.get(row["contact_id"], {})
                result.append(
                    self._meta(row)
                    | {
                        "name": row["display_name"],
                        "preview": (row["preview"] or "")[:180],
                        "students": family.get("names", []),
                        "unread": row["effective_unread"],
                        "unread_count": row["unread_count"],
                        "handled": row["handled_inbound_id"] == row["latest_inbound_id"]
                        and bool(row["needs_reply"]),
                        "status": {3: "client", 2: "lead", 1: "archive"}.get(
                            family.get("weight"), "unlinked"
                        ),
                        "last_message_at": row["last_message_at"],
                    }
                )
            return {
                "conversations": result,
                "total": counts["total"],
                "unread": counts["unread"],
                "page": page,
                "has_more": (page + 1) * 30 < counts["total"],
            }

    def history(
        self, actor, workspace, conversation, *, before_time=None, before_id=None, after_seq=None
    ):
        with self.max.chat_transaction(actor, workspace) as db:
            row = self._conversation(db, workspace, conversation)
            params = {"w": workspace, "c": conversation}
            end = db.execute(
                text(
                    "SELECT coalesce(max(seq),0) FROM message_changes WHERE workspace_id=:w "
                    "AND conversation_id=:c"
                ),
                params,
            ).scalar_one()
            where = "m.workspace_id=:w AND m.conversation_id=:c"
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
                next_seq = changes[-1]["seq"] if more_changes else end
            else:
                if (before_time is None) != (before_id is None):
                    raise AuthError(422, "Для загрузки истории нужен полный указатель страницы.")
                if before_time is not None:
                    params.update(before_time=before_time, before_id=before_id)
                    where += " AND (m.occurred_at,m.id)<(:before_time,:before_id)"
                records = self._messages(
                    db, where, params, "ORDER BY m.occurred_at DESC,m.id DESC LIMIT 21"
                )
                has_more = len(records) > 20
                records = list(reversed(records[:20]))
                next_seq = end
            return {
                "conversation": self._meta(row),
                "messages": [self._message(r) for r in records],
                "next_seq": next_seq,
                "has_more": has_more,
                "more_changes": more_changes,
            }

    def send(self, actor, workspace, conversation, model):
        self.max.accounts.throttle(f"chat-send:{workspace}:{actor.user_id}", limit=60, minutes=1)
        payload = {"text": model.text, "conversation_id": str(conversation)}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with self.max.chat_transaction(actor, workspace, lock=True) as db:
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
                if old["payload_hash"] != fingerprint(encoded):
                    raise AuthError(409, "Этот запрос уже использован для другого сообщения.")
                return {
                    "command_id": str(old["id"]),
                    "state": old["state"],
                    "request_id": str(model.request_id),
                }
            row = self._conversation(db, workspace, conversation, lock="UPDATE")
            if row["inbound_seq"] != model.seen_inbound_seq:
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
                    "requested_by) "
                    "VALUES (:w,:id,:c,:request,CAST(:p AS jsonb),:hash,:u)"
                ),
                {
                    "w": workspace,
                    "id": command,
                    "c": conversation,
                    "request": str(model.request_id),
                    "p": encoded,
                    "hash": fingerprint(encoded),
                    "u": actor.user_id,
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
                    "p": json.dumps({"source": "center_messenger"}),
                    "cmd": command,
                },
            )
            changed(db, workspace, conversation, message)
            db.execute(
                text(
                    "UPDATE conversations SET revision=revision+1,last_message_at=now() "
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
            self._conversation(db, workspace, conversation, lock="UPDATE")
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
                    "UPDATE conversations SET contact_id=:id,revision=revision+1 WHERE "
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

    def handle(self, actor, workspace, conversation):
        with self.max.chat_transaction(actor, workspace, lock=True) as db:
            row = self._conversation(db, workspace, conversation, lock="UPDATE")
            inbound_id = row["latest_inbound_id"]
            if not row["needs_reply"] or inbound_id is None:
                raise AuthError(409, "На последнее сообщение уже ответили.")
            if row["handled_inbound_id"] != inbound_id:
                db.execute(
                    text(
                        "UPDATE conversations SET handled_inbound_id=:message,handled_at=now(),"
                        "handled_by=:user,revision=revision+1 WHERE workspace_id=:w AND id=:c"
                    ),
                    {
                        "message": inbound_id,
                        "user": actor.user_id,
                        "w": workspace,
                        "c": conversation,
                    },
                )
                changed(db, workspace, conversation, inbound_id)
                audit(
                    db,
                    "chat_marked_handled",
                    user_id=actor.user_id,
                    workspace_id=workspace,
                    subject_id=conversation,
                )


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
    )


@router.get("/chat-updates")
def updates(
    workspace_id: UUID, request: Request, after_seq: int | None = Query(default=None, ge=0)
):
    _, reader = components(request)
    return reader.updates(identity(request), workspace_id, after_seq)


@router.get("/chats/{conversation_id}/messages")
def history(
    workspace_id: UUID,
    conversation_id: UUID,
    request: Request,
    before_time: datetime | None = None,
    before_id: UUID | None = None,
    after_seq: int | None = Query(default=None, ge=0),
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
    )


@router.post("/chats/{conversation_id}/messages", status_code=202)
def send(workspace_id: UUID, conversation_id: UUID, model: SendInput, request: Request):
    _, reader = components(request)
    return reader.send(identity(request, mutation=True), workspace_id, conversation_id, model)


@router.post("/chats/{conversation_id}/handled", status_code=204)
def mark_handled(workspace_id: UUID, conversation_id: UUID, request: Request):
    _, reader = components(request)
    reader.handle(identity(request, mutation=True), workspace_id, conversation_id)


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
