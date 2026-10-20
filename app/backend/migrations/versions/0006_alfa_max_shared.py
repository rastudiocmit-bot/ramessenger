"""Mark a MAX bot that is also configured in AlfaCRM."""

from alembic import op

revision = "0006_alfa_max_shared"
down_revision = "0005_max_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE max_settings ADD COLUMN shared_with_alfa boolean NOT NULL DEFAULT false"
    )


def downgrade():
    op.execute("ALTER TABLE max_settings DROP COLUMN shared_with_alfa")
