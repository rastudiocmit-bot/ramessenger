"""Complete family snapshots and bounded durable card scheduling."""
from alembic import op

revision = "0028_card_loader_snapshots"
down_revision = "0027_card_loader_grant"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE crm_card_loader_settings
      ADD COLUMN preload_active_cards boolean NOT NULL DEFAULT true,
      ADD COLUMN active_interval_minutes integer NOT NULL DEFAULT 720 CHECK(active_interval_minutes BETWEEN 360 AND 1440),
      ADD COLUMN automatic_clients_only boolean NOT NULL DEFAULT false,
      ADD COLUMN parallel_card_jobs integer NOT NULL DEFAULT 1 CHECK(parallel_card_jobs BETWEEN 1 AND 2),
      ADD COLUMN requests_per_minute integer NOT NULL DEFAULT 120 CHECK(requests_per_minute BETWEEN 6 AND 170),
      ADD COLUMN pause_background_on_send boolean NOT NULL DEFAULT true,
      ADD COLUMN manual_max_wait_seconds integer NOT NULL DEFAULT 60 CHECK(manual_max_wait_seconds BETWEEN 30 AND 300)
    """)
    op.execute("""CREATE TABLE crm_card_loader_state(
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
      turn bigint NOT NULL DEFAULT 0, manual_streak integer NOT NULL DEFAULT 0,
      api_requests jsonb NOT NULL DEFAULT '[]'
    )""")
    op.execute("""CREATE TABLE crm_family_snapshots(
      workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
      connection_id uuid NOT NULL, scope_key text NOT NULL,
      settings_version integer NOT NULL, payload jsonb NOT NULL,
      published_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(workspace_id,connection_id,scope_key),
      FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id) ON DELETE CASCADE
    )""")
    predicate = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
    for table in ("crm_card_loader_state", "crm_family_snapshots"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} TO cm_app USING ({predicate}) WITH CHECK ({predicate})")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app")
    op.execute("CREATE CONSTRAINT TRIGGER chat_read_version AFTER INSERT OR UPDATE OR DELETE ON crm_family_snapshots "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cm_chat_bump_version('card')")
    op.execute("CREATE CONSTRAINT TRIGGER chat_read_version AFTER INSERT OR UPDATE OR DELETE ON crm_card_loader_settings "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cm_chat_bump_version('card')")


def downgrade():
    op.execute("DROP TRIGGER chat_read_version ON crm_card_loader_settings")
    op.execute("DROP TABLE crm_family_snapshots,crm_card_loader_state")
    op.execute("ALTER TABLE crm_card_loader_settings DROP COLUMN preload_active_cards, DROP COLUMN active_interval_minutes, "
               "DROP COLUMN automatic_clients_only, DROP COLUMN parallel_card_jobs, DROP COLUMN requests_per_minute, "
               "DROP COLUMN pause_background_on_send, DROP COLUMN manual_max_wait_seconds")
