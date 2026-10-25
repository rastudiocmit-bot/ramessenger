"""Explicit two-step handling of all unread direct chats in one workspace."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.auth_security import AuthError
from center_messenger.chat_queue import cached_state_sql
from center_messenger.message_order import visible_message

TOPIC = "chat.bulk.handled"
LIFETIME = timedelta(minutes=10)


def prepare(reader, actor, workspace):
    reader.max.accounts.throttle(f"chat-bulk:{workspace}:{actor.user_id}", limit=6, minutes=1)
    with reader.max.chat_transaction(actor, workspace) as db:
        reader._require_write(db, actor, workspace)
        db.execute(text("SET LOCAL jit=off"))
        # One SQL snapshot covers every channel and page. UI filters intentionally
        # do not narrow an action whose confirmation explicitly says all chats.
        rows = list(db.execute(text(
            "SELECT c.id,c.contact_id,c.connection_id,c.revision,q.latest_inbound_id,q.unanswered_count,"
            "(SELECT coalesce(max(ch.seq),0) FROM message_changes ch WHERE ch.workspace_id=c.workspace_id "
            "AND ch.conversation_id=c.id) AS change_seq,"
            "ARRAY(SELECT m.id FROM messages m WHERE m.workspace_id=c.workspace_id AND m.conversation_id=c.id "
            "AND m.direction='in' AND m.actor_type='contact' AND " + visible_message() +
            " AND m.payload->>'handled_receipt' IS NULL AND EXISTS(SELECT 1 FROM message_changes known "
            "WHERE known.workspace_id=m.workspace_id AND known.conversation_id=c.id AND known.message_id=m.id)) AS known_ids "
            "FROM conversations c "
            "JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
            "LEFT JOIN max_settings s ON s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id "
            "LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
            "CROSS JOIN LATERAL (" + cached_state_sql() + ") q WHERE c.workspace_id=:w AND c.kind='direct' "
            "AND q.needs_reply AND q.latest_inbound_id IS NOT NULL AND ((cn.kind='max_bot' AND s.connection_id IS NOT NULL) "
            "OR (cn.kind='alfa_max_browser' AND am.connection_id IS NOT NULL)) ORDER BY c.id"
        ), {"w": workspace}).mappings())
        now = datetime.now(UTC)
        targets = [{"connection": str(r["connection_id"]), "contact": str(r["contact_id"]) if r["contact_id"] else None,
            "messages": [str(identifier) for identifier in r["known_ids"]],
            "checkpoint": {"conversation_id": str(r["id"]), "revision": r["revision"], "change_seq": r["change_seq"],
                           "latest_inbound_id": str(r["latest_inbound_id"])}} for r in rows]
        counts = {"conversation_count": len(rows), "client_count": len({
            ("contact", r["contact_id"]) if r["contact_id"] else ("chat", r["id"]) for r in rows}),
            "message_count": sum(r["unanswered_count"] or 0 for r in rows)}
        identifier = uuid4()
        payload = {"version": 1, "user": str(actor.user_id), "state": "prepared", "targets": targets,
                   "expires_at": (now + LIFETIME).isoformat(), **counts}
        # Prepared operations are not worker jobs. Expiration changes no chats.
        db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w AND topic=:topic AND "
            "((payload->>'state'='prepared' AND (payload->>'expires_at')::timestamptz<now()) OR "
            "(payload->>'completed_at')::timestamptz<now()-interval '1 day')"), {"w": workspace, "topic": TOPIC})
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,:topic,CAST(:id AS text),CAST(:p AS jsonb),'published')"),
            {"w": workspace, "id": identifier, "topic": TOPIC, "p": json.dumps(payload)})
        return {"operation_id": str(identifier), "expires_at": payload["expires_at"], **counts}


def confirm(reader, actor, workspace, model):
    from center_messenger.chat_api import SeenCheckpoint
    if model.confirmed is not True:
        raise AuthError(400, "Подтвердите обработку всех диалогов.")
    with reader.max.chat_transaction(actor, workspace, lock=True) as db:
        reader._require_write(db, actor, workspace)
        payload = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id AND topic=:topic "
            "AND payload->>'user'=:user FOR UPDATE"),
            {"w": workspace, "id": model.operation_id, "topic": TOPIC, "user": str(actor.user_id)}).scalar_one_or_none()
        if not payload:
            raise AuthError(404, "Подтверждение недоступно. Откройте окно обработки заново.")
        if payload.get("state") == "completed":
            return payload["result"]
        if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
            raise AuthError(409, "Срок подтверждения истёк. Обновите список в окне обработки.")
        processed, unchanged, skipped, messages = 0, 0, 0, 0
        for target in payload["targets"]:
            checkpoint = SeenCheckpoint(**target["checkpoint"])
            try:
                row = reader._conversation(db, workspace, checkpoint.conversation_id, lock="UPDATE")
                contact = str(row["contact_id"]) if row["contact_id"] else None
                if row["id"] != checkpoint.conversation_id or str(row["connection_id"]) != target["connection"] or contact != target["contact"]:
                    skipped += 1
                    continue
                reader._validate_checkpoint(db, workspace, row, checkpoint)
            except AuthError as error:
                if error.status not in (404, 409):
                    raise
                skipped += 1
                continue
            if not row["effective_unread"]:
                unchanged += 1
                continue
            count = reader._apply_handled(db, workspace, row, checkpoint, actor.user_id, known_ids=target["messages"])
            messages += count
            processed += int(count > 0)
            unchanged += int(count == 0)
        result = {"processed": processed, "already_handled": unchanged, "skipped": skipped, "messages": messages}
        payload.update(state="completed", result=result, completed_at=datetime.now(UTC).isoformat())
        payload.pop("targets")
        db.execute(text("UPDATE outbox_events SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id AND topic=:topic"),
            {"w": workspace, "id": model.operation_id, "topic": TOPIC, "p": json.dumps(payload)})
        audit(db, "chat_bulk_marked_handled", user_id=actor.user_id, workspace_id=workspace, subject_id=model.operation_id)
        return result
