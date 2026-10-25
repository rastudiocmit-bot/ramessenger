"""Transactional chat versions and stored history counts, without changing messages."""
from alembic import op

revision = "0017_chat_read_versions"
down_revision = "0016_crm_card_preference"
branch_labels = None
depends_on = None

TABLES = {
    "conversations": "list", "messages": "list", "message_changes": "list",
    "connections": "list", "max_settings": "list", "alfa_max_settings": "list",
    "contacts": "card", "students": "card", "contact_student_links": "card",
    "alfa_settings": "card", "alfa_branches": "card", "alfa_sync_runs": "card",
    "alfa_customers": "card", "alfa_groups": "card", "alfa_memberships": "card",
    "alfa_directory": "card", "alfa_family_details": "card", "outbox_events": "list",
}


def lock_tables():
    # Acquire the whole set before backfilling. Each failed attempt rolls back
    # its partial locks before a short retry, so an active writer cannot deadlock
    # with the migration. After five seconds the guarded deploy can retry later.
    tables = "workspaces," + ",".join(TABLES)
    op.execute("DO $$ DECLARE attempt integer; BEGIN FOR attempt IN 1..50 LOOP BEGIN "
               f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE NOWAIT; RETURN; "
               "EXCEPTION WHEN lock_not_available THEN IF attempt=50 THEN RAISE; END IF; "
               "PERFORM pg_sleep(0.1); END; END LOOP; END $$")


