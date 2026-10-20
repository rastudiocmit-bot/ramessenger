"""Resumable CRM imports and atomically published directory snapshots."""

from alembic import op

revision = "0004_crm_directory"
down_revision = "0003_alfa_connection"
branch_labels = None
depends_on = None

TABLES = (
    "alfa_sync_runs",
    "alfa_sync_control",
    "alfa_customers",
    "alfa_groups",
    "alfa_memberships",
    "alfa_statuses",
    "alfa_directory",
)


def upgrade():
    op.execute(
        "ALTER TABLE contact_student_links "
        "ADD COLUMN managed_by_alfa boolean NOT NULL DEFAULT false"
    )
    op.execute("""CREATE TABLE alfa_sync_runs (
      workspace_id uuid NOT NULL, id uuid NOT NULL, connection_id uuid NOT NULL,
      settings_version integer NOT NULL, requested_by uuid NOT NULL REFERENCES auth_users(id),
      state text NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
      cursor jsonb NOT NULL DEFAULT '{"phase":"branches"}',
      stats jsonb NOT NULL DEFAULT '{}', error_code text,
      created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
      PRIMARY KEY(workspace_id,id),
      FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id)
    )""")
    op.execute("""CREATE TABLE alfa_sync_control (
      workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
      connection_id uuid NOT NULL, requested_by uuid NOT NULL REFERENCES auth_users(id),
      enabled boolean NOT NULL DEFAULT false,
      interval_minutes integer NOT NULL DEFAULT 30 CHECK(interval_minutes BETWEEN 15 AND 1440),
      running_id uuid, published_id uuid, previous_id uuid, last_run_id uuid,
      failures integer NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id),
      FOREIGN KEY(workspace_id,running_id) REFERENCES alfa_sync_runs(workspace_id,id),
      FOREIGN KEY(workspace_id,published_id) REFERENCES alfa_sync_runs(workspace_id,id),
      FOREIGN KEY(workspace_id,previous_id) REFERENCES alfa_sync_runs(workspace_id,id),
      FOREIGN KEY(workspace_id,last_run_id) REFERENCES alfa_sync_runs(workspace_id,id)
    )""")
    op.execute("""CREATE TABLE alfa_customers (
      workspace_id uuid NOT NULL, run_id uuid NOT NULL, branch_id bigint NOT NULL,
      external_id bigint NOT NULL, name text NOT NULL, legal_name text NOT NULL DEFAULT '',
      legal_type integer NOT NULL DEFAULT 1, dob date,
      phones jsonb NOT NULL DEFAULT '[]', emails jsonb NOT NULL DEFAULT '[]',
      status text NOT NULL CHECK(status IN ('client','lead','archive')),
      study_status_id bigint, lead_status_ids jsonb NOT NULL DEFAULT '[]',
      contact_id uuid, student_id uuid, needs_review boolean NOT NULL DEFAULT false,
      PRIMARY KEY(workspace_id,run_id,branch_id,external_id),
      FOREIGN KEY(workspace_id,run_id) REFERENCES alfa_sync_runs(workspace_id,id) ON DELETE CASCADE,
      FOREIGN KEY(workspace_id,contact_id) REFERENCES contacts(workspace_id,id),
      FOREIGN KEY(workspace_id,student_id) REFERENCES students(workspace_id,id)
    )""")
    op.execute("""CREATE INDEX alfa_customers_contact_idx
      ON alfa_customers(workspace_id,run_id,contact_id,branch_id)""")
    op.execute("""CREATE TABLE alfa_groups (
      workspace_id uuid NOT NULL, run_id uuid NOT NULL, branch_id bigint NOT NULL,
      external_id bigint NOT NULL, name text NOT NULL,
      PRIMARY KEY(workspace_id,run_id,branch_id,external_id),
      FOREIGN KEY(workspace_id,run_id) REFERENCES alfa_sync_runs(workspace_id,id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE alfa_memberships (
      workspace_id uuid NOT NULL, run_id uuid NOT NULL, branch_id bigint NOT NULL,
      group_id bigint NOT NULL, external_id bigint NOT NULL, customer_id bigint NOT NULL,
      b_date date, e_date date,
      PRIMARY KEY(workspace_id,run_id,branch_id,group_id,external_id),
      FOREIGN KEY(workspace_id,run_id,branch_id,group_id)
        REFERENCES alfa_groups(workspace_id,run_id,branch_id,external_id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE INDEX alfa_memberships_customer_idx
      ON alfa_memberships(workspace_id,run_id,branch_id,customer_id)""")
    op.execute("""CREATE TABLE alfa_statuses (
      workspace_id uuid NOT NULL, run_id uuid NOT NULL, branch_id bigint NOT NULL,
      kind text NOT NULL CHECK(kind IN ('study-status','lead-status')),
      external_id bigint NOT NULL, name text NOT NULL,
      PRIMARY KEY(workspace_id,run_id,branch_id,kind,external_id),
      FOREIGN KEY(workspace_id,run_id) REFERENCES alfa_sync_runs(workspace_id,id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE alfa_directory (
      workspace_id uuid NOT NULL, run_id uuid NOT NULL, contact_id uuid NOT NULL,
      display_name text NOT NULL, phones jsonb NOT NULL, emails jsonb NOT NULL,
      needs_review boolean NOT NULL DEFAULT false, search_text text NOT NULL,
      search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple',search_text)) STORED,
      PRIMARY KEY(workspace_id,run_id,contact_id),
      FOREIGN KEY(workspace_id,run_id) REFERENCES alfa_sync_runs(workspace_id,id) ON DELETE CASCADE,
      FOREIGN KEY(workspace_id,contact_id) REFERENCES contacts(workspace_id,id)
    )""")
    op.execute("CREATE INDEX alfa_directory_search_idx ON alfa_directory USING gin(search_vector)")
    op.execute("""CREATE INDEX alfa_directory_name_idx
      ON alfa_directory(workspace_id,run_id,display_name,contact_id)""")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        predicate = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO cm_app "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO cm_app")
    # Dispatch IDs/leases only, no names, addresses, CRM credentials or payloads.
    op.execute("""CREATE TABLE alfa_sync_queue (
      workspace_id uuid PRIMARY KEY REFERENCES alfa_sync_control(workspace_id) ON DELETE CASCADE,
      due_at timestamptz NOT NULL DEFAULT now(), lease_owner uuid,
      lease_until timestamptz NOT NULL DEFAULT '-infinity'
    )""")
    op.execute("CREATE INDEX alfa_sync_queue_due_idx ON alfa_sync_queue(due_at)")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON alfa_sync_queue TO cm_app")


def downgrade():
    op.execute("DROP TABLE alfa_sync_queue")
    for table in (
        "alfa_directory",
        "alfa_statuses",
        "alfa_memberships",
        "alfa_groups",
        "alfa_customers",
        "alfa_sync_control",
        "alfa_sync_runs",
    ):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE contact_student_links DROP COLUMN managed_by_alfa")
