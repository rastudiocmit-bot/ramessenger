"""Provider contracts, bounded scheduling and isolation; no network or secrets."""

import sys
import asyncio
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))
from center_messenger.loader import Capability as C, Manifest, Operation, Registry, Scheduler, Step, StepResult
from center_messenger.loader.contracts import OperationCancelled, Page, RetryLater, SourceEvent
from center_messenger.loader.bootstrap import build_loader


class LoaderTests(unittest.TestCase):
    def registry(self, callback, *, provider="example", capability=C.RECEIVE, **options):
        registry = Registry()
        registry.register(Manifest(provider, frozenset({capability})),
                          lambda: [Step("poll", capability, callback, **options)])
        return registry

    def test_bad_optional_provider_cannot_prevent_healthy_provider(self):
        called = []
        registry = self.registry(lambda op: called.append("ok") or True)
        registry.register(Manifest("incompatible", frozenset({C.CALLS}), version=2), lambda: [])
        registry.register(Manifest("missing_dependency", frozenset({C.CALLS})), Mock(side_effect=ImportError("secret")))
        scheduler = Scheduler(registry)
        self.assertTrue(scheduler.tick("messages"))
        self.assertEqual(called, ["ok"])
        self.assertEqual(scheduler.status()["registration_errors"], {
            "incompatible": "incompatible_contract", "missing_dependency": "adapter_initialization_failed"})
        self.assertNotIn("secret", repr(scheduler.status()))

    def test_duplicate_registration_does_not_replace_existing_adapter(self):
        original = Mock(return_value=True)
        registry = self.registry(original)
        self.assertFalse(registry.register(Manifest("example", frozenset({C.CALLS})), lambda: []))
        Scheduler(registry).tick("messages")
        original.assert_called_once()

    def test_undeclared_capability_is_not_enabled(self):
        registry = Registry()
        registry.register(Manifest("example", frozenset({C.RECEIVE})),
                          lambda: [Step("send", C.SEND, Mock())])
        self.assertEqual(registry.resolve(), {})
        self.assertEqual(registry.errors["example"], "adapter_initialization_failed")

    def test_failure_backoff_is_local_and_healthy_channels_continue(self):
        now = [10.0]
        broken, healthy = Mock(side_effect=RuntimeError("credentials secret")), Mock(return_value=True)
        registry = self.registry(broken, provider="broken")
        registry.register(Manifest("healthy", frozenset({C.CALLS})), lambda: [Step("poll", C.CALLS, healthy)])
        scheduler = Scheduler(registry, clock=lambda: now[0])
        with self.assertLogs("center_messenger.loader.scheduler", level="WARNING") as captured:
            scheduler.tick("messages")
        self.assertNotIn("secret", "".join(captured.output))
        scheduler.tick("messages")
        now[0] += .2
        scheduler.tick("messages")
        self.assertEqual(broken.call_count, 1)
        self.assertEqual(healthy.call_count, 2)
        now[0] += 3
        scheduler.tick("messages")
        scheduler.tick("messages")
        self.assertEqual(broken.call_count, 2)
        state = next(s for s in scheduler.status()["steps"] if s["id"] == "broken.poll")
        self.assertEqual(state["next_in_seconds"], 6)

    def test_same_step_never_runs_concurrently_and_media_cannot_occupy_messages(self):
        entered, release = threading.Event(), threading.Event()
        called = Mock(return_value=True)
        def media(op):
            entered.set()
            release.wait(2)
            return True
        registry = self.registry(media, provider="recordings", capability=C.RECORDINGS, lane="media")
        registry.register(Manifest("messages", frozenset({C.RECEIVE})), lambda: [Step("poll", C.RECEIVE, called)])
        scheduler = Scheduler(registry)
        thread = threading.Thread(target=scheduler.tick, args=("media",))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(scheduler.tick("media"))
            self.assertTrue(scheduler.tick("messages"))
            called.assert_called_once()
        finally:
            release.set(); thread.join(2)

    def test_more_adapters_share_fixed_workers_and_fair_turns(self):
        registry, visited, now = Registry(), [], [1.0]
        for index in range(20):
            registry.register(Manifest(f"source{index}", frozenset({C.RECEIVE})),
                lambda n=index: [Step("poll", C.RECEIVE, lambda op: visited.append(n) or True)])
        scheduler = Scheduler(registry, clock=lambda: now[0])
        for _ in range(20):
            scheduler.tick("messages"); now[0] += 1
        self.assertEqual(len(set(visited)), 20)
        self.assertEqual(scheduler.limits["messages"], 4)

    def test_operation_checks_deadline_and_cooperative_stop(self):
        stop = threading.Event()
        operation = Operation(20, stop, lambda: 12)
        self.assertEqual(operation.remaining(5), 5)
        stop.set()
        with self.assertRaises(OperationCancelled): operation.remaining()
        with self.assertRaises(RetryLater): Operation(10, threading.Event(), lambda: 11).remaining()

    def test_retry_after_and_shutdown_do_not_immediately_repeat_work(self):
        called = Mock(side_effect=RetryLater("rate_limited", 60))
        scheduler = Scheduler(self.registry(called), clock=lambda: 10)
        scheduler.tick("messages")
        self.assertFalse(scheduler.tick("messages"))
        self.assertEqual(scheduler.status()["steps"][0]["next_in_seconds"], 60)
        scheduler.stop()
        self.assertFalse(scheduler.tick("messages"))

    def test_invalid_page_and_naive_timestamp_are_rejected(self):
        with self.assertRaises(ValueError): SourceEvent("messages", "1", "message", datetime(2026, 1, 1), {})
        event = SourceEvent("messages", "1", "message", datetime(2026, 1, 1, tzinfo=UTC), {})
        with self.assertRaises(ValueError): Page(tuple(event for _ in range(101)), {}, False).validate()

    def test_readme_example_fetches_messages_and_calls_with_stable_cursor(self):
        from center_messenger.loader.adapters.example import fetch, MANIFEST
        self.assertEqual(MANIFEST.version, 1)
        for stream, kind in (("incoming", "message"), ("calls", "call")):
            scope = SimpleNamespace(stream=stream)
            operation = Operation(60, threading.Event(), lambda: 0)
            first = fetch(scope, {}, operation)
            first.validate()
            self.assertEqual(first.events[0].kind, kind)
            self.assertEqual(fetch(scope, first.cursor, operation).events, ())

    def test_builtin_bridge_uses_existing_send_queues_without_starting_old_workers(self):
        messaging = Mock()
        messaging.run_next.return_value = True
        services = SimpleNamespace(messaging=messaging, alfa_sync=None, alfa_max=None, alfa=None, accounts=None)
        scheduler = build_loader(services, SimpleNamespace(crm_sync_worker=False, max_worker=True))
        for _ in range(3): scheduler.tick("messages")
        self.assertEqual({call.args for call in messaging.run_next.call_args_list}, {
            ("send", "alfa_max_browser"), ("send", "max_bot"), ("event", "max_bot")})
        self.assertEqual(len(scheduler.steps), 3)

    def test_application_stops_shared_loader_even_when_lifespan_is_interrupted(self):
        from center_messenger.main import create_app
        from center_messenger.config import Settings
        loader = Mock()
        loader.stop.return_value = True
        app = create_app(Settings(crm_sync_worker=False, max_worker=False), engine_factory=lambda config: None)
        async def run():
            with patch("center_messenger.main.build_loader", return_value=loader):
                with self.assertRaises(RuntimeError):
                    async with app.router.lifespan_context(app):
                        raise RuntimeError("interrupted")
        asyncio.run(run())
        loader.start.assert_called_once()
        loader.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