def upgrade():
    lock_tables()
    op.execute("ALTER TABLE conversations ADD COLUMN stored_message_count bigint NOT NULL DEFAULT 0")
    op.execute("UPDATE conversations c SET stored_message_count=s.n FROM "
               "(SELECT workspace_id,conversation_id,count(*) AS n FROM messages "
               "WHERE payload->>'superseded_by' IS NULL GROUP BY workspace_id,conversation_id) s "
               "WHERE c.workspace_id=s.workspace_id AND c.id=s.conversation_id")
    op.execute("""CREATE TABLE chat_read_versions (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
      revision bigint NOT NULL DEFAULT 0, card_revision bigint NOT NULL DEFAULT 0
    )""")
    op.execute("INSERT INTO chat_read_versions(workspace_id) SELECT id FROM workspaces")
    op.execute("ALTER TABLE chat_read_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_read_versions FORCE ROW LEVEL SECURITY")
    condition = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
    op.execute(f"CREATE POLICY tenant_isolation ON chat_read_versions TO cm_app "
               f"USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON chat_read_versions TO cm_app")
    op.execute("""CREATE FUNCTION cm_chat_bump_version() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE w uuid; domain text; topic text;
    BEGIN
      IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
      domain := TG_ARGV[0];
      IF TG_TABLE_NAME='alfa_settings' AND TG_OP='UPDATE' THEN
        IF NEW.version=OLD.version AND (NEW.secret_ciphertext IS NULL)=(OLD.secret_ciphertext IS NULL) THEN RETURN NULL; END IF;
      END IF;
      IF TG_TABLE_NAME='max_settings' AND TG_OP='UPDATE' THEN
        IF NEW.enabled=OLD.enabled AND NEW.subscription_confirmed=OLD.subscription_confirmed
          AND (NEW.secret_ciphertext IS NULL)=(OLD.secret_ciphertext IS NULL) THEN RETURN NULL; END IF;
      END IF;
      IF TG_TABLE_NAME='alfa_max_settings' AND TG_OP='UPDATE' THEN
        IF NEW.state=OLD.state AND NEW.crm_connection_id IS NOT DISTINCT FROM OLD.crm_connection_id
          AND (NEW.session_ciphertext IS NULL)=(OLD.session_ciphertext IS NULL)
          AND NEW.history_cursor IS NOT DISTINCT FROM OLD.history_cursor
          AND NEW.last_error_code IS NOT DISTINCT FROM OLD.last_error_code THEN RETURN NULL; END IF;
      END IF;
      IF TG_TABLE_NAME='workspaces' THEN
        IF NEW.name IS NOT DISTINCT FROM OLD.name AND NEW.timezone IS NOT DISTINCT FROM OLD.timezone THEN RETURN NULL; END IF;
        w := NEW.id;
      ELSE
        IF TG_OP='DELETE' THEN w := OLD.workspace_id; ELSE w := NEW.workspace_id; END IF;
      END IF;
      IF TG_TABLE_NAME='conversations' THEN
        IF TG_OP<>'UPDATE' OR NEW.contact_id IS DISTINCT FROM OLD.contact_id
          OR NEW.needs_reply IS DISTINCT FROM OLD.needs_reply
          OR NEW.crm_link_manual IS DISTINCT FROM OLD.crm_link_manual THEN domain := 'card'; END IF;
      END IF;
      IF TG_TABLE_NAME='outbox_events' THEN
        IF TG_OP='DELETE' THEN topic := OLD.topic; ELSE topic := NEW.topic; END IF;
        IF topic NOT IN ('chat.list.state','crm.card.preference','crm.active.review',
          'crm.card.refresh','crm.card.lessons','crm.card.lesson-history','crm.customer.groups') THEN RETURN NULL; END IF;
        IF TG_OP='UPDATE' AND NEW.state=OLD.state AND
          (NEW.payload-ARRAY['lease_owner','lease_until','available_at','attempts']) IS NOT DISTINCT FROM
          (OLD.payload-ARRAY['lease_owner','lease_until','available_at','attempts']) THEN RETURN NULL; END IF;
        IF topic<>'chat.list.state' THEN domain := 'card'; END IF;
      END IF;
      INSERT INTO chat_read_versions(workspace_id,revision,card_revision)
        SELECT w,1,CASE WHEN domain='card' THEN 1 ELSE 0 END
        WHERE EXISTS(SELECT 1 FROM workspaces WHERE id=w)
        ON CONFLICT(workspace_id) DO UPDATE SET revision=chat_read_versions.revision+1,
          card_revision=chat_read_versions.card_revision+CASE WHEN domain='card' THEN 1 ELSE 0 END;
      RETURN NULL;
    END $$""")
    for table, domain in TABLES.items():
        op.execute(f"CREATE CONSTRAINT TRIGGER chat_read_version AFTER INSERT OR UPDATE OR DELETE ON {table} "
                   f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cm_chat_bump_version('{domain}')")
    op.execute("CREATE CONSTRAINT TRIGGER chat_read_version AFTER UPDATE ON workspaces "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cm_chat_bump_version('card')")
    op.execute("""CREATE FUNCTION cm_chat_count_messages() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='UPDATE' AND OLD.workspace_id=NEW.workspace_id AND OLD.conversation_id=NEW.conversation_id
        AND (OLD.payload->>'superseded_by' IS NULL)=(NEW.payload->>'superseded_by' IS NULL) THEN RETURN NULL; END IF;
      IF TG_OP<>'INSERT' AND OLD.payload->>'superseded_by' IS NULL THEN
        UPDATE conversations SET stored_message_count=stored_message_count-1
          WHERE workspace_id=OLD.workspace_id AND id=OLD.conversation_id;
      END IF;
      IF TG_OP<>'DELETE' AND NEW.payload->>'superseded_by' IS NULL THEN
        UPDATE conversations SET stored_message_count=stored_message_count+1
          WHERE workspace_id=NEW.workspace_id AND id=NEW.conversation_id;
      END IF;
      RETURN NULL;
    END $$""")
    op.execute("CREATE TRIGGER chat_message_count AFTER INSERT OR UPDATE OR DELETE ON messages "
               "FOR EACH ROW EXECUTE FUNCTION cm_chat_count_messages()")
    op.execute("""CREATE INDEX messages_chat_order_idx ON messages
      (workspace_id,conversation_id,occurred_at DESC,
       (CASE WHEN payload->>'crm_order' ~ '^[0-9]{1,19}$' THEN (payload->>'crm_order')::numeric ELSE 0 END) DESC,id DESC)
      WHERE payload->>'superseded_by' IS NULL""")


def downgrade():
    lock_tables()
    op.execute("DROP INDEX messages_chat_order_idx")
    op.execute("DROP TRIGGER chat_message_count ON messages")
    op.execute("DROP FUNCTION cm_chat_count_messages()")
    op.execute("DROP TRIGGER chat_read_version ON workspaces")
    for table in TABLES:
        op.execute(f"DROP TRIGGER chat_read_version ON {table}")
    op.execute("DROP FUNCTION cm_chat_bump_version()")
    op.execute("DROP TABLE chat_read_versions")
    op.execute("ALTER TABLE conversations DROP COLUMN stored_message_count")
