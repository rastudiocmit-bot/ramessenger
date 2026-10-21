"""Per-member visible sidebar sections for non-admin roles."""

from alembic import op

revision = "0012_member_sections"
down_revision = "0011_crm_family_details"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE workspace_memberships "
        "ADD COLUMN sections text[] NOT NULL DEFAULT '{}'"
    )


def downgrade():
    op.execute("ALTER TABLE workspace_memberships DROP COLUMN sections")
