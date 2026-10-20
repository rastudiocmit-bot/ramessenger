"""Rebuild unread state from CRM-ordered human conversation history."""

from alembic import op

revision = "0009_message_unread"
down_revision = "0008_max_bot_contact_index"
branch_labels = None
depends_on = None

SYSTEM_MARKERS = (
    "service account|system|систем|сервис|служеб|автомат|auto|рассыл|"
    "триггер|робот|бот|уведом|интеграц|integration|notification|scheduler|"
    "schedule|workflow|campaign|маркетинг|массов"
)


def upgrade():
    # The old sequence-based columns remain inert for a safe rollback window.
    # Runtime code no longer reads or writes them.
    # Prior parsers could treat any name-shaped sender as a human. Preserve
    # CRM authors with an explicit user ID or employee phone; downgrade
    # ambiguous senders, with system markers taking priority over both.
    op.execute(
        "WITH corrected AS ("
        "UPDATE messages SET actor_type=CASE WHEN "
        f"lower(coalesce(payload->>'actor_name','')) ~ '{SYSTEM_MARKERS}' "
        "THEN 'automation' ELSE 'unknown' END, "
        "payload=jsonb_set(payload,'{actor_type}',to_jsonb(CASE WHEN "
        f"lower(coalesce(payload->>'actor_name','')) ~ '{SYSTEM_MARKERS}' "
        "THEN 'automation' ELSE 'unknown' END),true) "
        "WHERE direction='out' AND payload->>'source'='alfa_max' "
        "AND (lower(coalesce(payload->>'actor_name','')) ~ "
        f"'{SYSTEM_MARKERS}' OR (actor_type='human' "
        "AND nullif(payload->>'actor_id','') IS NULL "
        "AND length(regexp_replace(coalesce(payload->>'actor_name',''), "
        "'[^0-9]', '', 'g')) NOT BETWEEN 10 AND 15)) "
        "RETURNING workspace_id,conversation_id,id) "
        "INSERT INTO message_changes(workspace_id,conversation_id,message_id) "
        "SELECT workspace_id,conversation_id,id FROM corrected"
    )
    op.execute(
        "WITH states AS (SELECT c.workspace_id,c.id,"
        "(SELECT max(m.occurred_at) FROM messages m WHERE m.workspace_id=c.workspace_id "
        "AND m.conversation_id=c.id AND m.direction='in' AND m.actor_type='contact' "
        "AND coalesce(m.payload->>'deleted','false')<>'true') AS last_inbound,"
        "(SELECT max(m.occurred_at) FROM messages m WHERE m.workspace_id=c.workspace_id "
        "AND m.conversation_id=c.id "
        "AND coalesce(m.payload->>'deleted','false')<>'true') "
        "AS last_message FROM conversations c) "
        "UPDATE conversations c SET last_inbound_at=s.last_inbound,"
        "last_message_at=s.last_message,needs_reply=(s.last_inbound IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM messages i WHERE i.workspace_id=s.workspace_id "
        "AND i.conversation_id=s.id AND i.direction='in' AND i.actor_type='contact' "
        "AND i.occurred_at=s.last_inbound "
        "AND coalesce(i.payload->>'deleted','false')<>'true' "
        "AND NOT EXISTS (SELECT 1 FROM messages r LEFT JOIN send_commands sc "
        "ON sc.workspace_id=r.workspace_id AND sc.id=r.send_command_id "
        "WHERE r.workspace_id=i.workspace_id AND r.conversation_id=i.conversation_id "
        "AND r.direction='out' AND r.actor_type='human' "
        "AND coalesce(r.payload->>'deleted','false')<>'true' "
        "AND coalesce(r.payload->>'failed','false')<>'true' "
        "AND (r.send_command_id IS NULL OR sc.state IN ('accepted','delivered','read')) "
        "AND (r.occurred_at>i.occurred_at OR (r.occurred_at=i.occurred_at "
        "AND i.payload->>'branch_id' IS NOT NULL "
        "AND i.payload->>'branch_id'=r.payload->>'branch_id' "
        "AND i.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
        "AND r.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
        "AND CASE WHEN r.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
        "AND i.payload->>'crm_order' ~ '^[0-9]{1,19}$' "
        "THEN (r.payload->>'crm_order')::numeric>(i.payload->>'crm_order')::numeric "
        "ELSE false END)))"
        ")),revision=c.revision+1 FROM states s "
        "WHERE c.workspace_id=s.workspace_id AND c.id=s.id"
    )


def downgrade():
    # This is a data correction. Restoring the previous heuristic would make
    # already-imported system messages appear as human replies again.
    pass
