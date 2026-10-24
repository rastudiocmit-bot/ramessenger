"""Live diagnostics must not become checkpoints or cross lease/workspace boundaries."""
import sys
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))
from center_messenger.alfa_monitor import AlfaSyncMonitor, RECENT_LIMIT


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = AlfaSyncMonitor()
        self.workspace, self.channel, self.lease = uuid4(), uuid4(), uuid4()
        self.monitor.begin(self.workspace, self.channel, self.lease, {}, {"phase": "repair"})

    def test_a_stale_lease_or_another_workspace_cannot_read_update_or_close_the_live_client(self):
        self.monitor.update(self.workspace, self.channel, self.lease, {"client": {"name": "Example"}})
        other = uuid4()
        self.assertIsNone(self.monitor.snapshot(other, self.channel, self.lease))
        self.assertIsNone(self.monitor.snapshot(self.workspace, self.channel, other))
        self.monitor.close(self.workspace, self.channel, other)
        self.assertEqual(self.monitor.snapshot(self.workspace, self.channel, self.lease)["client"]["name"], "Example")

    def test_recent_operations_are_bounded_and_callers_cannot_mutate_shared_state(self):
        for n in range(RECENT_LIMIT + 5):
            self.monitor.update(self.workspace, self.channel, self.lease, {"event": {"record_id": str(n)}})
        snapshot = self.monitor.snapshot(self.workspace, self.channel, self.lease)
        self.assertEqual(len(snapshot["recent"]), RECENT_LIMIT)
        self.assertEqual(snapshot["recent"][0]["record_id"], str(RECENT_LIMIT + 4))
        snapshot["recent"].clear()
        self.assertEqual(len(self.monitor.snapshot(self.workspace, self.channel, self.lease)["recent"]), RECENT_LIMIT)

    def test_a_commit_preview_does_not_mark_live_uncommitted_results_as_saved(self):
        self.monitor.update(self.workspace, self.channel, self.lease, {"event": {"result": "checked"}})
        self.assertTrue(self.monitor.snapshot(self.workspace, self.channel, self.lease, committed=True)["recent"][0]["committed"])
        self.assertFalse(self.monitor.snapshot(self.workspace, self.channel, self.lease)["recent"][0]["committed"])

    def test_rollback_marks_only_pending_operations_and_never_turns_them_into_success(self):
        self.monitor.update(self.workspace, self.channel, self.lease, {"event": {"record_id": "1"}})
        self.monitor.committed(self.workspace, self.channel, self.lease)
        self.monitor.update(self.workspace, self.channel, self.lease, {"event": {"record_id": "2"}})
        self.monitor.rollback(self.workspace, self.channel, self.lease)
        recent = self.monitor.snapshot(self.workspace, self.channel, self.lease, committed=True)["recent"]
        self.assertFalse(recent[0]["committed"])
        self.assertTrue(recent[0]["rolled_back"])
        self.assertTrue(recent[1]["committed"])
