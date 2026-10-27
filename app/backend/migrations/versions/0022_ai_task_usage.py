"""Usage ledger accepts short model calls made for tasks (drafts, hints, replies)."""
from alembic import op

revision = "0022_ai_task_usage"
down_revision = "0021_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE ai_usage DROP CONSTRAINT ai_usage_kind_check")
    op.execute("ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_kind_check "
               "CHECK (kind IN ('voice','test','verify','synthesis','task'))")


def downgrade():
    # Keep existing task rows; older code never writes this kind.
    op.execute("ALTER TABLE ai_usage DROP CONSTRAINT ai_usage_kind_check")
    op.execute("ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_kind_check "
               "CHECK (kind IN ('voice','test','verify','synthesis')) NOT VALID")
