"""Separate AlfaCRM's native MAX web session from MAX Bot API settings."""

from alembic import op

revision = "0007_alfa_max_browser"
down_revision = "0006_alfa_max_shared"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE max_settings DROP COLUMN shared_with_alfa")
    # Queue dispatch is global metadata. Store the channel on the job itself so
    # workers can claim jobs without reading tenant-scoped connection rows.
    # The old FK only allowed MAX Bot jobs; generalize it to all connections.
    op.execute(
        "ALTER TABLE message_jobs DROP CONSTRAINT message_jobs_workspace_id_connection_id_fkey"
    )
    op.execute(
        "ALTER TABLE message_jobs ADD CONSTRAINT message_jobs_workspace_id_connection_id_fkey "
        "FOREIGN KEY (workspace_id,connection_id) REFERENCES connections(workspace_id,id) "
        "ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE message_jobs ADD COLUMN channel_kind text NOT NULL DEFAULT 'max_bot' "
        "CHECK (channel_kind IN ('max_bot','alfa_max_browser'))"
    )
    op.execute(
        "CREATE INDEX message_jobs_channel_due_idx ON message_jobs(channel_kind,kind,due_at)"
    )
    op.execute("ALTER TABLE connections DROP CONSTRAINT connections_kind_check")
    op.execute(
        "ALTER TABLE connections ADD CONSTRAINT connections_kind_check CHECK (kind IN ("
        "'max_account','max_bot','telegram_account','telegram_bot','vk_community','alfa_crm',"
        "'alfa_max_browser'))"
    )
    op.execute("""CREATE TABLE alfa_max_settings (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
      connection_id uuid NOT NULL UNIQUE,
      crm_connection_id uuid NOT NULL,
      email text NOT NULL,
      state text NOT NULL DEFAULT 'not_configured'
        CHECK(state IN ('not_configured','otp_required','active','reauth_required','error')),
      session_ciphertext text,
      version integer NOT NULL DEFAULT 1 CHECK(version > 0),
      history_cursor jsonb NOT NULL DEFAULT '{}'::jsonb,
      last_sync_at timestamptz,
      last_error_code text,
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(workspace_id,connection_id),
      FOREIGN KEY (workspace_id,connection_id) REFERENCES connections(workspace_id,id),
      FOREIGN KEY (workspace_id,crm_connection_id) REFERENCES connections(workspace_id,id)
    )""")
    op.execute("""CREATE INDEX alfa_max_settings_state_idx
      ON alfa_max_settings(state,last_sync_at)""")
    op.execute("""CREATE TABLE alfa_max_sync_queue (
      workspace_id uuid PRIMARY KEY REFERENCES alfa_max_settings(workspace_id) ON DELETE CASCADE,
      connection_id uuid NOT NULL,
      due_at timestamptz NOT NULL DEFAULT now(), lease_owner uuid,
      lease_until timestamptz NOT NULL DEFAULT '-infinity',
      FOREIGN KEY (workspace_id,connection_id)
        REFERENCES alfa_max_settings(workspace_id,connection_id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX alfa_max_sync_due_idx ON alfa_max_sync_queue(due_at)")
    predicate = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
    op.execute("ALTER TABLE alfa_max_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE alfa_max_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON alfa_max_settings TO cm_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON alfa_max_settings TO cm_app")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON alfa_max_sync_queue TO cm_app")


def downgrade():
    # Native AlfaCRM tasks cannot exist in the pre-channel-aware queue schema.
    op.execute("DELETE FROM message_jobs WHERE channel_kind <> 'max_bot'")
    op.execute("DROP INDEX message_jobs_channel_due_idx")
    op.execute(
        "ALTER TABLE message_jobs DROP CONSTRAINT message_jobs_workspace_id_connection_id_fkey"
    )
    op.execute("ALTER TABLE message_jobs DROP COLUMN channel_kind")
    op.execute(
        "ALTER TABLE message_jobs ADD CONSTRAINT message_jobs_workspace_id_connection_id_fkey "
        "FOREIGN KEY (workspace_id,connection_id) "
        "REFERENCES max_settings(workspace_id,connection_id) ON DELETE CASCADE"
    )
    op.execute("DROP TABLE alfa_max_sync_queue")
    op.execute("DROP TABLE alfa_max_settings")
    op.execute("ALTER TABLE connections DROP CONSTRAINT connections_kind_check")
    op.execute(
        "ALTER TABLE connections ADD CONSTRAINT connections_kind_check CHECK (kind IN ("
        "'max_account','max_bot','telegram_account','telegram_bot','vk_community','alfa_crm',"
        "'alfa_max_browser'))"
    )
    op.execute(
        "ALTER TABLE max_settings ADD COLUMN shared_with_alfa boolean NOT NULL DEFAULT false"
    )
