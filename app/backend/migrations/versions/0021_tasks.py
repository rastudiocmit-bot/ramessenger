"""Operator tasks made from chat messages: promises to a client with a due time.

Additive only: the running code never reads these tables, so the migration can
be applied before the new image is promoted.
"""
from alembic import op

revision = "0021_tasks"
down_revision = "0020_lesson_history_coverage"
branch_labels = None
depends_on = None

TABLES = ("tasks", "task_events")


def upgrade():
    op.execute("""CREATE TABLE tasks (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      conversation_id uuid,
      client_label text NOT NULL DEFAULT '' CHECK (length(client_label) <= 200),
      title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
      details text NOT NULL DEFAULT '' CHECK (length(details) <= 2000),
      kind text CHECK (kind IN ('makeup','subscription','group','call','other')),
      status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
      due_at timestamptz NOT NULL,
      origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('menu','select','voice','ai_hint','manual')),
      sources jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(sources)='array'),
      created_by uuid NOT NULL,
      assignee_id uuid NOT NULL,
      assignee_name text NOT NULL DEFAULT '' CHECK (length(assignee_name) <= 200),
      completed_at timestamptz,
      completed_by uuid,
      version integer NOT NULL DEFAULT 1 CHECK (version > 0),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id)
    )""")
    op.execute("CREATE INDEX tasks_open_idx ON tasks(workspace_id, assignee_id, due_at) WHERE status='open'")
    op.execute("CREATE INDEX tasks_conversation_idx ON tasks(workspace_id, conversation_id, status)")
    op.execute("CREATE INDEX tasks_recent_idx ON tasks(workspace_id, updated_at DESC)")
    op.execute("""CREATE TABLE task_events (
      workspace_id uuid NOT NULL,
      id uuid NOT NULL,
      task_id uuid NOT NULL,
      kind text NOT NULL CHECK (kind IN ('created','edited','snoozed','assigned','done','reopened','cancelled')),
      actor_id uuid,
      data jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id),
      FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX task_events_task_idx ON task_events(workspace_id, task_id, created_at)")
    predicate = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO cm_app "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app")


def downgrade():
    op.execute("DROP TABLE task_events")
    op.execute("DROP TABLE tasks")
