import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))
from center_messenger.card_loader_settings import CardLoaderSettings, public


class CardLoaderSettingsTests(unittest.TestCase):
    def test_runtime_grant_is_added_after_table_creation(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / "app/backend/migrations/versions/0027_card_loader_grant.py").read_text()
        self.assertIn("GRANT SELECT,INSERT,UPDATE,DELETE ON crm_card_loader_settings TO cm_app", migration)
        self.assertIn("nullif(current_setting('app.workspace_id',true),'')::uuid", migration)

    def test_defaults_keep_old_history_immutable(self):
        value = CardLoaderSettings()
        self.assertEqual(value.history_interval_minutes, 0)
        self.assertTrue(value.preload_unanswered)
        self.assertTrue(value.preload_active_groups)
        self.assertEqual(value.requests_per_turn, 3)

    def test_invalid_history_and_retry_ranges_are_rejected(self):
        with self.assertRaises(ValidationError):
            CardLoaderSettings(history_interval_minutes=120)
        with self.assertRaises(ValidationError):
            CardLoaderSettings(retry_initial_seconds=120, retry_max_seconds=60)

    def test_public_contract_reports_fixed_safety_limits(self):
        payload = public(CardLoaderSettings().model_dump())
        self.assertEqual(payload["parallel_card_jobs"], 1)
        self.assertEqual(payload["host_request_gap_ms"], 350)


if __name__ == "__main__":
    unittest.main()
