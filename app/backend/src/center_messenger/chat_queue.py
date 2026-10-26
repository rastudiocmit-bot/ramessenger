"""One policy for pending client messages, reply coverage and dialog priority.

The cached columns on conversations are only an index for the list. Counts,
previews and history metadata use this same query; automation never supplies a
priority date. Existing source rows and history cursors are not rewritten.
"""

import json
import logging
import threading

from sqlalchemy import text
from uuid import uuid4

from center_messenger.message_order import message_order, visible_message

PROJECTION_VERSION = 2


def queue_state_sql(c="c", *, enabled="true"):
    # Receive and sent journals do NOT prove a common numeric sequence. Equal
    # timestamps are ordered causally only with an explicit shared order scope.
    return f"""WITH events AS MATERIALIZED (
      SELECT m.*,s.state AS send_state,s.reply_through_seq,s.payload AS command_payload,
        (m.direction='out' AND m.actor_type='human'
          AND (m.send_command_id IS NOT NULL OR m.payload->>'possible_send_command_id' IS NULL)
          AND coalesce(m.payload->>'failed','false')<>'true'
          AND coalesce(m.payload->>'delivery_state','accepted') NOT IN
            ('queued','sending','unknown','failed','cancelled')
          AND (m.send_command_id IS NULL OR s.state IN ('accepted','delivered','read'))
        ) AS confirmed_reply
      FROM messages m LEFT JOIN send_commands s ON s.workspace_id=m.workspace_id
        AND s.id=m.send_command_id
      WHERE m.workspace_id={c}.workspace_id AND m.conversation_id={c}.id
        AND ({enabled}) AND {visible_message()}
    ), pending AS MATERIALIZED (
      SELECT i.* FROM events i WHERE i.direction='in' AND i.actor_type='contact'
        AND i.payload->>'handled_receipt' IS NULL
        AND NOT EXISTS (
          SELECT 1 FROM events h WHERE h.id={c}.handled_inbound_id
            AND {c}.answered_seq=0 AND (i.id=h.id OR (i.occurred_at<h.occurred_at
              AND i.received_at<=coalesce({c}.handled_at,h.received_at)))
        )
        AND NOT EXISTS (
          SELECT 1 FROM events r WHERE r.confirmed_reply AND (
            CASE WHEN r.send_command_id IS NOT NULL THEN
              EXISTS (SELECT 1 FROM message_changes seen
                JOIN messages observed ON observed.workspace_id=seen.workspace_id AND observed.id=seen.message_id
                WHERE seen.workspace_id=i.workspace_id AND (seen.message_id=i.id
                  OR observed.payload->>'superseded_by'=i.id::text)
                  AND seen.conversation_id::text=r.command_payload->>'conversation_id'
                  AND seen.seq <= CASE
                    WHEN r.command_payload->>'reply_checkpoint_version'='1'
                      THEN r.reply_through_seq
                    ELSE (SELECT min(queued.seq)-1 FROM message_changes queued
                      WHERE queued.workspace_id=r.workspace_id AND queued.message_id=r.id)
                  END)
            ELSE r.occurred_at>i.occurred_at OR (
              r.occurred_at=i.occurred_at
              AND nullif(r.payload->>'order_scope','') IS NOT NULL
              AND r.payload->>'order_scope'=i.payload->>'order_scope'
              AND CASE WHEN r.payload->>'crm_order' ~ '^[0-9]{{1,19}}$'
                AND i.payload->>'crm_order' ~ '^[0-9]{{1,19}}$'
                THEN (r.payload->>'crm_order')::numeric>(i.payload->>'crm_order')::numeric
                ELSE false END
            ) END)
        )
    ), latest_pending AS (
      SELECT * FROM pending m ORDER BY {message_order(descending=True)} LIMIT 1
    ), latest_human AS (
      SELECT * FROM events m WHERE (m.direction='in' AND m.actor_type='contact')
        OR m.confirmed_reply ORDER BY {message_order(descending=True)} LIMIT 1
    ), preview AS (
      SELECT * FROM latest_pending UNION ALL
      SELECT * FROM latest_human WHERE NOT EXISTS (SELECT 1 FROM latest_pending)
    ) SELECT
      (SELECT count(*) FROM pending) AS unanswered_count,
      EXISTS(SELECT 1 FROM pending) AS needs_reply,
      (SELECT id FROM latest_pending) AS last_unanswered_message_id,
      (SELECT occurred_at FROM latest_pending) AS last_unanswered_at,
      (SELECT max(occurred_at) FROM events WHERE direction='in' AND actor_type='contact') AS last_inbound_at,
      (SELECT id FROM events m WHERE direction='in' AND actor_type='contact'
        ORDER BY {message_order(descending=True)} LIMIT 1) AS latest_inbound_id,
      (SELECT occurred_at FROM latest_human) AS last_human_activity_at,
      (SELECT max(occurred_at) FROM events) AS last_event_at,
      (SELECT occurred_at FROM preview) AS priority_at,
      (SELECT id FROM preview) AS preview_message_id,
      (SELECT text FROM preview) AS preview_text,
      (SELECT payload->'attachments' FROM preview) AS preview_attachments,
      (SELECT direction FROM preview) AS last_direction,
      (SELECT coalesce(send_state,CASE WHEN direction='out' THEN 'accepted' ELSE 'received' END)
        FROM preview) AS last_state"""


