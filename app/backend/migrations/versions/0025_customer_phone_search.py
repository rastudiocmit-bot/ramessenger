"""Index exact direct matches against every normalized CRM phone."""
from alembic import op

revision = '0025_customer_phone_search'
down_revision = '0024_ai_task_agents'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE INDEX alfa_customers_phones_gin_idx ON alfa_customers USING gin(phones)')


def downgrade():
    op.execute('DROP INDEX alfa_customers_phones_gin_idx')
