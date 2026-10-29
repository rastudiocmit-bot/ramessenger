from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from sqlalchemy.exc import OperationalError

from center_messenger.auth_security import AuthError
from center_messenger.chat_card_view import CardViews, compact_card
from center_messenger import chat_events
from center_messenger.chat_api import ChatReader
from center_messenger.database import make_engine


class StartupTests(unittest.TestCase):
    def reader(self):
        reader = ChatReader.__new__(ChatReader)
        reader.context = Mock(return_value={"channels": []})
        reader.listing = Mock(return_value={"conversations": [{"id": str(uuid4())}]})
        reader.history = Mock(return_value={"messages": []})
        return reader

    def test_a_failed_timeline_preserves_the_authorized_list_and_retries_independently(self):
        reader = self.reader()
        reader.history.side_effect = OperationalError("private SQL", {}, RuntimeError("private detail"))
        with self.assertLogs("center_messenger.chat_api", level="ERROR") as logs:
            result = reader.bootstrap(None, uuid4(), branch=1)
        self.assertEqual(result["listing"], reader.listing.return_value)
        self.assertIsNone(result["history"])
        self.assertTrue(result["history_pending"])
        self.assertNotIn("private", " ".join(logs.output))
        reader.listing.assert_called_once()
        reader.history.assert_called_once()

    def test_history_authorization_failures_are_not_downgraded_to_pending(self):
        for status in (401, 403, 503):
            with self.subTest(status=status):
                reader = self.reader()
                reader.history.side_effect = AuthError(status, "Unavailable")
                with self.assertRaises(AuthError):
                    reader.bootstrap(None, uuid4())

    def test_failed_context_never_falls_through_to_a_private_list(self):
        reader = self.reader()
        reader.context.side_effect = AuthError(403, "No access")
        with self.assertRaises(AuthError):
            reader.bootstrap(None, uuid4())
        reader.listing.assert_not_called()

    def test_short_reads_disable_jit_and_have_bounded_capacity_for_workers_and_requests(self):
        from sqlalchemy.pool import QueuePool
        settings = SimpleNamespace(database_url=lambda: "postgresql+psycopg://local/test",
                                   db_connect_timeout=3, db_statement_timeout_ms=2500)
        with patch("center_messenger.database.create_engine") as factory:
            make_engine(settings)
        options = factory.call_args.kwargs
        self.assertIn("-c jit=off", options["connect_args"]["options"])
        self.assertIn("statement_timeout=2500", options["connect_args"]["options"])
        self.assertEqual(options["pool_timeout"], 3)
        pool = QueuePool(lambda: Mock(), pool_size=options["pool_size"],
                         max_overflow=options["max_overflow"], timeout=.01)
        connections = []
        try:
            # Six server workers cannot exhaust the entire foreground capacity.
            for _ in range(8):
                connections.append(pool.connect())
            self.assertLessEqual(options["pool_size"] + options["max_overflow"], 10)
        finally:
            for connection in connections:
                connection.close()
            pool.dispose()


def family():
    return {"id": "parent", "name": "Synthetic family", "cache": {"ready": True},
            "students": [{"id": "child", "balance": "123.45", "paid_lesson_count": 8,
                "subscriptions": [{"id": "tariff", "balance": "100.00"}],
                "groups": [{"previous_lesson": {"date": "2026-10-25"}, "next_lesson": {"date": "2026-10-27"}}],
                "cards": [{"id": "1", "lessons": [{"date": "2025-10-25"}] * 1000,
                           "subscriptions": [{"id": "tariff"}], "branch_name": "Branch"}]}]}


