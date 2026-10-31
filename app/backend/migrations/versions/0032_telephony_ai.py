"""Durable telephone-only AI runs and operation receipts; additive and retained."""
from alembic import op

revision='0032_telephony_ai'
down_revision='0031_telephony_calls'
branch_labels=depends_on=None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_ai_settings (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
      auto_new boolean NOT NULL DEFAULT false, enabled_after timestamptz,
      version integer NOT NULL DEFAULT 1 CHECK(version>0), configured_by uuid,
      updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK(NOT auto_new OR enabled_after IS NOT NULL AND configured_by IS NOT NULL)
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_ai_runs (
      workspace_id uuid NOT NULL, id uuid NOT NULL, call_id uuid NOT NULL,
      request_id uuid NOT NULL, requested_by uuid NOT NULL,
      historical boolean NOT NULL DEFAULT false,
      profile jsonb NOT NULL CHECK(jsonb_typeof(profile)='object'),
      phase text NOT NULL DEFAULT 'queued' CHECK(phase IN
        ('queued','waiting_recording','fetching','recognizing','summarizing','complete','error','needs_review','budget_wait')),
      audio_hash text CHECK(audio_hash ~ '^[a-f0-9]{64}$'),
      gateway_hash text CHECK(gateway_hash ~ '^[a-f0-9]{64}$'),
      channels integer CHECK(channels IN (1,2)), part integer NOT NULL DEFAULT 0 CHECK(part>=0),
      transcript text, summary text, error_code text,
      attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0), retry_at timestamptz,
      started_at timestamptz,
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,request_id),
      FOREIGN KEY(workspace_id,call_id) REFERENCES telephony_calls(workspace_id,id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS telephony_ai_runs_work_idx ON telephony_ai_runs(workspace_id,phase,retry_at,created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS telephony_ai_runs_call_idx ON telephony_ai_runs(workspace_id,call_id,created_at DESC)")
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_ai_links (
      workspace_id uuid NOT NULL, call_id uuid NOT NULL, run_id uuid NOT NULL,
      summary_override text CHECK(length(summary_override)<=6000), edited_by uuid, edited_at timestamptz,
      edit_version integer NOT NULL DEFAULT 0 CHECK(edit_version>=0),
      PRIMARY KEY(workspace_id,call_id),
      FOREIGN KEY(workspace_id,call_id) REFERENCES telephony_calls(workspace_id,id) ON DELETE CASCADE,
      FOREIGN KEY(workspace_id,run_id) REFERENCES telephony_ai_runs(workspace_id,id)
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_ai_operations (
      workspace_id uuid NOT NULL, run_id uuid NOT NULL, operation text NOT NULL,
      input_hash text NOT NULL CHECK(input_hash ~ '^[a-f0-9]{64}$'),
      status text NOT NULL CHECK(status IN ('sent','complete','unknown')),
      result text, usable boolean NOT NULL DEFAULT true,
      cost_rub numeric(12,4) NOT NULL DEFAULT 0 CHECK(cost_rub>=0),
      audio_seconds numeric(12,4) NOT NULL DEFAULT 0 CHECK(audio_seconds>=0),
      input_tokens integer NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
      output_tokens integer NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
      started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
      PRIMARY KEY(workspace_id,run_id,operation),
      FOREIGN KEY(workspace_id,run_id) REFERENCES telephony_ai_runs(workspace_id,id) ON DELETE CASCADE
    )""")
    condition="workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
    for table in ('telephony_ai_settings','telephony_ai_runs','telephony_ai_links','telephony_ai_operations'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {table}')
        op.execute(f'CREATE POLICY tenant_isolation ON {table} TO cm_app USING ({condition}) WITH CHECK ({condition})')
        op.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app')
    # Routing IDs and leases only; no audio, telephone numbers, text or secrets.
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_ai_routes (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
      due_at timestamptz NOT NULL DEFAULT now(), lease_owner uuid,run_id uuid,
      lease_until timestamptz NOT NULL DEFAULT '2000-01-01T00:00:00Z'
    )""")
    op.execute('CREATE INDEX IF NOT EXISTS telephony_ai_routes_due_idx ON telephony_ai_routes(due_at)')
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON telephony_ai_routes TO cm_app')
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_archive_control (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
      enabled boolean NOT NULL DEFAULT false, version integer NOT NULL DEFAULT 1 CHECK(version>0),
      started_by uuid NOT NULL, cursor_start timestamptz NOT NULL,
      cursor_end timestamptz, page_offset integer NOT NULL DEFAULT 0 CHECK(page_offset>=0),
      account_id text, last_started_at timestamptz, last_imported_at timestamptz, last_fetch_at timestamptz,
      imported bigint NOT NULL DEFAULT 0 CHECK(imported>=0),
      last_error_code text, retry_at timestamptz, caught_up_at timestamptz,
      updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute('ALTER TABLE telephony_archive_control ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE telephony_archive_control FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON telephony_archive_control')
    op.execute(f'CREATE POLICY tenant_isolation ON telephony_archive_control TO cm_app USING ({condition}) WITH CHECK ({condition})')
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON telephony_archive_control TO cm_app')
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_archive_seen (
      workspace_id uuid NOT NULL,call_id uuid NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(workspace_id,call_id),
      FOREIGN KEY(workspace_id,call_id) REFERENCES telephony_calls(workspace_id,id) ON DELETE CASCADE
    )""")
    op.execute('ALTER TABLE telephony_archive_seen ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE telephony_archive_seen FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON telephony_archive_seen')
    op.execute(f'CREATE POLICY tenant_isolation ON telephony_archive_seen TO cm_app USING ({condition}) WITH CHECK ({condition})')
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON telephony_archive_seen TO cm_app')
    op.execute('ALTER TABLE ai_usage DROP CONSTRAINT IF EXISTS ai_usage_kind_check')
    op.execute("ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_kind_check CHECK(kind IN ('voice','test','verify','synthesis','task','call_asr','call_summary'))")


def downgrade():
    # Rollback restores old processes/revision, not an older database dump.
    # Imported calls, paid receipts, manual edits and partial results survive.
    pass
