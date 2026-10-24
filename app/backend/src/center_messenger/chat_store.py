"""Transactional projection of verified MAX updates; source IDs define identity."""

import hashlib
import json
from uuid import uuid4, uuid5

from sqlalchemy import text

from center_messenger.max_client import MaxError, integer_id, message_id, timestamp, user_name
from center_messenger.message_order import visible_message


def changed(db, workspace, conversation, message, *, new_inbound=False):
    return db.execute(
        text(
            "INSERT INTO message_changes(workspace_id,conversation_id,message_id,new_inbound) "
            "VALUES (:w,:c,:m,:new_inbound) RETURNING seq"
        ),
        {"w": workspace, "c": conversation, "m": message, "new_inbound": new_inbound},
    ).scalar_one()


def refresh_unread(db, workspace, conversation):
    from center_messenger.chat_queue import refresh
    return refresh(db, workspace, conversation)


def conversation(db, workspace, connection, user):
    peer = integer_id(user.get("user_id"))
    name = user_name(user) or f"Пользователь MAX {peer}"
    identifier = uuid5(connection, f"max:user:{peer}")
    db.execute(
        text(
            "INSERT INTO "
            "conversations(workspace_id,id,connection_id,external_chat_id,peer_user_id,peer_name) "
            "VALUES (:w,:id,:c,:external,:peer,:name) ON CONFLICT "
            "(workspace_id,connection_id,external_chat_id,external_topic_id) "
            "DO UPDATE SET peer_name=EXCLUDED.peer_name"
        ),
        {
            "w": workspace,
            "id": identifier,
            "c": connection,
            "external": f"user:{peer}",
            "peer": peer,
            "name": name,
        },
    )
    return (
        db.execute(
            text("SELECT * FROM conversations WHERE workspace_id=:w AND id=:c FOR UPDATE"),
            {"w": workspace, "c": identifier},
        )
        .mappings()
        .one()
    )


def find_message(db, workspace, connection, mid):
    return (
        db.execute(
            text(
                "SELECT m.* FROM source_records s JOIN messages m ON "
                "m.workspace_id=s.workspace_id AND "
                "m.id=s.message_id WHERE s.workspace_id=:w AND s.connection_id=:c "
                "AND s.namespace='max_bot' AND s.external_record_id=:mid"
            ),
            {"w": workspace, "c": connection, "mid": mid},
        )
        .mappings()
        .one_or_none()
    )


def source(db, workspace, connection, message, mid):
    canonical = db.execute(
        text(
            "INSERT INTO "
            "source_records(workspace_id,id,connection_id,message_id,namespace,external_record_id) "
            "VALUES (:w,:id,:c,:m,'max_bot',:mid) ON CONFLICT "
            "(workspace_id,connection_id,namespace,external_record_id) "
            "DO UPDATE SET message_id=source_records.message_id RETURNING message_id"
        ),
        {"w": workspace, "id": uuid4(), "c": connection, "m": message, "mid": mid},
    ).scalar_one()
    if canonical != message:
        raise MaxError("invalid_response")


def normalized_body(body, event_ms, *, edited=False):
    if body is not None and not isinstance(body, dict):
        raise MaxError("invalid_response")
    body = body or {}
    content = body.get("text")
    if content is not None and (not isinstance(content, str) or len(content) > 20000):
        raise MaxError("invalid_response")
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list) or len(attachments) > 100:
        raise MaxError("invalid_response")
    # Render only text and attachment kinds. Never embed source HTML or remote media.
    kinds = []
    for attachment in attachments:
        if not isinstance(attachment, dict) or not isinstance(attachment.get("type"), str):
            raise MaxError("invalid_response")
        kinds.append(attachment["type"][:40])
    payload = {"source_ms": event_ms, "attachments": kinds, "edited": edited}
    return content, payload


