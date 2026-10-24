"""Yandex Cloud voice input: settings, AI agents with versions, usage ledger.

Additive only: the running 0012 code never reads these tables, so the
migration can be applied before the new image is promoted.
"""

from alembic import op

revision = "0013_ai_voice"
down_revision = "0012_member_sections"
branch_labels = None
depends_on = None

TABLES = ("ai_settings", "ai_agents", "ai_agent_versions", "ai_usage")


def upgrade():
    op.execute("""CREATE TABLE ai_settings (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
      folder_id text NOT NULL DEFAULT '' CHECK (folder_id ~ '^[a-z0-9]{0,50}$'),
      key_ciphertext text,
      key_hint text NOT NULL DEFAULT '' CHECK (length(key_hint) <= 8),
      voice_enabled boolean NOT NULL DEFAULT false,
      monthly_limit_rub numeric(10,2) CHECK (monthly_limit_rub IS NULL OR monthly_limit_rub >= 0),
      status text NOT NULL DEFAULT 'not_configured'
        CHECK (status IN ('not_configured','active','error')),
      last_error_code text,
      verified_at timestamptz,
      version integer NOT NULL DEFAULT 1 CHECK (version > 0),
      updated_at timestamptz NOT NULL DEFAULT now(),
      updated_by uuid
    )""")
    op.execute("""CREATE TABLE ai_agents (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
      task text NOT NULL DEFAULT 'voice_input' CHECK (task IN ('voice_input')),
      model text NOT NULL CHECK (model ~ '^[a-z0-9][a-z0-9._-]{0,79}$'),
      prompt text NOT NULL CHECK (length(btrim(prompt)) BETWEEN 1 AND 4000),
      temperature numeric(3,2) NOT NULL DEFAULT 0.2 CHECK (temperature BETWEEN 0 AND 1),
      enabled boolean NOT NULL DEFAULT true,
      is_default boolean NOT NULL DEFAULT false,
      version integer NOT NULL DEFAULT 1 CHECK (version > 0),
      deleted_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      updated_by uuid,
      PRIMARY KEY (workspace_id, id)
    )""")
    op.execute(
        "CREATE UNIQUE INDEX ai_agents_default_idx ON ai_agents(workspace_id, task) "
        "WHERE is_default AND deleted_at IS NULL"
    )
    op.execute("""CREATE TABLE ai_agent_versions (
      workspace_id uuid NOT NULL,
      agent_id uuid NOT NULL,
      version integer NOT NULL CHECK (version > 0),
      name text NOT NULL,
      model text NOT NULL,
      prompt text NOT NULL,
      temperature numeric(3,2) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      created_by uuid,
      PRIMARY KEY (workspace_id, agent_id, version),
      FOREIGN KEY (workspace_id, agent_id) REFERENCES ai_agents(workspace_id, id)
    )""")
    op.execute("""CREATE TABLE ai_usage (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      id uuid NOT NULL,
      user_id uuid,
      agent_id uuid,
      kind text NOT NULL CHECK (kind IN ('voice','test','verify')),
      audio_seconds numeric(7,2) NOT NULL DEFAULT 0 CHECK (audio_seconds >= 0),
      input_tokens integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
      output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
      cost_rub numeric(10,4) NOT NULL DEFAULT 0 CHECK (cost_rub >= 0),
      outcome text NOT NULL CHECK (outcome IN ('ok','fallback','error')),
      error_code text,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, id)
    )""")
    op.execute("CREATE INDEX ai_usage_month_idx ON ai_usage(workspace_id, created_at)")
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
    for table in ("ai_usage", "ai_agent_versions", "ai_agents", "ai_settings"):
        op.execute(f"DROP TABLE {table}")
