"""Workspace settings for the durable AlfaCRM family-card loader."""

from alembic import op

revision = "0026_card_loader_settings"
down_revision = "0025_customer_phone_search"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE crm_card_loader_settings(
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
        preload_unanswered boolean NOT NULL DEFAULT true,
        preload_active_groups boolean NOT NULL DEFAULT true,
        finance_interval_minutes integer NOT NULL DEFAULT 30 CHECK(finance_interval_minutes BETWEEN 15 AND 1440),
        lessons_interval_minutes integer NOT NULL DEFAULT 30 CHECK(lessons_interval_minutes BETWEEN 15 AND 1440),
        groups_interval_minutes integer NOT NULL DEFAULT 360 CHECK(groups_interval_minutes BETWEEN 60 AND 10080),
        history_interval_minutes integer NOT NULL DEFAULT 0 CHECK(history_interval_minutes=0 OR history_interval_minutes BETWEEN 360 AND 10080),
        requests_per_turn integer NOT NULL DEFAULT 3 CHECK(requests_per_turn BETWEEN 1 AND 10),
        busy_retry_seconds integer NOT NULL DEFAULT 5 CHECK(busy_retry_seconds BETWEEN 1 AND 60),
        retry_initial_seconds integer NOT NULL DEFAULT 30 CHECK(retry_initial_seconds BETWEEN 5 AND 300),
        retry_max_seconds integer NOT NULL DEFAULT 1800 CHECK(retry_max_seconds BETWEEN 60 AND 86400),
        manual_priority_seconds integer NOT NULL DEFAULT 30 CHECK(manual_priority_seconds BETWEEN 10 AND 300),
        manual_interest_minutes integer NOT NULL DEFAULT 10 CHECK(manual_interest_minutes BETWEEN 1 AND 60),
        manual_burst integer NOT NULL DEFAULT 5 CHECK(manual_burst BETWEEN 1 AND 20),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK(retry_max_seconds>=retry_initial_seconds)
      )
    """)
    op.execute("ALTER TABLE crm_card_loader_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE crm_card_loader_settings FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON crm_card_loader_settings TO cm_app "
               "USING(workspace_id=current_setting('app.workspace_id',true)::uuid) "
               "WITH CHECK(workspace_id=current_setting('app.workspace_id',true)::uuid)")


def downgrade():
    op.execute("DROP TABLE crm_card_loader_settings")
