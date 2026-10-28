"""AI agents gain a third job: tasks made from chat messages (title, details, replies)."""
from alembic import op

revision = "0024_ai_task_agents"
down_revision = "0023_member_tasks_section"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE ai_agents DROP CONSTRAINT ai_agents_task_check")
    op.execute("ALTER TABLE ai_agents ADD CONSTRAINT ai_agents_task_check "
               "CHECK (task IN ('voice_input','text_processing','tasks'))")


def downgrade():
    # Older code never creates task agents; keep existing rows readable.
    op.execute("ALTER TABLE ai_agents DROP CONSTRAINT ai_agents_task_check")
    op.execute("ALTER TABLE ai_agents ADD CONSTRAINT ai_agents_task_check "
               "CHECK (task IN ('voice_input','text_processing')) NOT VALID")
