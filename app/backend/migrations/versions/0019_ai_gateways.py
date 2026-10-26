"""Independent encrypted MiMo gateways and action routing; preserve existing defaults."""
from alembic import op

revision = "0019_ai_gateways"
down_revision = "0018_voice_duration"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE ai_settings ADD COLUMN mimo_key_ciphertext text, "
               "ADD COLUMN mimo_plan_key_ciphertext text, "
               "ADD COLUMN gateway_config jsonb NOT NULL DEFAULT '{}'::jsonb "
               "CHECK (jsonb_typeof(gateway_config)='object')")
    op.execute("ALTER TABLE ai_agents DROP CONSTRAINT ai_agents_task_check")
    op.execute("ALTER TABLE ai_agents ADD CONSTRAINT ai_agents_task_check "
               "CHECK (task IN ('voice_input','text_processing'))")
    op.execute("ALTER TABLE ai_agent_versions ADD COLUMN task text NOT NULL DEFAULT 'voice_input'")
    op.execute("ALTER TABLE ai_usage DROP CONSTRAINT ai_usage_kind_check")
    op.execute("ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_kind_check "
               "CHECK (kind IN ('voice','test','verify','synthesis'))")


def downgrade():
    # Keep the new task/usage records; older code cannot create these kinds.
    op.execute("ALTER TABLE ai_usage DROP CONSTRAINT ai_usage_kind_check")
    op.execute("ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_kind_check "
               "CHECK (kind IN ('voice','test','verify')) NOT VALID")
    op.execute("ALTER TABLE ai_agents DROP CONSTRAINT ai_agents_task_check")
    op.execute("ALTER TABLE ai_agents ADD CONSTRAINT ai_agents_task_check "
               "CHECK (task='voice_input') NOT VALID")
    op.execute("ALTER TABLE ai_agent_versions DROP COLUMN task")
    op.execute("ALTER TABLE ai_settings DROP COLUMN gateway_config, "
               "DROP COLUMN mimo_key_ciphertext, DROP COLUMN mimo_plan_key_ciphertext")
