from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from center_messenger.auth_security import AuthError
from center_messenger.chat_card_view import CardViews, compact_card
from center_messenger import chat_events


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
