"""Persist the chosen section grants in a single-use employee invitation."""
from alembic import op

revision = "0029_invitation_sections"
down_revision = "0028_card_loader_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE auth_invitations ADD COLUMN sections text[] NOT NULL DEFAULT '{}' "
               "CHECK (sections <@ ARRAY['overview','security','messages','tasks']::text[])")


def downgrade():
    op.execute("ALTER TABLE auth_invitations DROP COLUMN sections")