def state(db, workspace, conversation):
    return db.execute(text(
        "SELECT q.* FROM conversations c CROSS JOIN LATERAL (" + queue_state_sql() +
        ") q WHERE c.workspace_id=:w AND c.id=:c"
    ), {"w": workspace, "c": conversation}).mappings().one()


def refresh(db, workspace, conversation):
    result = state(db, workspace, conversation)
    db.execute(text(
        "UPDATE conversations SET last_inbound_at=:inbound,last_message_at=:priority,"
        "needs_reply=:pending,revision=revision+1 WHERE workspace_id=:w AND id=:c AND "
        "(last_inbound_at IS DISTINCT FROM :inbound OR last_message_at IS DISTINCT FROM :priority "
        "OR needs_reply IS DISTINCT FROM :pending)"
    ), {"w": workspace, "c": conversation, "inbound": result["last_inbound_at"],
        "priority": result["priority_at"], "pending": result["needs_reply"]})
    # Store only this policy's derived facts, in the same message transaction.
    # The list must not scan a whole timeline again for every visible row/poll.
    payload = dict(result) | {"projection_version": PROJECTION_VERSION}
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES (:w,:id,'chat.list.state',:key,CAST(:p AS jsonb),'published') "
        "ON CONFLICT (workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload"),
        {"w": workspace, "id": uuid4(), "key": str(conversation), "p": json.dumps(payload, default=str)})
    return result


def cached_state_sql(c="c"):
    fields = "unanswered_count bigint,needs_reply boolean,last_unanswered_message_id uuid,last_unanswered_at timestamptz," \
        "last_inbound_at timestamptz,latest_inbound_id uuid,last_human_activity_at timestamptz,last_event_at timestamptz," \
        "priority_at timestamptz,preview_message_id uuid,preview_text text,preview_attachments jsonb,last_direction text,last_state text"
    exists = (f"SELECT 1 FROM outbox_events projection WHERE projection.workspace_id={c}.workspace_id "
              f"AND projection.topic='chat.list.state' AND projection.event_key={c}.id::text "
              f"AND projection.payload->>'projection_version'='{PROJECTION_VERSION}'")
    return (f"SELECT cached.* FROM outbox_events projection CROSS JOIN LATERAL jsonb_to_record(projection.payload) "
            f"AS cached({fields}) WHERE projection.workspace_id={c}.workspace_id AND projection.topic='chat.list.state' "
            f"AND projection.event_key={c}.id::text AND projection.payload->>'projection_version'='{PROJECTION_VERSION}' "
            "UNION ALL SELECT live.* FROM (" + queue_state_sql(c, enabled=f"NOT EXISTS ({exists})") + ") live "
            f"WHERE NOT EXISTS ({exists})")


