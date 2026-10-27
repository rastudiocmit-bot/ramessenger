"""Record the actual end of a daily lesson check which reuses recent coverage."""
from alembic import op

revision = "0020_lesson_history_coverage"
down_revision = "0019_ai_gateways"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE alfa_family_details ADD COLUMN lessons_history_through date")
    op.execute("CREATE CONSTRAINT TRIGGER chat_read_version AFTER INSERT OR UPDATE OR DELETE ON alfa_statuses "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cm_chat_bump_version('card')")


def downgrade():
    op.execute("DROP TRIGGER chat_read_version ON alfa_statuses")
    op.execute("ALTER TABLE alfa_family_details DROP COLUMN lessons_history_through")
