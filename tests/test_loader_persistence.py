"""Real SQL page/cursor/outbox rollback tests using the existing isolated DB fixture."""

import os
import threading
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from unittest.mock import Mock

from sqlalchemy import text
import test_message_sync as fixture
from center_messenger.loader.contracts import Operation, Page, Scope, SourceEvent
from center_messenger.loader.persistence import EVENT, FOLLOWUP, STREAM, LostClaim, PageStore
from center_messenger.loader.reader import PageReader, SourceFailure
from center_messenger.loader.contracts import RetryLater


@unittest.skipUnless(os.environ.get("CM_TEST_DATABASE_URL"), "isolated PostgreSQL URL required")
class LoaderPersistenceTests(unittest.TestCase):
    setUpClass = fixture.MessageSyncTests.__dict__["setUpClass"]
    tearDownClass = fixture.MessageSyncTests.__dict__["tearDownClass"]
    tearDown = fixture.MessageSyncTests.tearDown
    add_customer = fixture.MessageSyncTests.add_customer

    def setUp(self):
        fixture.MessageSyncTests.setUp(self)
        self.clock = [datetime(2026, 10, 26, tzinfo=UTC)]
        self.store = PageStore(self.accounts, clock=lambda: self.clock[0])
        self.scope = Scope(self.workspace, self.channel, "example", "incoming")
        self.store.configure(self.scope, generation=1)
        self.event = SourceEvent("messages", "42", "message", self.clock[0], {"text": "Synthetic"}, crm_id="7")

    def count(self, topic):
        return self.db.execute(text("SELECT count(*) FROM outbox_events WHERE topic=:t"), {"t": topic}).scalar_one()

    def test_event_cursor_and_followup_commit_atomically_and_mirrors_deduplicate(self):
        project = Mock()
        page = Page((self.event,), {"after": "42"}, False, ({"kind": "image", "key": "42:1", "source_id": "42"},))
        self.assertEqual(self.store.commit(self.store.claim(self.scope), page, project=project), 1)
        mirror = Scope(self.workspace, self.channel, "example", "other_branch")
        self.store.configure(mirror, generation=1)
        self.assertEqual(self.store.commit(self.store.claim(mirror), page, project=project), 0)
        self.assertEqual((self.count(EVENT), self.count(FOLLOWUP)), (1, 1))
        project.assert_called_once()
        self.clock[0] += timedelta(seconds=20)
        self.assertEqual(self.store.claim(self.scope).cursor, {"after": "42"})

    def test_projection_failure_rolls_back_events_jobs_and_cursor(self):
        claim = self.store.claim(self.scope)
        page = Page((self.event,), {"after": "42"}, False, ({"kind": "image", "key": "42:1"},))
        with self.assertRaises(RuntimeError):
            self.store.commit(claim, page, project=Mock(side_effect=RuntimeError("projection failed")))
        self.assertEqual((self.count(EVENT), self.count(FOLLOWUP)), (0, 0))
        self.assertEqual(self.store.commit(claim, page), 1)

    def test_changed_settings_reject_old_result_and_preserve_checkpoint(self):
        claim = self.store.claim(self.scope)
        self.store.configure(self.scope, generation=2)
        with self.assertRaises(LostClaim): self.store.commit(claim, Page((self.event,), {"p": 2}, False))
        self.assertEqual(self.count(EVENT), 0)
        self.assertEqual(self.store.claim(self.scope).cursor, {})

    def test_restart_and_expired_owner_do_not_publish_twice(self):
        first = self.store.claim(self.scope, seconds=5)
        restarted = PageStore(self.accounts, clock=lambda: self.clock[0])
        self.assertIsNone(restarted.claim(self.scope))
        self.clock[0] += timedelta(seconds=6)
        second = restarted.claim(self.scope)
        with self.assertRaises(LostClaim): self.store.commit(first, Page((self.event,), {"p": 1}, False))
        restarted.commit(second, Page((self.event,), {"p": 1}, False))
        self.assertEqual(self.count(EVENT), 1)

    def test_equal_source_ids_in_other_provider_connection_and_workspace_are_distinct(self):
        other_workspace = uuid4()
        self.db.execute(text("INSERT INTO workspaces(id,name) VALUES (:w,'Other')"), {"w": other_workspace})
        scopes = [self.scope, Scope(self.workspace, uuid4(), "example", "incoming"),
                  Scope(self.workspace, self.channel, "phone", "incoming"),
                  Scope(other_workspace, self.channel, "example", "incoming")]
        for scope in scopes:
            self.store.configure(scope, generation=1)
            self.store.commit(self.store.claim(scope), Page((self.event,), {"p": 1}, False))
        self.assertEqual(self.count(EVENT), 4)

    def test_runtime_role_cannot_read_another_workspace_stream(self):
        other_workspace = uuid4()
        self.db.execute(text("INSERT INTO workspaces(id,name) VALUES (:w,'Other')"), {"w": other_workspace})
        other = Scope(other_workspace, uuid4(), "example", "incoming")
        self.store.configure(other, generation=1)
        with self.db.begin_nested():
            self.db.execute(text(f'GRANT USAGE ON SCHEMA "{self.schema}" TO cm_app'))
            self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {"w": str(self.workspace)})
            self.db.execute(text("SET LOCAL ROLE cm_app"))
            self.assertTrue(self.store.status(self.scope)["configured"])
            self.assertFalse(self.store.status(other)["configured"])

    def test_auth_failure_pauses_only_its_stream_until_new_configuration(self):
        fetch = Mock(side_effect=SourceFailure("credentials_rejected", retryable=False))
        reader = PageReader(self.store, lambda: self.scope, fetch)
        self.assertTrue(reader(Operation(100, threading.Event(), lambda: 0)).worked)
        self.assertTrue(self.store.status(self.scope)["paused"])
        self.assertIsNone(self.store.claim(self.scope))
        self.store.configure(self.scope, generation=2)
        self.assertIsNotNone(self.store.claim(self.scope))

    def test_one_failed_account_does_not_delay_other_account_in_same_adapter(self):
        other = Scope(self.workspace, uuid4(), "example", "incoming")
        self.store.configure(other, generation=1)
        scopes = iter([self.scope, other])
        def fetch(scope, cursor, operation):
            if scope == self.scope:
                raise SourceFailure("rate_limited", delay=60)
            return Page((self.event,), {"p": 1}, False)
        reader = PageReader(self.store, lambda: next(scopes), fetch)
        reader(Operation(100, threading.Event(), lambda: 0))
        reader(Operation(100, threading.Event(), lambda: 0))
        self.assertEqual(self.count(EVENT), 1)
        self.assertEqual(self.store.status(self.scope)["error_code"], "rate_limited")
        self.assertIsNotNone(self.store.status(other)["last_success_at"])

    def test_bad_cursor_does_not_advance_and_can_retry(self):
        claim = self.store.claim(self.scope)
        with self.assertRaises(ValueError): self.store.commit(claim, Page((self.event,), {}, True))
        self.store.fail(claim, "invalid_cursor", delay=3)
        self.assertEqual(self.count(EVENT), 0)
        self.clock[0] += timedelta(seconds=4)
        self.assertEqual(self.store.claim(self.scope).cursor, {})

    def test_disabled_connection_rejects_running_read_and_preserves_event(self):
        self.store.commit(self.store.claim(self.scope), Page((self.event,), {"p": 1}, False))
        self.clock[0] += timedelta(seconds=20)
        claim = self.store.claim(self.scope)
        self.store.configure(self.scope, generation=2, enabled=False)
        with self.assertRaises(LostClaim): self.store.commit(claim, Page((), {"p": 2}, False))
        self.store.configure(self.scope, generation=3)
        self.assertEqual(self.store.claim(self.scope).cursor, {"p": 1})
        self.assertEqual(self.count(EVENT), 1)
