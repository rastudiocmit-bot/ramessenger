"""Independent call journal and resumable telephony work; no MAX table writes."""
from alembic import op

revision = '0031_telephony_calls'
down_revision = '0030_novofon_settings'
branch_labels = depends_on = None


def upgrade():
    # Retain these additive tables on downgrade: calls imported during a failed
    # release must survive rollback, just like the encrypted Novofon settings.
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_calls (
      workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
      id uuid NOT NULL, provider text NOT NULL CHECK(provider='novofon'),
      account_id text NOT NULL, source_id text NOT NULL, connection_id uuid NOT NULL,
      phone text CHECK(phone ~ '^\\+[1-9][0-9]{7,14}$'),
      direction text NOT NULL CHECK(direction IN ('in','out')),
      started_at timestamptz NOT NULL, finished_at timestamptz,
      talk_seconds integer CHECK(talk_seconds BETWEEN 0 AND 604800),
      total_seconds integer CHECK(total_seconds BETWEEN 0 AND 604800),
      missed boolean NOT NULL, employees jsonb NOT NULL DEFAULT '[]'::jsonb,
      recording_ciphertext text, recording_fingerprint text, fingerprint text NOT NULL,
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,provider,account_id,source_id),
      CHECK(finished_at IS NULL OR finished_at>=started_at),
      CHECK(jsonb_typeof(employees)='array')
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS telephony_calls_phone_idx ON telephony_calls(workspace_id,phone,started_at DESC,id DESC)")
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_state (
      workspace_id uuid PRIMARY KEY REFERENCES novofon_settings(workspace_id) ON DELETE CASCADE,
      enabled boolean NOT NULL DEFAULT true, history_days integer NOT NULL DEFAULT 7 CHECK(history_days BETWEEN 1 AND 90),
      poll_seconds integer NOT NULL DEFAULT 60 CHECK(poll_seconds BETWEEN 30 AND 3600),
      settings_version integer, account_id text, timezone text, connected boolean NOT NULL DEFAULT false,
      recent jsonb NOT NULL DEFAULT '{}'::jsonb, archive jsonb NOT NULL DEFAULT '{}'::jsonb,
      recent_due timestamptz NOT NULL DEFAULT now(), archive_done boolean NOT NULL DEFAULT false,
      last_sync_at timestamptz, last_error_code text, retry_at timestamptz,
      attempts integer NOT NULL DEFAULT 0, imported bigint NOT NULL DEFAULT 0,
      version integer NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK(jsonb_typeof(recent)='object' AND jsonb_typeof(archive)='object')
    )""")
    for table in ('telephony_calls','telephony_state'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {table}')
        condition = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
        if table == 'telephony_state':
            op.execute(f'CREATE POLICY tenant_isolation ON {table} FOR SELECT TO cm_app USING ({condition})')
            op.execute(f'DROP POLICY IF EXISTS tenant_write ON {table}')
            condition += " AND current_setting('app.membership_admin',true)='true'"
            op.execute(f'CREATE POLICY tenant_write ON {table} TO cm_app USING ({condition}) WITH CHECK ({condition})')
        else:
            op.execute(f'CREATE POLICY tenant_isolation ON {table} TO cm_app USING ({condition}) WITH CHECK ({condition})')
        op.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app')
    # Like max_routes, this is a global worker routing table: IDs and leases
    # only. No keys, numbers, call contents or account IDs may be placed here.
    op.execute("""CREATE TABLE IF NOT EXISTS telephony_routes (
      workspace_id uuid PRIMARY KEY REFERENCES novofon_settings(workspace_id) ON DELETE CASCADE,
      due_at timestamptz NOT NULL DEFAULT now(), lease_owner uuid,
      lease_until timestamptz NOT NULL DEFAULT '2000-01-01T00:00:00Z'
    )""")
    op.execute('CREATE INDEX IF NOT EXISTS telephony_routes_due_idx ON telephony_routes(due_at)')
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON telephony_routes TO cm_app')
    op.execute("INSERT INTO telephony_state(workspace_id) SELECT workspace_id FROM novofon_settings WHERE key_ciphertext IS NOT NULL ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO telephony_routes(workspace_id) SELECT workspace_id FROM novofon_settings WHERE key_ciphertext IS NOT NULL ON CONFLICT DO NOTHING")


def downgrade():
    pass
