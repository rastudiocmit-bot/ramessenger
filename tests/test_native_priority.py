"""Browser arbitration, independent clocks and durable per-page handoff."""
import json
import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import text
import test_message_sync as messages
import test_native_session as sessions
from center_messenger.alfa_history import (advance_history, live_freshness, next_history_delay,
                                          plan_history, ready_history)
from center_messenger.alfa_max import AlfaMaxWorker
from center_messenger.alfa_max_browser import AlfaBrowserError
from center_messenger.alfa_session import CONTROL, control
from center_messenger.native_priority import choose, record_turn


class PriorityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, tzinfo=UTC)

    def test_three_sends_then_a_waiting_head_then_sends_again(self):
        ready = dict.fromkeys(['send', 'live', 'catchup', 'archive'], self.now)
        state = {}
        for _ in range(3):
            self.assertEqual(choose(ready, state, self.now), 'send')
            state = record_turn(state, 'send')
        self.assertEqual(choose(ready, state, self.now), 'live')
        self.assertEqual(choose(ready, record_turn(state, 'live'), self.now), 'send')

    def test_overdue_head_interrupts_after_one_send(self):
        ready = {'send': self.now, 'live': self.now - timedelta(seconds=16)}
        self.assertEqual(choose(ready, record_turn({}, 'send'), self.now), 'live')

    def test_aged_archive_gets_a_turn_under_continuous_foreground_or_catchup(self):
        ready = {'send': self.now, 'live': self.now, 'catchup': self.now,
                 'archive': self.now - timedelta(seconds=40)}
        state = {}
        for _ in range(12):
            state = record_turn(state, 'catchup')
        self.assertEqual(choose(ready, state, self.now), 'archive')
        self.assertEqual(choose(ready, record_turn(state, 'archive'), self.now), 'send')

    def test_archive_does_not_poll_recent_heads_again(self):
        cursor, requests = plan_history({}, [1], self.now)
        advance_history(cursor, requests, [{'messages': [], 'has_more': True} for _ in requests], self.now)
        cursor, requests = plan_history(cursor, [1], self.now + timedelta(seconds=2))
        self.assertEqual(requests, [(1, 'receive', 1), (1, 'sent', 1)])
        advance_history(cursor, requests, [{'messages': [], 'has_more': True} for _ in requests], self.now + timedelta(seconds=2))
        self.assertEqual(next_history_delay(cursor, self.now + timedelta(seconds=2)), 2)
        _, requests = plan_history(cursor, [1], self.now + timedelta(seconds=15))
        self.assertEqual(requests[:2], [(1, 'receive', 0), (1, 'sent', 0)])

    def test_live_failure_does_not_poll_zero_archive_cursor_in_a_hot_loop(self):
        cursor, requests = plan_history({}, [1], self.now)
        advance_history(cursor, requests, [{'error': 'crm_unavailable'} for _ in requests], self.now)
        self.assertEqual(next_history_delay(cursor, self.now), 60)
        self.assertFalse(plan_history(cursor, [1], self.now + timedelta(seconds=2))[1])

    def test_failed_archive_and_live_have_independent_catchup_schedule(self):
        cursor, _ = plan_history({'1:receive': 9, 'catchup:1:receive': 2}, [1], self.now)
        advance_history(cursor, [(1, 'receive', 9), (1, 'receive', 0)],
                        [{'error': 'crm_unavailable'}, {'error': 'crm_unavailable'}], self.now)
        _, requests = plan_history(cursor, [1], self.now + timedelta(seconds=3))
        self.assertIn((1, 'receive', 2), requests)
        self.assertNotIn((1, 'receive', 0), requests)
        self.assertNotIn((1, 'receive', 9), requests)

    def test_pause_does_not_hide_freshness_or_catchup(self):
        cursor, _ = plan_history({'archive_paused': True, '1:receive': 10, 'catchup:1:receive': 2,
                                 'live_checked:1:receive': (self.now - timedelta(seconds=60)).isoformat()}, [1], self.now)
        self.assertEqual(set(ready_history(cursor, self.now)), {'live', 'catchup'})
        self.assertEqual(live_freshness(cursor, self.now), {'interval_seconds': 15, 'unobserved_journals': 1,
                                                         'delayed_journals': 1, 'max_lag_seconds': 45})

    def test_adoption_preserves_pages_and_uses_last_success_not_last_attempt(self):
        original = {'1:receive': 23, 'catchup:1:receive': 4, 'live_checked:1:receive': self.now.isoformat()}
        cursor, requests = plan_history(original, [1], self.now + timedelta(seconds=2))
        self.assertEqual((cursor['1:receive'], cursor['catchup:1:receive']), (23, 4))
        self.assertNotIn((1, 'receive', 0), requests)
        self.assertEqual(cursor['due_live:1:receive'], (self.now + timedelta(seconds=15)).isoformat())


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'), 'isolated PostgreSQL URL required')
class PrioritySessionTests(unittest.TestCase):
    setUpClass = messages.MessageSyncTests.__dict__['setUpClass']
    tearDownClass = messages.MessageSyncTests.__dict__['tearDownClass']
    tearDown = messages.MessageSyncTests.tearDown
    setUp = sessions.NativeSessionTests.setUp
    add_customer = messages.MessageSyncTests.add_customer
    project = messages.MessageSyncTests.project
    row = messages.MessageSyncTests.row
    conversation = messages.MessageSyncTests.conversation
    setting = sessions.NativeSessionTests.setting
    queue_operator_reply = sessions.NativeSessionTests.queue_operator_reply

    def schedule(self, cursor=None):
        value, _ = plan_history(cursor or {}, [1, 2])
        self.db.execute(text('UPDATE alfa_max_settings SET history_cursor=CAST(:v AS jsonb) WHERE workspace_id=:w'),
                        {'w': self.workspace, 'v': json.dumps(value)})
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id) VALUES (:w,:c) "
                            "ON CONFLICT(workspace_id) DO UPDATE SET due_at='-infinity'"),
                        {'w': self.workspace, 'c': self.channel})
        return value

    def work(self, after_page=None):
        seen = []
        def stream(origin, session, requests, **options):
            results = []
            state = session
            for request in requests:
                if not options['before_read'](request):
                    result = {'skipped': True}
                else:
                    options['on_progress']()
                    seen.append(request)
                    state = {'cookies': [{'name': 'handoff', 'value': str(len(seen))}], 'origins': []}
                    result = {'messages': [], 'has_more': True}
                results.append(result)
                options['on_result'](request, result, state)
                if not result.get('skipped') and after_page:
                    after_page(request)
            return results, state
        self.service.browser.read_histories.side_effect = stream
        with patch('center_messenger.alfa_max.repair_batch', return_value=False):
            self.assertTrue(AlfaMaxWorker(self.service).run_next())
        return seen

    def test_sends_win_races_but_fourth_yields_to_history_without_logout(self):
        self.queue_operator_reply()
        self.schedule()
        for _ in range(3):
            with self.assertRaises(AlfaBrowserError) as raised:
                self.service.sessions.claim(self.workspace, self.channel, purpose='history')
            self.assertEqual(raised.exception.code, 'browser_priority_wait')
            operation = self.service.sessions.claim(self.workspace, self.channel, purpose='send')
            self.service.sessions.release(operation)
        with self.assertRaises(AlfaBrowserError) as raised:
            self.service.sessions.claim(self.workspace, self.channel, purpose='send')
        self.assertEqual(raised.exception.code, 'browser_priority_wait')
        operation = self.service.sessions.claim(self.workspace, self.channel, purpose='history')
        self.assertEqual(operation['kind'], 'live')
        self.assertFalse(control(self.setting()).get('needs_check'))
        self.service.sessions.release(operation)

    def test_priority_reservation_does_not_stall_if_preferred_worker_is_gone(self):
        self.queue_operator_reply()
        self.schedule()
        with self.assertRaises(AlfaBrowserError):
            self.service.sessions.claim(self.workspace, self.channel, purpose='history')
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,"
                             "'{session_control,dispatch,reservation,until}',to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE workspace_id=:w"),
                        {'w': self.workspace})
        operation = self.service.sessions.claim(self.workspace, self.channel, purpose='history')
        self.assertEqual(operation['kind'], 'live')
        self.assertTrue(self.service.sessions.can_read(self.db, operation, 'live'))
        self.service.sessions.release(operation)

    def test_release_wakes_busy_sender_and_wait_does_not_count_as_send_attempt(self):
        messaging, command = self.queue_operator_reply()
        operation = self.service.sessions.claim(self.workspace, self.channel)
        messaging.run_next('send', 'alfa_max_browser')
        row = self.db.execute(text('SELECT attempts,last_error_code FROM send_commands WHERE id=:c'),
                              {'c': command['command_id']}).mappings().one()
        self.assertEqual((row['attempts'], row['last_error_code']), (0, 'browser_busy'))
        started = time.monotonic()
        self.service.sessions.release(operation)
        self.assertTrue(self.db.execute(text('SELECT due_at<=clock_timestamp() FROM message_jobs WHERE subject_id=:c'),
                                        {'c': command['command_id']}).scalar_one())
        # Arbitration has no mandatory ten-second cooldown after the release.
        operation = self.service.sessions.claim(self.workspace, self.channel, purpose='send')
        self.assertLess(time.monotonic() - started, 1)
        self.service.sessions.release(operation)

    def test_new_send_interrupts_after_committed_page_and_uses_saved_cookies(self):
        self.schedule()
        queued = []
        def after_page(request):
            if not queued:
                queued.append(self.queue_operator_reply())
        self.assertEqual(self.work(after_page), [(1, 'receive', 0)])
        cursor = self.setting()['history_cursor']
        self.assertEqual(cursor['1:receive'], 1)
        self.assertEqual(cursor['live_cycle']['checked'], 1)
        self.assertNotIn('last_live_cycle', cursor)
        messaging, command = queued[0]
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(self.service.browser.send.call_args.args[1]['cookies'], [{'name': 'handoff', 'value': '1'}])
        cursor = self.setting()['history_cursor']
        cursor['due_archive:1:receive'] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.schedule(cursor)
        seen = self.work()
        self.assertNotIn((1, 'receive', 0), seen)
        self.assertIn((1, 'receive', 1), seen)
        cursor = self.setting()['history_cursor']
        self.assertEqual(cursor['last_live_cycle']['checked'], 4)
        self.assertNotIn('live_cycle', cursor)

    def test_idle_poll_does_not_rotate_cookies_or_open_browser(self):
        future = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
        cursor = {}
        for b in [1, 2]:
            for d in ['receive', 'sent']:
                cursor.update({f'complete:{b}:{d}': True, f'due_live:{b}:{d}': future})
        self.schedule(cursor)
        before = self.setting()['session_ciphertext']
        operation = self.service.sessions.claim(self.workspace, self.channel)
        self.assertEqual(self.work(), [])
        self.service.browser.read_histories.assert_not_called()
        self.assertEqual(self.setting()['session_ciphertext'], before)
        self.service.sessions.renew(self.db, operation)
        self.service.sessions.release(operation)

    def test_pausing_archive_mid_batch_keeps_fresh_heads_and_durable_position(self):
        self.schedule({'1:receive': 10})
        def pause(request):
            self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,'{archive_paused}','true'::jsonb) WHERE workspace_id=:w"),
                            {'w': self.workspace})
        seen = self.work(pause)
        self.assertEqual(seen, [(1, 'receive', 0), (1, 'sent', 0), (2, 'receive', 0), (2, 'sent', 0)])
        self.assertEqual(self.setting()['history_cursor']['1:receive'], 10)

    def test_disabled_sender_does_not_block_reader(self):
        self.queue_operator_reply()
        self.schedule()
        self.accounts.settings = SimpleNamespace(max_worker=False, crm_sync_worker=True)
        operation = self.service.sessions.claim(self.workspace, self.channel, purpose='history')
        self.assertEqual(operation['kind'], 'live')
        self.service.sessions.release(operation)

    def test_release_does_not_bypass_history_network_backoff(self):
        self.schedule()
        self.db.execute(text("UPDATE alfa_max_sync_queue SET due_at=clock_timestamp()+interval '60 seconds' WHERE workspace_id=:w"),
                        {'w': self.workspace})
        before = self.db.execute(text('SELECT due_at FROM alfa_max_sync_queue WHERE workspace_id=:w'), {'w': self.workspace}).scalar_one()
        operation = self.service.sessions.claim(self.workspace, self.channel)
        self.service.sessions.release(operation)
        self.assertEqual(self.db.execute(text('SELECT due_at FROM alfa_max_sync_queue WHERE workspace_id=:w'),
                                         {'w': self.workspace}).scalar_one(), before)

    def test_native_postgres_racing_claims_grant_only_the_ready_sender(self):
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import contextmanager
        from threading import Barrier
        if 'wasm' in self.db.execute(text('SELECT version()')).scalar_one().lower():
            self.skipTest('native PostgreSQL required for concurrent priority arbitration')
        self.queue_operator_reply()
        self.schedule()
        self.transaction.commit()
        self.transaction = self.db.begin()
        barrier = Barrier(3)
        @contextmanager
        def transaction(*args, **kwargs):
            with self.engine.begin() as db:
                db.execute(text(f'SET search_path TO "{self.schema}",public'))
                db.execute(text("SET LOCAL statement_timeout='3000ms'"))
                yield db
        def claim(kind):
            barrier.wait(3)
            try:
                return self.service.sessions.claim(self.workspace, self.channel, purpose=kind)
            except AlfaBrowserError as error:
                return error.code
        try:
            with patch.object(self.accounts, 'transaction', transaction), ThreadPoolExecutor(max_workers=2) as pool:
                sending = pool.submit(claim, 'send')
                reading = pool.submit(claim, 'history')
                barrier.wait(3)
                sent, read = sending.result(5), reading.result(5)
                self.assertIsInstance(sent, dict)
                self.assertIn(read, {'browser_busy', 'browser_priority_wait'})
                self.service.sessions.release(sent)
        finally:
            self.transaction.rollback()
            try:
                with transaction() as db:
                    # The class owns an isolated schema and drops it at teardown.
                    # Remove only committed routing jobs so later fixture tests
                    # cannot claim this workspace; domain FKs need no cascade.
                    db.execute(text('DELETE FROM message_jobs WHERE workspace_id=:w'), {'w': self.workspace})
                    db.execute(text('DELETE FROM alfa_max_sync_queue WHERE workspace_id=:w'), {'w': self.workspace})
            finally:
                self.transaction = self.db.begin()
