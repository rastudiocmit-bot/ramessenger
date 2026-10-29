"""Grant the runtime role access to card-loader policy settings."""

from alembic import op

revision = "0027_card_loader_grant"
down_revision = "0026_card_loader_settings"
branch_labels = None
depends_on = None


def upgrade():
    # Migrations run as the owner while API transactions use cm_app. Creating
    # the table does not implicitly grant the runtime role any privileges.
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON crm_card_loader_settings TO cm_app")
    op.execute("DROP POLICY tenant_isolation ON crm_card_loader_settings")
    op.execute("CREATE POLICY tenant_isolation ON crm_card_loader_settings TO cm_app "
               "USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid) "
               "WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)")


def downgrade():
    op.execute("DROP POLICY tenant_isolation ON crm_card_loader_settings")
    op.execute("CREATE POLICY tenant_isolation ON crm_card_loader_settings TO cm_app "
               "USING(workspace_id=current_setting('app.workspace_id',true)::uuid) "
               "WITH CHECK(workspace_id=current_setting('app.workspace_id',true)::uuid)")
    op.execute("REVOKE SELECT,INSERT,UPDATE,DELETE ON crm_card_loader_settings FROM cm_app")
