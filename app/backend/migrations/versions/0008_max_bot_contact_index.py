"""Index MAX bot conversations by their linked CRM contact."""

from alembic import op

revision = "0008_max_bot_contact_index"
down_revision = "0007_alfa_max_browser"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX conversations_contact_channel_idx "
        "ON conversations(workspace_id,contact_id,connection_id) "
        "WHERE contact_id IS NOT NULL"
    )


def downgrade():
    op.execute("DROP INDEX conversations_contact_channel_idx")
