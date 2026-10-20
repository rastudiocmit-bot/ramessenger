"""Customer balances from snapshots and bounded per-card CRM detail cache."""

from alembic import op

revision = "0011_crm_family_details"
down_revision = "0010_chat_handling_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE alfa_customers ADD COLUMN balance numeric(14,2)")
    op.execute("ALTER TABLE alfa_customers ADD COLUMN paid_lesson_count integer")
    op.execute("""CREATE TABLE alfa_family_details (
      workspace_id uuid NOT NULL, connection_id uuid NOT NULL,
      branch_id bigint NOT NULL, customer_id bigint NOT NULL,
      settings_version integer NOT NULL, balance numeric(14,2),
      paid_lesson_count integer, subscriptions jsonb NOT NULL DEFAULT '[]',
      lessons jsonb NOT NULL DEFAULT '[]', refreshed_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(workspace_id,connection_id,branch_id,customer_id),
      FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id)
    )""")
    op.execute(
        "CREATE INDEX alfa_family_details_age_idx ON alfa_family_details(workspace_id,refreshed_at)"
    )
    op.execute("ALTER TABLE alfa_family_details ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE alfa_family_details FORCE ROW LEVEL SECURITY")
    predicate = "workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation ON alfa_family_details TO cm_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON alfa_family_details TO cm_app")


def downgrade():
    op.execute("DROP TABLE alfa_family_details")
    op.execute("ALTER TABLE alfa_customers DROP COLUMN paid_lesson_count")
    op.execute("ALTER TABLE alfa_customers DROP COLUMN balance")
