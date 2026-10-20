"""Encrypted AlfaCRM configuration and atomic branch cache, no client import."""

from alembic import op

revision = "0003_alfa_connection"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE alfa_settings (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
      connection_id uuid NOT NULL,
      origin text NOT NULL,
      api_email text NOT NULL,
      secret_ciphertext text,
      version integer NOT NULL DEFAULT 1 CHECK (version > 0),
      seed_branch_id integer CHECK (seed_branch_id > 0),
      updated_at timestamptz NOT NULL DEFAULT now(),
      last_checked_at timestamptz,
      last_success_at timestamptz,
      last_error_code text,
      FOREIGN KEY (workspace_id,connection_id) REFERENCES connections(workspace_id,id)
    )""")
    op.execute("""CREATE TABLE alfa_branches (
      workspace_id uuid NOT NULL,
      connection_id uuid NOT NULL,
      external_id bigint NOT NULL CHECK (external_id > 0),
      name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
      available boolean NOT NULL DEFAULT true,
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id,connection_id,external_id),
      FOREIGN KEY (workspace_id,connection_id) REFERENCES connections(workspace_id,id)
    )""")
    for table in ("alfa_settings", "alfa_branches"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        predicate = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO cm_app "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app")
    # Shared opaque host budgets serialize probes and token refresh across workers/tenants.
    # No API exposes this table, and it contains no hostnames or credentials.
    op.execute("""CREATE TABLE alfa_request_leases (
      host_hash text PRIMARY KEY CHECK (host_hash ~ '^[a-f0-9]{64}$'),
      lease_owner uuid NOT NULL,
      lease_until timestamptz NOT NULL,
      next_request_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON alfa_request_leases TO cm_app")


def downgrade():
    for table in ("alfa_request_leases", "alfa_branches", "alfa_settings"):
        op.execute(f"DROP TABLE {table}")
