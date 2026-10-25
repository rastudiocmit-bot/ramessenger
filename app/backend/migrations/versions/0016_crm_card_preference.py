"""Keep explicit operator links independent of automatic display preferences."""
from alembic import op

revision = "0016_crm_card_preference"
down_revision = "0015_family_refresh_tiers"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE conversations ADD COLUMN crm_link_manual boolean NOT NULL DEFAULT false")
    op.execute("UPDATE conversations c SET crm_link_manual=true WHERE EXISTS(SELECT 1 FROM auth_events a "
               "WHERE a.workspace_id=c.workspace_id AND a.subject_id=c.id AND a.action='chat_contact_linked')")
    op.execute("CREATE INDEX alfa_customers_primary_phone_idx ON alfa_customers(workspace_id,run_id,(phones->>0)) "
               "WHERE phones->>0 IS NOT NULL")


def downgrade():
    op.execute("DROP INDEX alfa_customers_primary_phone_idx")
    op.execute("ALTER TABLE conversations DROP COLUMN crm_link_manual")
