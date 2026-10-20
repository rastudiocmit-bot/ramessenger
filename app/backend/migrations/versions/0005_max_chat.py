"""MAX bot credentials, durable work and chat revisions; no remote subscriptions."""

from alembic import op

revision = "0005_max_chat"
down_revision = "0004_crm_directory"
branch_labels = None
depends_on = None

TABLES = ("max_settings", "message_changes", "max_tombstones")


def upgrade():
    op.execute("""CREATE TABLE max_settings (
      workspace_id uuid NOT NULL, connection_id uuid NOT NULL, version integer NOT NULL DEFAULT 1,
      secret_ciphertext text, bot_user_id bigint, bot_name text NOT NULL DEFAULT '',
      username text NOT NULL DEFAULT '', enabled boolean NOT NULL DEFAULT false,
      subscription_confirmed boolean NOT NULL DEFAULT false,
      foreign_subscriptions integer NOT NULL DEFAULT 0,
      checked_at timestamptz, last_inbound_at timestamptz, last_error_code text,
      history_since timestamptz, send_after timestamptz NOT NULL DEFAULT '2000-01-01T00:00:00Z',
      PRIMARY KEY(workspace_id,connection_id),
      FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id)
    )""")
    op.execute("""CREATE TABLE max_routes (
      connection_id uuid PRIMARY KEY, workspace_id uuid NOT NULL,
      token_fingerprint text UNIQUE, remote_bot_id bigint UNIQUE,
      secret_hash text NOT NULL CHECK(secret_hash ~ '^[0-9a-f]{64}$'),
      accept_events boolean NOT NULL DEFAULT false,
      FOREIGN KEY(workspace_id,connection_id)
        REFERENCES max_settings(workspace_id,connection_id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE message_jobs (
      id uuid PRIMARY KEY, workspace_id uuid NOT NULL, connection_id uuid NOT NULL,
      subject_id uuid NOT NULL, kind text NOT NULL CHECK(kind IN ('event','send')),
      due_at timestamptz NOT NULL DEFAULT now(), lease_owner uuid,
      lease_until timestamptz NOT NULL DEFAULT '2000-01-01T00:00:00Z',
      UNIQUE(workspace_id,connection_id,kind,subject_id),
      FOREIGN KEY(workspace_id,connection_id)
        REFERENCES max_settings(workspace_id,connection_id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX message_jobs_due_idx ON message_jobs(due_at)")
    op.execute("ALTER TABLE inbound_events ADD COLUMN error_code text")
    op.execute(
        "CREATE INDEX inbound_events_state_idx ON inbound_events(workspace_id,connection_id,state)"
    )
    for table in ("max_routes", "message_jobs"):
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app")
    op.execute("""ALTER TABLE conversations
      ADD COLUMN peer_user_id bigint,
      ADD COLUMN peer_name text NOT NULL DEFAULT '',
      ADD COLUMN last_message_at timestamptz,
      ADD COLUMN last_inbound_at timestamptz,
      ADD COLUMN answered_until timestamptz NOT NULL DEFAULT '2000-01-01T00:00:00Z',
      ADD COLUMN needs_reply boolean NOT NULL DEFAULT false,
      ADD COLUMN inbound_seq bigint NOT NULL DEFAULT 0,
      ADD COLUMN answered_seq bigint NOT NULL DEFAULT 0,
      ADD COLUMN revision integer NOT NULL DEFAULT 1""")
    op.execute("""CREATE INDEX conversations_inbox_idx ON conversations
      (workspace_id,needs_reply DESC,last_message_at DESC,id)""")
    op.execute("""ALTER TABLE send_commands
      ADD COLUMN requested_by uuid REFERENCES auth_users(id),
      ADD COLUMN reply_through timestamptz,
      ADD COLUMN reply_through_seq bigint NOT NULL DEFAULT 0,
      ADD COLUMN last_error_code text,
      ADD COLUMN attempts integer NOT NULL DEFAULT 0,
      ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()""")
    op.execute("""ALTER TABLE messages ADD COLUMN send_command_id uuid,
      ADD CONSTRAINT messages_send_command_fk FOREIGN KEY(workspace_id,send_command_id)
        REFERENCES send_commands(workspace_id,id),
      ADD CONSTRAINT messages_send_command_unique UNIQUE(workspace_id,send_command_id)""")
    op.execute("""CREATE TABLE message_changes (
      workspace_id uuid NOT NULL, seq bigint GENERATED ALWAYS AS IDENTITY,
      conversation_id uuid NOT NULL, message_id uuid NOT NULL,
      PRIMARY KEY(workspace_id,seq),
      FOREIGN KEY(workspace_id,conversation_id) REFERENCES conversations(workspace_id,id),
      FOREIGN KEY(workspace_id,message_id) REFERENCES messages(workspace_id,id)
    )""")
    op.execute("""CREATE INDEX message_changes_conversation_idx ON message_changes
      (workspace_id,conversation_id,seq)""")
    op.execute("GRANT USAGE ON SEQUENCE message_changes_seq_seq TO cm_app")
    op.execute("""CREATE TABLE max_tombstones (
      workspace_id uuid NOT NULL, connection_id uuid NOT NULL, message_id text NOT NULL,
      source_ms bigint NOT NULL, PRIMARY KEY(workspace_id,connection_id,message_id),
      FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id)
    )""")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        condition = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO cm_app "
            f"USING ({condition}) WITH CHECK ({condition})"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app")


def downgrade():
    op.execute("DROP INDEX inbound_events_state_idx")
    op.execute("ALTER TABLE inbound_events DROP COLUMN error_code")
    op.execute("DROP TABLE message_jobs")
    op.execute("DROP TABLE max_routes")
    op.execute("DROP TABLE max_settings")
    op.execute("DROP TABLE max_tombstones")
    op.execute("DROP TABLE message_changes")
    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_send_command_fk")
    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_send_command_unique")
    op.execute("ALTER TABLE messages DROP COLUMN send_command_id")
    for name in (
        "requested_by",
        "reply_through",
        "reply_through_seq",
        "last_error_code",
        "attempts",
        "updated_at",
    ):
        op.execute(f"ALTER TABLE send_commands DROP COLUMN {name}")
    op.execute("DROP INDEX conversations_inbox_idx")
    for name in (
        "peer_user_id",
        "peer_name",
        "last_message_at",
        "last_inbound_at",
        "answered_until",
        "inbound_seq",
        "answered_seq",
        "needs_reply",
        "revision",
    ):
        op.execute(f"ALTER TABLE conversations DROP COLUMN {name}")