def preview(result):
    if not result["preview_message_id"]:
        return "Переписка ещё не началась", "empty"
    if result["preview_text"] and result["preview_text"].strip():
        return result["preview_text"][:180], "text"
    attachments = result["preview_attachments"] or []
    if attachments:
        item = attachments[0]
        if isinstance(item, dict):
            return (item.get("name") or "Документ")[:180], "attachment"
        return {"image": "Фото", "photo": "Фото", "audio": "Голосовое сообщение",
                "voice": "Голосовое сообщение", "video": "Видео"}.get(item, "Документ"), "attachment"
    return "Сообщение клиента", "message"


def materialize_legacy_handled(db, workspace, conversation):
    # Convert a legacy dialog-wide marker to receipts before it can be moved to
    # another canonical family. Unknown same-minute order stays unanswered.
    db.execute(text("UPDATE messages i SET payload=i.payload||jsonb_build_object('handled_receipt',"
        "jsonb_build_object('version',1,'legacy',true)),version=i.version+1 "
        "FROM conversations c JOIN messages h ON h.workspace_id=c.workspace_id "
        "AND h.id=c.handled_inbound_id WHERE c.workspace_id=:w AND c.id=:c "
        "AND c.answered_seq=0 AND i.workspace_id=c.workspace_id AND i.conversation_id=c.id "
        "AND i.direction='in' AND i.actor_type='contact' AND i.payload->>'handled_receipt' IS NULL "
        "AND (i.id=h.id OR (i.occurred_at<h.occurred_at AND i.received_at<=coalesce(c.handled_at,h.received_at)))"),
        {"w": workspace, "c": conversation})
    db.execute(text("UPDATE conversations SET answered_seq=greatest(inbound_seq,1) "
                    "WHERE workspace_id=:w AND id=:c AND handled_inbound_id IS NOT NULL AND answered_seq=0"),
               {"w": workspace, "c": conversation})


def repair_batch(db, workspace, connection, *, limit=40):
    """Resume the derived-column repair using existing connection metadata.

    No schema change: the guarded deployment and its image rollback remain
    compatible. Each transaction yields the workspace lock between batches.
    """
    channel = db.execute(text("SELECT capabilities FROM connections WHERE workspace_id=:w "
                              "AND id=:c FOR UPDATE"), {"w": workspace, "c": connection}).scalar_one()
    progress = channel.get("chat_queue_projection", {})
    if progress.get("version") == PROJECTION_VERSION and progress.get("complete"):
        return False
    after = progress.get("after") if progress.get("version") == PROJECTION_VERSION else None
    identifiers = list(db.execute(text(
        "SELECT id FROM conversations WHERE workspace_id=:w AND connection_id=:c "
        "AND (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid)) ORDER BY id LIMIT :limit FOR UPDATE"
    ), {"w": workspace, "c": connection, "after": after, "limit": limit}).scalars())
    for conversation in identifiers:
        materialize_legacy_handled(db, workspace, conversation)
        refresh(db, workspace, conversation)
    progress = {"version": PROJECTION_VERSION, "complete": len(identifiers) < limit,
                "after": str(identifiers[-1]) if identifiers else after,
                "processed": (progress.get("processed", 0) if after else 0) + len(identifiers)}
    db.execute(text("UPDATE connections SET capabilities=jsonb_set(capabilities,"
                    "'{chat_queue_projection}',CAST(:p AS jsonb)) WHERE workspace_id=:w AND id=:c"),
               {"w": workspace, "c": connection, "p": json.dumps(progress)})
    return not progress["complete"]


def invalidate_projection(db, workspace):
    return db.execute(text("UPDATE connections SET capabilities=capabilities-'chat_queue_projection' "
        "WHERE workspace_id=:w AND kind IN ('max_bot','alfa_max_browser')"), {"w": workspace}).rowcount


