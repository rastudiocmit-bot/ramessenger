"""«Задачи» becomes its own section an admin can show or hide for each employee.

Everyone who already had «Сообщения» saw the tasks section, so they keep it."""
from alembic import op

revision = "0023_member_tasks_section"
down_revision = "0022_ai_task_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE workspace_memberships SET sections = array_append(sections, 'tasks') "
               "WHERE 'messages' = ANY(sections) AND NOT 'tasks' = ANY(sections)")


def downgrade():
    op.execute("UPDATE workspace_memberships SET sections = array_remove(sections, 'tasks')")
