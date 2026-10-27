"""Session policy and browser cookie must expire at the same absolute deadline."""

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import patch

from fastapi import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))

from center_messenger.accounts_api import signed_in
from center_messenger.config import Settings


class SessionLifetimeTests(unittest.TestCase):
    def test_mobile_session_defaults_and_cookie_lifetime(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.session_idle_minutes, 6000)
        self.assertEqual(settings.session_hours, 1200)

        actor = SimpleNamespace(public_user=lambda: {"id": "test"})
        accounts = SimpleNamespace(
            settings=settings,
            authenticate=lambda raw: actor,
            workspaces=lambda identity: [],
        )
        response = Response()
        with patch("center_messenger.accounts_api.service", return_value=accounts):
            signed_in(None, response, "test-session-token")
        self.assertIn("Max-Age=4320000", response.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
