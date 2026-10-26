"""Configurable recording duration; existing keys and agents are preserved."""
from alembic import op

revision = "0018_voice_duration"
down_revision = "0017_chat_read_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE ai_settings ADD COLUMN voice_max_seconds integer NOT NULL DEFAULT 180 "
               "CHECK (voice_max_seconds BETWEEN 15 AND 180)")


def downgrade():
    op.execute("ALTER TABLE ai_settings DROP COLUMN voice_max_seconds")