class QueueProjectionWorker:
    def __init__(self, accounts):
        self.accounts, self.stopping = accounts, threading.Event()
        self.thread = threading.Thread(target=self.run, name="chat-queue-projection", daemon=True)

    def run_next(self):
        """One bounded repair batch; workspace discovery uses routing tables only."""
        pending = getattr(self, "_pending_scopes", [])
        if not pending:
            with self.accounts.transaction() as db:
                pending = list(db.execute(text("SELECT workspace_id FROM max_routes UNION "
                    "SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars())
            self._pending_scopes = pending
        if not pending:
            return False
        workspace = pending.pop(0)
        with self.accounts.transaction(workspace_id=workspace) as db:
            connection = db.execute(text("SELECT id FROM connections WHERE workspace_id=:w AND "
                "kind IN ('max_bot','alfa_max_browser') AND (capabilities->'chat_queue_projection' "
                "IS NULL OR capabilities->'chat_queue_projection'->>'version'<>:version OR "
                "capabilities->'chat_queue_projection'->>'complete' IS DISTINCT FROM 'true') "
                "ORDER BY id LIMIT 1"), {"version": str(PROJECTION_VERSION), "w": workspace}).scalar_one_or_none()
        if connection:
            with self.accounts.transaction(workspace_id=workspace) as db:
                db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                repair_batch(db, workspace, connection)
        return bool(connection or pending)

    def run(self):
        while not self.stopping.is_set():
            worked = False
            try:
                # Only these existing routing/queue tables are intentionally
                # readable outside a tenant scope. connections itself has RLS.
                with self.accounts.transaction() as db:
                    workspaces = list(db.execute(text("SELECT workspace_id FROM max_routes UNION "
                        "SELECT workspace_id FROM alfa_max_sync_queue")).scalars())
                for workspace in workspaces:
                    with self.accounts.transaction(workspace_id=workspace) as db:
                        channels = list(db.execute(text("SELECT id FROM connections WHERE workspace_id=:w AND "
                        "kind IN ('max_bot','alfa_max_browser') AND (capabilities->'chat_queue_projection' "
                        "IS NULL OR capabilities->'chat_queue_projection'->>'version'<>:version OR "
                        "capabilities->'chat_queue_projection'->>'complete' IS DISTINCT FROM 'true') "
                        "ORDER BY id"), {"version": str(PROJECTION_VERSION), "w": workspace}).scalars())
                    for connection in channels:
                        if self.stopping.is_set():
                            break
                        with self.accounts.transaction(workspace_id=workspace) as db:
                            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                            repair_batch(db, workspace, connection)
                        worked = True
            except Exception:
                logging.getLogger(__name__).warning("Chat queue projection will resume")
            self.stopping.wait(0.1 if worked else 10)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=15)


def main(argv=None):
    """Explicit maintenance after an image rollback or a requested recheck."""
    import argparse
    import sys
    from center_messenger.accounts import Accounts
    from center_messenger.config import Settings
    from center_messenger.database import make_engine

    parser = argparse.ArgumentParser(description="Rebuild only derived dialog priorities; preserve message history")
    parser.add_argument("--rebuild", action="store_true", required=True)
    parser.parse_args(argv)
    engine = None
    try:
        settings = Settings()
        engine = make_engine(settings)
        accounts = Accounts(engine, settings)
        with accounts.transaction() as db:
            workspaces = list(db.execute(text("SELECT workspace_id FROM max_routes UNION "
                "SELECT workspace_id FROM alfa_max_sync_queue")).scalars())
        count = 0
        for workspace in workspaces:
            with accounts.transaction(workspace_id=workspace) as db:
                db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                count += invalidate_projection(db, workspace)
        print(json.dumps({"scheduled_connections": count, "history_preserved": True}))
        return 0
    except Exception as error:
        print(f"Queue rebuild unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