class CardViewTests(unittest.TestCase):
    def test_compact_card_preserves_displayed_facts_without_mutating_yearly_storage(self):
        original = family()
        result = compact_card(original)
        student = result["students"][0]
        self.assertEqual((student["balance"], student["paid_lesson_count"]), ("123.45", 8))
        self.assertEqual(student["subscriptions"], original["students"][0]["subscriptions"])
        self.assertEqual(student["groups"], original["students"][0]["groups"])
        self.assertNotIn("lessons", student["cards"][0])
        self.assertNotIn("subscriptions", student["cards"][0])
        self.assertTrue(student["cards"][0]["lessons_ready"])
        self.assertEqual(len(original["students"][0]["cards"][0]["lessons"]), 1000)

    def test_compacting_a_published_compact_snapshot_preserves_readiness(self):
        value = compact_card(compact_card(family()))
        self.assertTrue(value['students'][0]['cards'][0]['lessons_ready'])
        self.assertTrue(value['students'][0]['cards'][0]['subscriptions_ready'])

    def test_missing_facts_remain_missing_instead_of_claiming_an_empty_history(self):
        value = family()
        del value["students"][0]["cards"][0]["lessons"]
        del value["students"][0]["cards"][0]["subscriptions"]
        card = compact_card(value)["students"][0]["cards"][0]
        self.assertFalse(card["lessons_ready"])
        self.assertFalse(card["subscriptions_ready"])

    def test_versions_are_content_based_and_never_reuse_another_workspace_or_actor(self):
        cache = CardViews()
        key = ("workspace-1", "actor-1", "conversation", "revision-1")
        value = cache.put(key, family())
        self.assertEqual(cache.reply(value, value["version"]), {"not_modified": True, "version": value["version"]})
        self.assertIsNone(cache.get(("workspace-2", "actor-1", "conversation", "revision-1")))
        self.assertIsNone(cache.get(("workspace-1", "actor-2", "conversation", "revision-1")))
        self.assertIsNone(cache.get(("workspace-1", "actor-1", "conversation", "revision-2")))
        changed = family(); changed["students"][0]["balance"] = "200.00"
        self.assertNotEqual(cache.put(key, changed)["version"], value["version"])

    def test_expiry_entry_limit_and_byte_budget_bound_the_private_cache(self):
        cache = CardViews(limit=2, byte_limit=4096, ttl=10)
        with patch("center_messenger.chat_card_view.time.monotonic", return_value=0):
            for number in range(3):
                cache.put((number,), family())
            self.assertIsNone(cache.get((0,)))
            self.assertEqual(len(cache.entries), 2)
            self.assertLessEqual(cache.bytes, 4096)
        with patch("center_messenger.chat_card_view.time.monotonic", return_value=11):
            self.assertIsNone(cache.get((2,)))
        tiny = CardViews(byte_limit=10)
        self.assertIn("version", tiny.put((1,), family()))
        self.assertEqual(tiny.entries, {})


class EventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actor = SimpleNamespace(session_id=uuid4())
        self.workspace = uuid4()
        self.initial = {"version": "1:2026-10-26", "card_version": "1"}
        self.reader = SimpleNamespace(event_revision=Mock(return_value=self.initial))
        self.request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        self.slot_patch = patch.object(chat_events, "slots", chat_events.StreamSlots(limit=1))
        self.slot_patch.start(); self.addCleanup(self.slot_patch.stop)

    def stream(self, **kwargs):
        return chat_events.events(self.request, self.reader, self.actor, self.workspace, self.initial, interval=0, **kwargs)

    async def test_changes_and_heartbeats_carry_only_revision_hints_and_release_the_slot(self):
        stream = self.stream(heartbeat=0)
        self.assertIn("event: ready", await anext(stream))
        self.assertIn("event: pulse", await anext(stream))
        self.reader.event_revision.return_value = {"version": "2:2026-10-26", "card_version": "1"}
        change = await anext(stream)
        self.assertIn("event: change", change)
        self.assertNotIn("customer", change)
        await stream.aclose()
        self.assertEqual(chat_events.slots.active, {})

    async def test_revoked_session_or_section_access_stops_an_existing_stream(self):
        stream = self.stream()
        await anext(stream)
        self.reader.event_revision.side_effect = AuthError(403, "No message access")
        self.assertEqual(await anext(stream), chat_events.frame("access_lost", {}))
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertEqual(chat_events.slots.active, {})

    async def test_database_outage_requests_fallback_and_does_not_expose_driver_text(self):
        stream = self.stream()
        await anext(stream)
        self.reader.event_revision.side_effect = RuntimeError("private database details")
        self.assertEqual(await anext(stream), chat_events.frame("retrying", {}))
        await stream.aclose()
        self.assertEqual(chat_events.slots.active, {})

    async def test_parallel_tab_limit_is_bounded_and_disconnect_releases_capacity(self):
        first = self.stream(); await anext(first)
        second = self.stream()
        self.assertIn("event: retrying", await anext(second))
        await second.aclose(); await first.aclose()
        third = self.stream(); self.assertIn("event: ready", await anext(third))
        await third.aclose()

    async def test_authorization_fails_before_a_streaming_response_is_opened(self):
        self.reader.event_revision.side_effect = AuthError(401, "Expired")
        with self.assertRaises(AuthError):
            await chat_events.response(self.request, self.reader, self.actor, self.workspace)
        self.assertEqual(chat_events.slots.active, {})


if __name__ == "__main__":
    unittest.main()
