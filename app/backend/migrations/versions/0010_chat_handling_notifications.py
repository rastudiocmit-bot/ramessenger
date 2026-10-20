"""Persist manual chat handling and distinguish new inbound events."""

from alembic import op

revision = "0010_chat_handling_notifications"
down_revision = "0009_message_unread"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE conversations ADD COLUMN handled_inbound_id uuid")
    op.execute("ALTER TABLE conversations ADD COLUMN handled_at timestamptz")
    op.execute("ALTER TABLE conversations ADD COLUMN handled_by uuid REFERENCES auth_users(id)")
    op.execute("ALTER TABLE message_changes ADD COLUMN new_inbound boolean NOT NULL DEFAULT false")


def downgrade():
    op.execute("ALTER TABLE message_changes DROP COLUMN new_inbound")
    op.execute("ALTER TABLE conversations DROP COLUMN handled_by")
    op.execute("ALTER TABLE conversations DROP COLUMN handled_at")
    op.execute("ALTER TABLE conversations DROP COLUMN handled_inbound_id")
