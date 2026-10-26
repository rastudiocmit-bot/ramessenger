"""Diagnostic output contains safe codes/timings, never SQL or user data."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy.exc import OperationalError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))
from center_messenger.cabinet_summary import _query
from center_messenger.summary_audit import failure, main


class SummaryAuditTests(unittest.TestCase):
    def test_failed_calculation_identifies_stage_without_exposing_query_or_driver_details(self):
        cause = RuntimeError("private driver message")
        cause.sqlstate = "57014"
        error = OperationalError("private SQL", {"key": "private token"}, cause)
        db, stages = Mock(), []
        db.execute.side_effect = error
        with self.assertRaises(OperationalError), self.assertLogs("center_messenger.cabinet_summary"):
            _query(db, "responses", "private SQL", {"key": "private token"}, diagnostics=stages)
        self.assertEqual(stages[0]["stage"], "responses")
        self.assertEqual(stages[0]["sqlstate"], "57014")
        self.assertNotIn("private", str(stages) + str(failure(error)))
        self.assertNotIn("statement_timeout", str(stages))

    def test_default_audit_does_not_run_calculations_and_calculate_is_explicit(self):
        engine = Mock()
        with patch("center_messenger.summary_audit.make_engine", return_value=engine), \
             patch("center_messenger.summary_audit.inspect", return_value={"workspaces": []}) as inspect, \
             patch("builtins.print"):
            self.assertEqual(main([]), 0)
            self.assertFalse(inspect.call_args.kwargs["run_calculation"])
            self.assertEqual(main(["--calculate"]), 0)
            self.assertTrue(inspect.call_args.kwargs["run_calculation"])
        self.assertEqual(engine.dispose.call_count, 2)


if __name__ == "__main__":
    unittest.main()
