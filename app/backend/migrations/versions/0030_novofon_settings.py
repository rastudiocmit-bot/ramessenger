"""Independent tenant-scoped Novofon credentials and read-only checks."""
from alembic import op

revision = "0030_novofon_settings"
down_revision = "0029_invitation_sections"
branch_labels = None
depends_on = None


def upgrade():
    # Retained on downgrade so a failed API release cannot erase a saved key.
    op.execute("""CREATE TABLE IF NOT EXISTS novofon_settings (
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
        connection_id uuid NOT NULL,
        key_ciphertext text,
        version integer NOT NULL DEFAULT 1 CHECK(version > 0),
        sample_hours integer NOT NULL DEFAULT 24 CHECK(sample_hours BETWEEN 1 AND 168),
        status text NOT NULL DEFAULT 'saved'
          CHECK(status IN ('not_configured','saved','checking','api_verified','error')),
        checked_at timestamptz,
        check_token uuid,
        check_until timestamptz,
        report jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(report)='object'),
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by uuid REFERENCES auth_users(id)
    )""")
    op.execute("ALTER TABLE novofon_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE novofon_settings FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON novofon_settings")
    op.execute("""CREATE POLICY tenant_isolation ON novofon_settings TO cm_app
        USING (workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
          AND current_setting('app.membership_admin',true)='true')
        WITH CHECK (workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
          AND current_setting('app.membership_admin',true)='true')""")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON novofon_settings TO cm_app")


def downgrade():
    # Deliberately preserve encrypted credentials after rollback; no existing
    # MAX/CRM table is changed in either direction. Upgrade is repeatable.
    pass
