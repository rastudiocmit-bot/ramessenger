"""DeepSeek as a second model provider for AI agents: its own sealed API key.

Additive only: three nullable/defaulted columns that the running 0013 code
never reads, so the migration can be applied before the new image is promoted.
"""

from alembic import op

revision = "0014_ai_deepseek"
down_revision = "0013_ai_voice"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE ai_settings "
        "ADD COLUMN deepseek_key_ciphertext text, "
        "ADD COLUMN deepseek_key_hint text NOT NULL DEFAULT '' CHECK (length(deepseek_key_hint) <= 8), "
        "ADD COLUMN deepseek_status text NOT NULL DEFAULT 'not_configured' "
        "CHECK (deepseek_status IN ('not_configured','active','error')), "
        "ADD COLUMN deepseek_error_code text"
    )


def downgrade():
    op.execute(
        "ALTER TABLE ai_settings DROP COLUMN deepseek_error_code, DROP COLUMN deepseek_status, "
        "DROP COLUMN deepseek_key_hint, DROP COLUMN deepseek_key_ciphertext"
    )
