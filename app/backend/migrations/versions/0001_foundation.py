"""Tenant-scoped storage; no external connectors or message sending yet."""

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None

TABLES = (
    "workspaces",
    "contacts",
    "students",
    "contact_student_links",
    "connections",
    "conversations",
    "messages",
    "source_records",
    "inbound_events",
    "send_commands",
    "outbox_events",
)

DDL = [
    """CREATE TABLE workspaces (
      id uuid PRIMARY KEY,
      name text NOT NULL CHECK (length(btrim(name)) > 0),
      timezone text NOT NULL DEFAULT 'Europe/Moscow',
      created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE contacts (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      display_name text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id)
    )""",
    """CREATE TABLE students (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      display_name text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id)
    )""",
    """CREATE TABLE contact_student_links (
      workspace_id uuid NOT NULL,
      contact_id uuid NOT NULL,
      student_id uuid NOT NULL,
      relationship text NOT NULL DEFAULT 'representative',
      PRIMARY KEY (workspace_id, contact_id, student_id),
      FOREIGN KEY (workspace_id, contact_id) REFERENCES contacts(workspace_id, id),
      FOREIGN KEY (workspace_id, student_id) REFERENCES students(workspace_id, id)
    )""",
    """CREATE TABLE connections (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      kind text NOT NULL CHECK (kind IN (
        'max_account', 'max_bot', 'telegram_account', 'telegram_bot', 'vk_community', 'alfa_crm'
      )),
      name text NOT NULL,
      status text NOT NULL DEFAULT 'not_configured'
        CHECK (status IN ('not_configured', 'connecting', 'active', 'reauth_required', 'error')),
      capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id)
    )""",
    """CREATE TABLE conversations (
      workspace_id uuid NOT NULL,
      id uuid NOT NULL,
      connection_id uuid NOT NULL,
      contact_id uuid,
      external_chat_id text NOT NULL CHECK (length(external_chat_id) > 0),
      external_topic_id text NOT NULL DEFAULT '',
      kind text NOT NULL DEFAULT 'direct' CHECK (kind IN ('direct', 'group')),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      UNIQUE (workspace_id, connection_id, external_chat_id, external_topic_id),
      FOREIGN KEY (workspace_id, connection_id) REFERENCES connections(workspace_id, id),
      FOREIGN KEY (workspace_id, contact_id) REFERENCES contacts(workspace_id, id)
    )""",
    """CREATE TABLE messages (
      workspace_id uuid NOT NULL,
      id uuid NOT NULL,
      conversation_id uuid NOT NULL,
      provider_message_id text,
      direction text NOT NULL CHECK (direction IN ('in', 'out')),
      actor_type text NOT NULL CHECK (
        actor_type IN ('contact', 'human', 'automation', 'ai', 'unknown')
      ),
      text text,
      payload jsonb NOT NULL DEFAULT '{}'::jsonb,
      occurred_at timestamptz NOT NULL,
      received_at timestamptz NOT NULL DEFAULT now(),
      version integer NOT NULL DEFAULT 1 CHECK (version > 0),
      PRIMARY KEY (workspace_id, id),
      UNIQUE (workspace_id, conversation_id, provider_message_id),
      FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id),
      CHECK (coalesce(length(btrim(text)), 0) > 0 OR payload <> '{}'::jsonb)
    )""",
    """CREATE INDEX messages_history_idx
      ON messages (workspace_id, conversation_id, occurred_at DESC, id DESC)""",
    """CREATE TABLE source_records (
      workspace_id uuid NOT NULL,
      id uuid NOT NULL,
      connection_id uuid NOT NULL,
      message_id uuid NOT NULL,
      namespace text NOT NULL CHECK (length(namespace) > 0),
      external_record_id text NOT NULL CHECK (length(external_record_id) > 0),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      UNIQUE (workspace_id, connection_id, namespace, external_record_id),
      FOREIGN KEY (workspace_id, connection_id) REFERENCES connections(workspace_id, id),
      FOREIGN KEY (workspace_id, message_id) REFERENCES messages(workspace_id, id)
    )""",
    """CREATE TABLE inbound_events (
      workspace_id uuid NOT NULL,
      id uuid NOT NULL,
      connection_id uuid NOT NULL,
      event_key text NOT NULL CHECK (length(event_key) > 0),
      event_type text NOT NULL,
      payload jsonb NOT NULL,
      state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'processed', 'failed')),
      received_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      UNIQUE (workspace_id, connection_id, event_key),
      FOREIGN KEY (workspace_id, connection_id) REFERENCES connections(workspace_id, id)
    )""",
    """CREATE TABLE send_commands (
      workspace_id uuid NOT NULL,
      id uuid NOT NULL,
      conversation_id uuid NOT NULL,
      client_request_id text NOT NULL CHECK (length(client_request_id) > 0),
      payload jsonb NOT NULL,
      payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
      state text NOT NULL DEFAULT 'queued' CHECK (
        state IN (
          'queued', 'sending', 'accepted', 'delivered', 'read', 'failed', 'unknown', 'cancelled'
        )
      ),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      UNIQUE (workspace_id, client_request_id),
      FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id)
    )""",
    """CREATE TABLE outbox_events (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      event_key text NOT NULL,
      topic text NOT NULL,
      payload jsonb NOT NULL,
      state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'published', 'failed')),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      UNIQUE (workspace_id, topic, event_key)
    )""",
    """CREATE INDEX outbox_pending_idx ON outbox_events (state, created_at)
      WHERE state = 'pending'""",
    """CREATE INDEX inbound_pending_idx ON inbound_events (state, received_at)
      WHERE state = 'pending'""",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO cm_app")
    for table in TABLES:
        column = "id" if table == "workspaces" else "workspace_id"
        predicate = f"{column} = nullif(current_setting('app.workspace_id', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO cm_app "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO cm_app")
    op.execute("GRANT SELECT ON alembic_version TO cm_app")


def downgrade():
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
