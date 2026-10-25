"""Separate financial freshness from recent lessons and their daily full check.

Additive columns; existing lesson snapshots retain their original read time.
Old application images can still read/write the table during promotion.
"""

from alembic import op

revision = "0015_family_refresh_tiers"
down_revision = "0014_ai_deepseek"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE alfa_family_details ADD COLUMN lessons_refreshed_at timestamptz, "
               "ADD COLUMN lessons_full_refreshed_at timestamptz")
    op.execute("UPDATE alfa_family_details SET lessons_refreshed_at=refreshed_at, "
               "lessons_full_refreshed_at=refreshed_at")


def downgrade():
    op.execute("ALTER TABLE alfa_family_details DROP COLUMN lessons_full_refreshed_at, "
               "DROP COLUMN lessons_refreshed_at")