def project_event(db, workspace, connection, bot_id, event):
    kind = event.get("update_type")
    event_ms = integer_id(event.get("timestamp"))
    event_at = timestamp(event_ms)
    if kind == "bot_started":
        user = event.get("user")
        if not isinstance(user, dict) or user.get("is_bot") is not False:
            raise MaxError("invalid_response")
        row = conversation(db, workspace, connection, user)
        refresh_unread(db, workspace, row["id"])
        return "processed"
    if kind == "message_removed":
        mid = message_id(event.get("message_id"))
        db.execute(
            text(
                "INSERT INTO max_tombstones(workspace_id,connection_id,message_id,source_ms) "
                "VALUES (:w,:c,:mid,:ms) ON CONFLICT(workspace_id,connection_id,message_id) "
                "DO UPDATE SET source_ms=greatest(max_tombstones.source_ms,EXCLUDED.source_ms)"
            ),
            {"w": workspace, "c": connection, "mid": mid, "ms": event_ms},
        )
        old = find_message(db, workspace, connection, mid)
        if old:
            db.execute(
                text("SELECT id FROM conversations WHERE workspace_id=:w AND id=:c FOR UPDATE"),
                {"w": workspace, "c": old["conversation_id"]},
            )
            payload = dict(old["payload"])
            if not payload.get("deleted"):
                payload.update(deleted=True, source_ms=max(event_ms, payload.get("source_ms", 0)))
                db.execute(
                    text(
                        "UPDATE messages SET text=NULL,payload=CAST(:p AS jsonb),version=version+1 "
                        "WHERE workspace_id=:w AND id=:m"
                    ),
                    {"w": workspace, "m": old["id"], "p": json.dumps(payload)},
                )
                changed(db, workspace, old["conversation_id"], old["id"])
                refresh_unread(db, workspace, old["conversation_id"])
        return "processed"
    if kind not in {"message_created", "message_edited"}:
        return "ignored"
    message = event.get("message")
    if not isinstance(message, dict):
        raise MaxError("invalid_response")
    recipient, user, body = message.get("recipient"), message.get("sender"), message.get("body")
    if not isinstance(recipient, dict):
        raise MaxError("invalid_response")
    if recipient.get("chat_type") != "dialog":
        return "ignored"
    if not isinstance(user, dict):
        raise MaxError("invalid_response")
    peer = integer_id(user.get("user_id"))
    if peer == bot_id or user.get("is_bot") is True:
        # Bot-origin events do not establish that an employee answered a request.
        return "ignored"
    if user.get("is_bot") is not False:
        raise MaxError("invalid_response")
    if recipient.get("user_id") is not None and integer_id(recipient["user_id"]) != bot_id:
        raise MaxError("invalid_response")
    forwarded = body is None and isinstance(message.get("link"), dict)
    if forwarded:
        # MAX allows body=null for forwarded-only messages. Preserve a placeholder
        # using the complete signed event identity, never the forwarded source MID.
        raw = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        body = {"mid": "event:" + hashlib.sha256(raw.encode()).hexdigest()}
    if not isinstance(body, dict):
        raise MaxError("invalid_response")
    mid = message_id(body.get("mid"))
    occurred_at = timestamp(message.get("timestamp"))
    content, payload = normalized_body(body, event_ms, edited=kind == "message_edited")
    if forwarded:
        payload["forwarded"] = True
    row = conversation(db, workspace, connection, user)
    old = find_message(db, workspace, connection, mid)
    if old and old["conversation_id"] != row["id"]:
        raise MaxError("invalid_response")
    tombstone = db.execute(
        text(
            "SELECT source_ms FROM max_tombstones WHERE workspace_id=:w "
            "AND connection_id=:c AND message_id=:mid"
        ),
        {"w": workspace, "c": connection, "mid": mid},
    ).scalar_one_or_none()
    if tombstone is not None:
        content = None
        payload.update(deleted=True, source_ms=max(event_ms, tombstone))
    if old:
        if (
            kind == "message_created"
            or old["payload"].get("deleted")
            or event_ms <= old["payload"].get("source_ms", 0)
        ):
            return "processed"
        identifier = old["id"]
        if "handled_receipt" in old["payload"]:
            payload["handled_receipt"] = old["payload"]["handled_receipt"]
        db.execute(
            text(
                "UPDATE messages SET text=:t,payload=CAST(:p AS jsonb),version=version+1 "
                "WHERE workspace_id=:w AND id=:m"
            ),
            {"w": workspace, "m": identifier, "t": content, "p": json.dumps(payload)},
        )
    else:
        identifier = uuid4()
        db.execute(
            text(
                "INSERT INTO "
                "messages(workspace_id,id,conversation_id,provider_message_id,direction,actor_type,"
                "text,payload,occurred_at) VALUES (:w,:m,:c,:mid,'in','contact',:t,CAST(:p "
                "AS jsonb),:at)"
            ),
            {
                "w": workspace,
                "m": identifier,
                "c": row["id"],
                "mid": mid,
                "t": content,
                "p": json.dumps(payload),
                "at": occurred_at,
            },
        )
        source(db, workspace, connection, identifier, mid)
    sequence = changed(
        db,
        workspace,
        row["id"],
        identifier,
        new_inbound=old is None and kind == "message_created" and not payload.get("deleted"),
    )
    if not payload.get("deleted"):
        db.execute(
            text("UPDATE conversations SET inbound_seq=:seq WHERE workspace_id=:w AND id=:c"),
            {"w": workspace, "c": row["id"], "seq": sequence},
        )
    refresh_unread(db, workspace, row["id"])
    return "processed"
