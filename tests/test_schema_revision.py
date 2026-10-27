"""Keep readiness and release metadata aligned with the actual Alembic head."""

import json
from pathlib import Path
import sys
import unittest

from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/backend/src"))
sys.path.insert(0, str(ROOT / "scripts"))

from center_messenger.database import SCHEMA_REVISION
from github_promote_vps import EXPECTED_DB_REVISION


class SchemaRevisionTests(unittest.TestCase):
    def test_readiness_and_release_match_migration_head(self):
        head = ScriptDirectory(str(ROOT / "app/backend/migrations")).get_current_head()
        self.assertEqual(SCHEMA_REVISION, head)
        self.assertEqual(EXPECTED_DB_REVISION, head)
        self.assertEqual(json.loads((ROOT / "release.json").read_text())["schema_revision"], head)


if __name__ == "__main__":
    unittest.main()
