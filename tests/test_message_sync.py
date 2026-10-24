"""Regression tests for browser-polled CRM history and chat reads.

Set CM_TEST_DATABASE_URL to an isolated PostgreSQL database. Each test rolls
back its data; the suite creates and removes its own randomly named schema.
"""

import importlib.util
import json
import os
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/backend/src"))

from center_messenger.alfa_max import AlfaMaxConnections, AlfaMaxWorker
from center_messenger.alfa_history import PROJECTION_VERSION, RECHECK_INTERVAL, advance_history, history_coverage, history_pending, plan_history, retry_history, verify_saved_pages
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser, _message_time, parse_history_html
from bs4 import BeautifulSoup
from center_messenger.chat_api import ChatReader, SendInput
from center_messenger.chat_store import changed, project_event, refresh_unread
from center_messenger.crm_api import DirectoryReader
from center_messenger.auth_security import AuthError
from center_messenger.database import log_database_error
from center_messenger.history_audit import compare_source_history
from center_messenger.alfa_repair import repair_batch
from sqlalchemy.exc import OperationalError


class HistoryCursorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, tzinfo=UTC)

    def test_completed_journals_are_rechecked_for_late_old_rows(self):
        original = {"projection_version": PROJECTION_VERSION, "1:receive": 0,
                    "complete:1:receive": True, "finished:1:receive": self.now.isoformat()}
        cursor, requests = plan_history(original, [1], self.now + RECHECK_INTERVAL)
        self.assertFalse(cursor["complete:1:receive"])
        advance_history(cursor, requests, [{"messages": [], "has_more": True} for _ in requests], self.now)
        self.assertEqual(cursor["1:receive"], 1)

    def test_a_new_branch_is_incomplete_until_both_journals_are_read(self):
        cursor, requests = plan_history({}, [1], self.now)
        advance_history(cursor, requests, [{"messages": [], "has_more": False} for _ in requests], self.now)
        self.assertFalse(history_pending(cursor))
        cursor, _ = plan_history(cursor, [1, 2], self.now)
        self.assertTrue(history_pending(cursor))

    def test_catchup_has_its_own_durable_boundary_and_finishes_at_overlap(self):
        original = {"projection_version": PROJECTION_VERSION, "1:receive": 9,
                    "catchup:1:receive": 2, "until:1:receive": "10", "head:1:receive": "30"}
        cursor, requests = plan_history(original, [1], self.now)
        def page(request):
            return {"messages": [{"record_id": "40" if request[2] == 0 else "10"}], "has_more": True}
        advance_history(cursor, requests, [page(request) for request in requests], self.now)
        self.assertEqual(cursor["1:receive"], 10)
        self.assertEqual(cursor["catchup:1:receive"], 1)
        self.assertEqual(cursor["until:1:receive"], "30")

    def test_source_verification_identifies_missing_and_duplicate_ids_without_messages(self):
        browser = Mock()
        pages = [{"messages": [{"record_id": n} for n in values], "total": 6,
                  "row_count": 2, "has_more": i < 2}
                 for i, values in enumerate((("6", "5"), ("5", "4"), ("2", "1")))]
        browser.read_histories.side_effect = lambda origin, state, requests: ([pages[r[2]] for r in requests], state)
        report = compare_source_history(browser, "https://example.test", {}, 4, "sent", {"1", "2", "3", "4", "5"})
        self.assertEqual(report["missing_ids"], ["6"])
        self.assertEqual(report["duplicate_source_ids"], ["5"])
        self.assertEqual(report["saved_not_observed_ids"], ["3"])
        self.assertEqual(report["distinct_source_rows"], 5)
        self.assertEqual(report["observed_rows"], 6)
        self.assertEqual(report["pages_checked"], 3)

    def test_database_error_diagnostics_never_log_sql_parameters_or_driver_text(self):
        logger = Mock()
        original = RuntimeError("secret message contents")
        original.sqlstate = "57014"
        error = OperationalError("SELECT 'secret query'", {"token": "secret token"}, original)
        log_database_error(logger, error, operation="GET /api/workspaces/{workspace_id}/chat-updates")
        args = logger.error.call_args.args
        output = args[0] % args[1:]
        self.assertIn("57014", output)
        self.assertIn("OperationalError", output)
        self.assertNotIn("secret", output)


class PageTests(unittest.TestCase):
    def test_last_older_page_finishes_backfill(self):
        cursor, requests = plan_history({"projection_version": PROJECTION_VERSION, "1:receive": 5}, [1])
        advance_history(cursor, requests, [
            {"messages": [], "has_more": request == (1, "receive", 0)} for request in requests
        ])
        self.assertEqual(cursor["1:receive"], 0)
        self.assertTrue(cursor["complete:1:receive"])

    def test_browser_parser_preserves_stable_identity_and_exact_time(self):
        html = '''<table class="crm-table"><tbody>
          <tr data-key="9007199254740993"><td></td><td>
            <a href="/company/2/customer/view?id=7">Клиент</a></td>
            <td>Клиент</td><td>Привет</td>
            <td><time datetime="2026-10-24T03:10:11.123456+03:00">03:10</time></td>
          </tr></tbody></table>'''
        result = parse_history_html(html, origin="https://test.s20.online", branch=2,
                                    direction="receive")
        self.assertEqual(result["messages"][0]["record_id"], "9007199254740993")
        self.assertEqual(result["messages"][0]["occurred_at"],
                         datetime(2026, 10, 24, 0, 10, 11, 123456, tzinfo=UTC))

    def test_browser_does_not_silently_advance_past_an_unparsed_row(self):
        html = '<table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td></td><td>Hello</td><td>unknown</td></tr></tbody></table>'
        with self.assertRaises(AlfaBrowserError) as error:
            parse_history_html(html, origin="https://test.s20.online", branch=1, direction="receive")
        self.assertEqual(error.exception.code, "invalid_response")

    def test_browser_rejects_a_nonempty_journal_with_unrecognized_rows(self):
        html = '<div class="summary">1–1 из 1</div><table class="crm-table"><tbody><tr><td>Hello</td></tr></tbody></table>'
        with self.assertRaises(AlfaBrowserError):
            parse_history_html(html, origin="https://test.s20.online", branch=1, direction="receive")

    def test_empty_source_message_is_retained_with_its_identity(self):
        html = '<table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td></td><td></td><td>24.10.2026 03:10</td></tr></tbody></table>'
        result = parse_history_html(html, origin="https://test.s20.online", branch=1, direction="receive")
        self.assertEqual([m["record_id"] for m in result["messages"]], ["1"])

    def test_source_counts_cannot_hide_a_skipped_row(self):
        html = '<div class="summary">1–2 из 2</div><table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td></td><td>Text</td><td>24.10.2026 03:10</td></tr></tbody></table>'
        with self.assertRaises(AlfaBrowserError):
            parse_history_html(html, origin="https://test.s20.online", branch=1, direction="receive")

    def test_empty_summary_is_a_verified_zero_and_message_line_breaks_are_preserved(self):
        result = parse_history_html('<div class="summary">0–0 из 0</div><table class="crm-table"></table>',
                                    origin="https://test.s20.online", branch=1, direction="receive")
        self.assertEqual(result["total"], 0)
        body = "a" * 21000
        html = f'<table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td></td><td><p>First<br>Second</p><p>{body}</p></td><td>24.10.2026 03:10</td></tr></tbody></table>'
        result = parse_history_html(html, origin="https://test.s20.online", branch=1, direction="receive")
        self.assertEqual(result["messages"][0]["text"], "First\nSecond\n" + body)

    def test_browser_can_keep_healthy_journals_when_one_page_is_invalid(self):
        manager, browser, context, page = [Mock() for _ in range(4)]
        page.goto.side_effect = [
            SimpleNamespace(status=200, text=lambda: '<html>Unknown journal</html>'),
            SimpleNamespace(status=200, text=lambda: '<table class="crm-table"></table>'),
        ]
        context.storage_state.return_value = {}
        with patch.object(AlfaMaxBrowser, "_open", return_value=(manager, browser, context, page)), \
             patch.object(AlfaMaxBrowser, "_check_origin"):
            results, _ = AlfaMaxBrowser().read_histories(
                "https://test.s20.online", {}, [(1, "receive", 0), (2, "sent", 0)], continue_on_error=True)
        self.assertEqual(results[0]["error"], "invalid_response")
        self.assertEqual(results[1]["messages"], [])

    def test_failed_browser_start_releases_its_playwright_manager(self):
        manager = Mock()
        manager.chromium.launch.side_effect = RuntimeError("cannot launch")
        factory = Mock()
        factory.return_value.start.return_value = manager
        with patch.object(AlfaMaxBrowser, "_playwright", return_value=factory), \
             patch("center_messenger.alfa_max_browser._public_dns"):
            with self.assertRaises(AlfaBrowserError):
                AlfaMaxBrowser._open("https://test.s20.online")
        manager.stop.assert_called_once()

    def test_visible_crm_date_can_precede_the_time(self):
        cell = BeautifulSoup('<td>24.10.2026 03:10:11,123456</td>', 'html.parser').td
        self.assertEqual(_message_time(cell), datetime(2026, 10, 24, 0, 10, 11, 123456, tzinfo=UTC))

    def test_browser_renews_the_lease_before_each_history_navigation(self):
        manager, browser, context, page, progress = [Mock() for _ in range(5)]
        page.goto.return_value = SimpleNamespace(status=200, text=lambda: '<table class="crm-table"></table>')
        context.storage_state.return_value = {"cookies": [{"name": "test"}]}
        with patch.object(AlfaMaxBrowser, "_open", return_value=(manager, browser, context, page)), \
             patch.object(AlfaMaxBrowser, "_check_origin"):
            results, state = AlfaMaxBrowser().read_histories(
                "https://test.s20.online", {}, [(1, "receive", 0), (1, "sent", 0)], on_progress=progress)
        self.assertEqual(progress.call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(state, context.storage_state.return_value)
        context.close.assert_called_once()


@unittest.skipUnless(os.environ.get("CM_TEST_DATABASE_URL"), "isolated PostgreSQL URL required")
class MessageSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["CM_TEST_DATABASE_URL"].replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        cls.engine = create_engine(url)
        cls.db = cls.engine.connect()
        cls.schema = "message_test_" + uuid4().hex
        cls.db.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='cm_app') "
                            "THEN CREATE ROLE cm_app; END IF; END $$"))
        cls.db.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.db.execute(text(f'SET search_path TO "{cls.schema}",public'))
        context = MigrationContext.configure(cls.db)
        context._ensure_version_table()
        with Operations.context(context):
            for path in sorted((ROOT / "app/backend/migrations/versions").glob("*.py")):
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.rollback()
        cls.db.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.db.commit()
        cls.db.close()
        cls.engine.dispose()

    def setUp(self):
        self.transaction = self.db.begin()
        self.workspace, self.channel, self.crm, self.run, self.contact, self.user = [uuid4() for _ in range(6)]
        self.at = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
        self.db.execute(text("INSERT INTO workspaces(id,name) VALUES (:id,'Test')"), {"id": self.workspace})
        self.db.execute(text("INSERT INTO auth_users(id,email,display_name,password_hash) "
                             "VALUES (:id,:email,'Operator','unused')"),
                        {"id": self.user, "email": f"{self.user}@example.test"})
        for connection, kind in [(self.channel, "alfa_max_browser"), (self.crm, "alfa_crm")]:
            self.db.execute(text("INSERT INTO connections(workspace_id,id,kind,name,status) "
                                 "VALUES (:w,:id,:kind,'Test','active')"),
                            {"w": self.workspace, "id": connection, "kind": kind})
        self.db.execute(text("INSERT INTO alfa_settings(workspace_id,connection_id,origin,api_email,secret_ciphertext) "
                             "VALUES (:w,:c,'https://test.s20.online','test@example.test','test')"),
                        {"w": self.workspace, "c": self.crm})
        self.db.execute(text("INSERT INTO alfa_max_settings(workspace_id,connection_id,crm_connection_id,email,state,session_ciphertext) "
                             "VALUES (:w,:c,:crm,'test@example.test','active','test')"),
                        {"w": self.workspace, "c": self.channel, "crm": self.crm})
        self.db.execute(text("INSERT INTO alfa_sync_runs(workspace_id,id,connection_id,settings_version,requested_by,state) "
                             "VALUES (:w,:r,:c,1,:u,'succeeded')"),
                        {"w": self.workspace, "r": self.run, "c": self.crm, "u": self.user})
        for branch in (1, 2):
            self.db.execute(text("INSERT INTO alfa_branches(workspace_id,connection_id,external_id,name) "
                                 "VALUES (:w,:c,:b,:name)"),
                            {"w": self.workspace, "c": self.crm, "b": branch, "name": f"Branch {branch}"})
        self.add_customer(self.contact, 1, 7)
        self.snapshot = {"run": self.run, "connection_id": self.crm}
        published = patch.object(DirectoryReader, "_published", return_value=self.snapshot)
        self.published = published.start()
        self.addCleanup(published.stop)

        @contextmanager
        def transaction(*args, **kwargs):
            with self.db.begin_nested():
                yield self.db

        self.accounts = SimpleNamespace(transaction=transaction, throttle=Mock())
        self.service = AlfaMaxConnections(SimpleNamespace(accounts=self.accounts, vault=Mock(), _transaction=transaction), browser=Mock())
        maximum = SimpleNamespace(chat_transaction=transaction, accounts=self.accounts)
        self.reader = ChatReader(SimpleNamespace(max=maximum, alfa_max=self.service))

    def tearDown(self):
        self.transaction.rollback()

    def add_customer(self, contact, branch, customer):
        self.db.execute(text("INSERT INTO contacts(workspace_id,id,display_name) VALUES (:w,:id,'Parent') "
                             "ON CONFLICT DO NOTHING"), {"w": self.workspace, "id": contact})
        self.db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,status,contact_id) "
                             "VALUES (:w,:r,:b,:id,'Child','client',:contact)"),
                        {"w": self.workspace, "r": self.run, "b": branch, "id": customer, "contact": contact})
        self.db.execute(text("INSERT INTO alfa_directory(workspace_id,run_id,contact_id,display_name,phones,emails,search_text) "
                             "VALUES (:w,:r,:id,'Parent','[]','[]','parent') ON CONFLICT DO NOTHING"),
                        {"w": self.workspace, "r": self.run, "id": contact})

    def row(self, record="1", *, direction="in", branch=1, customer=7, actor=None, body="Hello", at=None):
        return {"record_id": record, "crm_order": int(record), "direction": direction,
                "customers": [{"branch_id": branch, "customer_id": customer, "name": "Child", "status": "client"}],
                "attachments": [], "actor_name": "Operator" if direction == "out" else "Parent",
                "actor_type": actor or ("human" if direction == "out" else "contact"),
                "actor_id": "42" if direction == "out" else None, "failed": False,
                "text": body, "occurred_at": at or self.at}

    def project(self, *rows, branch=1):
        self.service._project_page(self.db, self.workspace, self.channel, self.crm, branch, rows)

    def messages(self):
        return self.db.execute(text("SELECT * FROM messages WHERE workspace_id=:w "
                                    "AND payload->>'superseded_by' IS NULL ORDER BY occurred_at,id"),
                               {"w": self.workspace}).mappings().all()

    def conversation(self):
        return self.db.execute(text("SELECT * FROM conversations WHERE workspace_id=:w "
                                    "AND last_inbound_at IS NOT NULL"), {"w": self.workspace}).mappings().one()

    def local_reply(self, conversation, *, seconds=5):
        command, message = uuid4(), uuid4()
        self.db.execute(text("INSERT INTO send_commands(workspace_id,id,conversation_id,client_request_id,payload,payload_hash,state) "
                             "VALUES (:w,:id,:c,:request,CAST(:payload AS jsonb),:hash,'accepted')"),
                        {"w": self.workspace, "id": command, "c": conversation, "request": str(uuid4()),
                         "payload": json.dumps({"text": "Reply", "conversation_id": str(conversation)}), "hash": "0" * 64})
        self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,direction,actor_type,text,payload,occurred_at,send_command_id) "
                             "VALUES (:w,:id,:c,'out','human','Reply',CAST(:p AS jsonb),:at,:command)"),
                        {"w": self.workspace, "id": message, "c": conversation,
                         "p": json.dumps({"source": "center_messenger_alfa_max", "crm_branch": 1, "customer_id": 7}),
                         "at": self.at + timedelta(seconds=seconds), "command": command})
        return message

    def test_repeated_page_is_idempotent(self):
        self.project(self.row())
        first_seq = self.conversation()["inbound_seq"]
        self.project(self.row())
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.conversation()["inbound_seq"], first_seq)

    def test_same_text_and_timestamp_are_distinct_source_messages(self):
        self.project(self.row("1"), self.row("2"))
        self.assertEqual(len(self.messages()), 2)

    def test_contact_lookup_includes_customer_branch(self):
        other = uuid4()
        self.add_customer(other, 2, 7)
        self.project(self.row(branch=2), branch=2)
        self.assertEqual(self.conversation()["contact_id"], other)

    def test_linking_an_imported_customer_moves_the_same_message(self):
        self.published.return_value = None
        self.project(self.row())
        original = self.messages()[0]
        self.published.return_value = self.snapshot
        self.project(self.row())
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.messages()[0]["id"], original["id"])
        self.assertEqual(self.conversation()["contact_id"], self.contact)
        self.assertEqual(self.messages()[0]["conversation_id"], self.conversation()["id"])

    def test_local_reply_matches_a_crm_employee_id(self):
        self.project(self.row())
        message = self.local_reply(self.conversation()["id"])
        self.project(self.row("20", direction="out", body="Reply", at=self.at + timedelta(seconds=10)))
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual(self.db.execute(text("SELECT message_id FROM source_records "
                                             "WHERE workspace_id=:w AND external_record_id='out:1:20'"),
                                        {"w": self.workspace}).scalar_one(), message)

    def test_two_crm_rows_cannot_claim_one_local_send(self):
        self.project(self.row())
        self.local_reply(self.conversation()["id"])
        outgoing = self.row("20", direction="out", body="Reply", at=self.at + timedelta(seconds=5))
        outgoing["actor_id"] = None
        self.project(outgoing, outgoing | {"record_id": "21", "crm_order": 21})
        self.assertEqual(len(self.messages()), 3)

    def test_history_pages_use_crm_order_for_equal_timestamps(self):
        rows = [self.row(str(i), body=f"Message {i}") for i in range(1, 46)]
        self.project(*rows)
        conversation = self.conversation()["id"]
        collected, kwargs = [], {}
        while True:
            result = self.reader.history(None, self.workspace, conversation, **kwargs)
            self.assertEqual(result["total"], 45)
            collected = result["messages"] + collected
            if not result["has_more"]:
                break
            first = result["messages"][0]
            kwargs = {"before_time": first["occurred_at"], "before_id": UUID(first["id"])}
        self.assertEqual([str(m["crm_order"]) for m in collected], [str(i) for i in range(1, 46)])

    def test_listing_preview_is_the_last_visible_message(self):
        self.project(self.row("1", body="Question"),
                     self.row("2", body="Automated reminder", direction="out", actor="automation",
                              at=self.at + timedelta(minutes=1)))
        result = self.reader.listing(None, self.workspace)
        self.assertEqual(result["conversations"][0]["preview"], "Automated reminder")

    def test_outgoing_only_clients_are_visible_in_the_all_messages_list(self):
        self.project(self.row("1", direction="out", actor="automation", body="Only outgoing"))
        result = self.reader.listing(None, self.workspace)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["conversations"][0]["preview"], "Only outgoing")

    def test_saved_history_stays_visible_when_a_client_leaves_the_current_directory(self):
        self.project(self.row())
        self.db.execute(text("DELETE FROM alfa_directory WHERE workspace_id=:w"), {"w": self.workspace})
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 1)

    def test_saved_unlinked_history_is_visible_before_the_directory_snapshot_exists(self):
        self.published.return_value = None
        self.project(self.row())
        result = self.reader.listing(None, self.workspace)
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["conversations"][0]["can_send"])

    def test_worker_persists_progress_instead_of_restoring_old_cursor(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 1, "1:sent": 1})
        self.assertEqual(cursor["1:receive"], 2)

    def run_worker(self, cursor, page_result=None, browser_effect=None):
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "cursor": json.dumps(cursor)})
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id) VALUES (:w,:c) "
                             "ON CONFLICT(workspace_id) DO UPDATE SET due_at='-infinity',lease_until='-infinity',lease_owner=NULL"),
                        {"w": self.workspace, "c": self.channel})
        self.service._row = Mock(side_effect=lambda db, w: db.execute(
            text("SELECT *, 'https://test.s20.online' AS crm_origin FROM alfa_max_settings WHERE workspace_id=:w"),
            {"w": w}).mappings().one())
        self.service.sms_templates = Mock()
        self.service.vault.seal_browser_state.return_value = "new-session"
        self.service.browser.read_histories.side_effect = browser_effect or (
            lambda origin, session, requests, **kwargs: (
                [page_result(request) if page_result else {"messages": [], "has_more": True} for request in requests], {}))
        worker = AlfaMaxWorker(self.service)
        worker.run_next()
        cursor = self.db.execute(text("SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w"),
                                 {"w": self.workspace}).scalar_one()
        return cursor

    def test_completed_backfill_does_not_restart_on_every_poll(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 2},
                                 page_result=lambda request: {"messages": [], "has_more": request[2] == 0})
        self.assertEqual(cursor["1:receive"], 0)
        self.assertTrue(cursor["complete:1:receive"])
        self.service.browser.read_histories.reset_mock()
        cursor = self.run_worker(cursor)
        self.assertEqual(cursor["1:receive"], 0)
        requests = self.service.browser.read_histories.call_args.args[2]
        self.assertNotIn((1, "receive", 1), requests)

    def test_legacy_history_replays_once_to_repair_existing_aliases(self):
        cursor = self.run_worker({"1:receive": 9})
        self.assertEqual(cursor["projection_version"], PROJECTION_VERSION)
        self.assertEqual(cursor["1:receive"], 1)
        self.assertNotIn((1, "receive", 9), self.service.browser.read_histories.call_args.args[2])

    def test_new_message_burst_reopens_backfill_near_the_head(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 0,
                                  "complete:1:receive": True, "head:1:receive": "old"},
                                 page_result=lambda request: {"messages": [self.row("10")], "has_more": True})
        self.assertEqual(cursor["1:receive"], 0)
        self.assertEqual(cursor["catchup:1:receive"], 1)
        self.assertTrue(cursor["complete:1:receive"])

    def test_new_bursts_do_not_restart_the_archive_before_it_reaches_old_clients(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 5,
                                  "head:1:receive": "old"},
                                 page_result=lambda request: {"messages": [self.row("10")], "has_more": True})
        self.assertEqual(cursor["1:receive"], 6)
        self.assertEqual(cursor["catchup:1:receive"], 1)

    def test_a_failed_page_does_not_discard_other_clients_or_advance_its_cursor(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 4}, page_result=lambda request:
            {"error": "invalid_response"} if request == (1, "receive", 4)
            else {"messages": [self.row("20")] if request == (2, "receive", 0) else [], "has_more": False})
        self.assertEqual(cursor["1:receive"], 4)
        self.assertEqual(cursor["error:1:receive"], "invalid_response")
        self.assertEqual(len(self.messages()), 1)

    def test_all_recipients_of_a_journal_row_have_the_message(self):
        other = uuid4()
        self.add_customer(other, 2, 8)
        row = self.row("30", direction="out", actor="automation")
        row["customers"].append({"branch_id": 2, "customer_id": 8, "name": "Other", "status": "archive"})
        self.project(row)
        self.project(row | {"customers": list(reversed(row["customers"]))})
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        contacts = self.db.execute(text("SELECT contact_id FROM conversations WHERE workspace_id=:w "
                                       "AND id=ANY(CAST(:ids AS uuid[]))"),
                                   {"w": self.workspace, "ids": [m["conversation_id"] for m in messages]}).scalars().all()
        self.assertEqual(set(contacts), {self.contact, other})

    def test_multiple_cards_of_one_parent_do_not_repeat_a_journal_row(self):
        self.add_customer(self.contact, 2, 8)
        row = self.row("30", direction="out", actor="automation")
        row["customers"].append({"branch_id": 2, "customer_id": 8, "name": "Second card", "status": "client"})
        self.project(row)
        self.project(row | {"customers": list(reversed(row["customers"]))})
        self.assertEqual(len(self.messages()), 1)

    def test_later_verification_merges_proven_copies_of_one_bulk_row(self):
        self.add_customer(self.contact, 2, 8)
        row = self.row("30", direction="out", actor="automation")
        row["customers"].append({"branch_id": 2, "customer_id": 8, "name": "Second card", "status": "client"})
        self.published.return_value = None
        self.project(row)
        self.assertEqual(len(self.messages()), 2)
        self.published.return_value = self.snapshot
        self.project(row)
        self.project(row | {"customers": list(reversed(row["customers"]))})
        self.assertEqual(len(self.messages()), 1)

    def test_complete_archive_for_every_client_survives_new_bursts_and_worker_restarts(self):
        self.add_customer(uuid4(), 2, 8)
        journals = {}
        for branch in (1, 2):
            for direction in ("receive", "sent"):
                journals[(branch, direction)] = [self.row(
                    str(i), branch=branch, customer=7 if branch == 1 else 8,
                    direction="in" if direction == "receive" else "out",
                    at=datetime(2000 + i, 1, 1, tzinfo=UTC), body=f"{branch}:{direction}:{i}",
                ) for i in range(26, 0, -1)]

        def page(request):
            branch, direction, number = request
            rows = journals[(branch, direction)]
            return {"messages": rows[number * 5:(number + 1) * 5], "total": len(rows),
                    "has_more": (number + 1) * 5 < len(rows)}

        cursor = {}
        for cycle in range(30):
            if cycle in (1, 2):
                rows = journals[(1, "receive")]
                rows[:0] = [self.row(str(100 + cycle * 10 + i), at=self.at + timedelta(seconds=i))
                            for i in range(6, 0, -1)]
            # run_worker creates a fresh worker and reloads only the DB cursor.
            cursor = self.run_worker(cursor, page_result=page)
            if not history_pending(cursor):
                break
        self.assertFalse(history_pending(cursor))
        expected = {(b, "in" if d == "receive" else "out", r["record_id"])
                    for (b, d), rows in journals.items() for r in rows}
        actual = {(m["payload"]["branch_id"], m["direction"], m["payload"]["crm_record_id"])
                  for m in self.messages()}
        self.assertEqual(actual, expected)
        self.assertEqual(min(m["occurred_at"] for m in self.messages()).year, 2001)
        for item in self.reader.listing(None, self.workspace)["conversations"]:
            loaded, kwargs = [], {}
            for _ in range(30):
                response = self.reader.history(None, self.workspace, UUID(item["id"]), **kwargs)
                loaded = response["messages"] + loaded
                if not response["has_more"]:
                    break
                first = response["messages"][0]
                kwargs = {"before_time": first["occurred_at"], "before_id": UUID(first["id"])}
            self.assertEqual(len(loaded), response["total"])
            keys = [(int(m["sort_time"]), int(m["crm_order"]), m["id"]) for m in loaded]
            self.assertEqual(keys, sorted(keys))

        late = self.row("999", at=datetime(1999, 1, 1, tzinfo=UTC), body="Late old import")
        journals[(1, "receive")].append(late)
        cursor["finished:1:receive"] = (datetime.now(UTC) - RECHECK_INTERVAL).isoformat()
        for _ in range(30):
            cursor = self.run_worker(cursor, page_result=page)
            if not history_pending(cursor):
                break
        self.assertEqual(len(self.messages()), len(expected) + 1)
        self.assertEqual(min(m["occurred_at"] for m in self.messages()).year, 1999)

    def test_unlinked_customers_with_equal_numbers_in_different_branches_stay_separate(self):
        self.published.return_value = None
        self.project(self.row("1", branch=1, customer=9))
        self.project(self.row("2", branch=2, customer=9))
        self.assertEqual(len({m["conversation_id"] for m in self.messages()}), 2)

    def test_reported_source_count_mismatch_reopens_the_archive(self):
        def page(request):
            if request == (1, "receive", 0):
                return {"messages": [self.row("1")], "has_more": False, "total": 2}
            return {"messages": [], "has_more": False, "total": 0}
        cursor = self.run_worker({}, page_result=page)
        self.assertFalse(cursor["complete:1:receive"])
        self.assertEqual(cursor["error:1:receive"], "history_incomplete")
        report = history_coverage(self.db, self.workspace, self.channel, cursor, [1, 2])
        self.assertEqual(report["streams"][0]["missing_rows"], 1)
        self.assertFalse(report["history_complete"])

    def test_persistent_gap_stops_replays_but_keeps_new_messages_and_other_archives(self):
        def page(request):
            if request == (1, "receive", 0):
                return {"messages": [self.row("1")], "has_more": False, "total": 2}
            return {"messages": [], "has_more": False, "total": 0}
        cursor = self.run_worker({}, page_result=page)
        self.assertEqual(cursor["retries:1:receive"], 1)
        cursor = self.run_worker(cursor, page_result=page)
        self.assertIn("paused:1:receive", cursor)
        self.assertEqual(cursor["error:1:receive"], "history_gap")
        self.assertFalse(history_pending(cursor))
        for _ in range(3):
            cursor = self.run_worker(cursor, page_result=page)
            self.assertEqual(cursor["error:1:receive"], "history_gap")
            self.assertEqual(cursor["scan_pages:1:receive"], 1)
        report = history_coverage(self.db, self.workspace, self.channel, cursor, [1, 2])
        self.assertTrue(report["history_paused"])
        self.assertFalse(report["history_complete"])
        self.assertFalse(report["totals_verified"])
        self.assertFalse(report["backfill_pending"])
        def new_page(request):
            if request == (1, "receive", 0):
                return {"messages": [self.row("3", body="New live message")], "has_more": False, "total": 3}
            return page(request)
        cursor = self.run_worker(cursor, page_result=new_page)
        self.assertEqual({m["payload"]["crm_record_id"] for m in self.messages()}, {"1", "3"})
        self.assertIn("paused:1:receive", cursor)
        cursor, _ = plan_history(cursor, [1, 2], datetime.now(UTC) + RECHECK_INTERVAL + timedelta(seconds=1))
        self.assertNotIn("paused:1:receive", cursor)
        self.assertNotIn("retries:1:receive", cursor)

    def test_manual_retry_resumes_only_paused_archives_and_repairs_the_gap(self):
        original = {"projection_version": PROJECTION_VERSION, "streams": ["1:receive", "1:sent"],
                    "paused:1:receive": (datetime.now(UTC) + RECHECK_INTERVAL).isoformat(),
                    "error:1:receive": "history_gap", "1:receive": 0, "1:sent": 19}
        cursor = retry_history(original)
        self.assertNotIn("paused:1:receive", cursor)
        self.assertEqual(cursor["1:sent"], 19)
        self.assertIn("paused:1:receive", original)
        cursor = self.run_worker(cursor, page_result=lambda request: {
            "messages": [self.row("1"), self.row("2")] if request == (1, "receive", 0) else [],
            "has_more": False, "total": 2 if request[:2] == (1, "receive") else 0})
        report = history_coverage(self.db, self.workspace, self.channel, cursor, [1, 2])
        self.assertTrue(report["history_complete"])
        self.assertEqual(len(self.messages()), 2)

    def test_projection_failure_cannot_advance_past_missing_source_ids(self):
        def page(request):
            return {"messages": [self.row("20")] if request == (1, "receive", 4) else [], "has_more": True}
        cursor = {"projection_version": PROJECTION_VERSION, "1:receive": 4}
        with patch.object(self.service, "_project_page"):
            for _ in range(3):
                cursor = self.run_worker(cursor, page_result=page)
                self.assertEqual(cursor["1:receive"], 4)
                self.assertEqual(cursor["error:1:receive"], "history_projection")
                self.assertEqual(cursor["missing_ids:1:receive"], ["20"])
        self.assertIn("paused:1:receive", cursor)
        cursor = self.run_worker(cursor, page_result=page)
        self.assertNotIn((1, "receive", 4), self.service.browser.read_histories.call_args.args[2])
        cursor = self.run_worker(retry_history(cursor), page_result=lambda request: {
            "messages": [self.row("20")] if request == (1, "receive", 0) else [], "has_more": False})
        self.assertNotIn("error:1:receive", cursor)
        self.assertEqual(len(self.messages()), 1)

    def test_coverage_counts_source_rows_once_for_bulk_recipients(self):
        self.add_customer(uuid4(), 2, 8)
        row = self.row("1", direction="out", actor="automation")
        row["customers"].append({"branch_id": 2, "customer_id": 8, "name": "Other", "status": "archive"})
        self.project(row)
        cursor = {"projection_version": PROJECTION_VERSION, "streams": ["1:receive", "1:sent"]}
        for direction in ("receive", "sent"):
            cursor[f"complete:1:{direction}"] = True
            cursor[f"total:1:{direction}"] = int(direction == "sent")
        report = history_coverage(self.db, self.workspace, self.channel, cursor, [1])
        self.assertTrue(report["history_complete"])
        self.assertTrue(report["totals_verified"])
        self.assertEqual(report["streams"][1]["source_rows"], 1)
        self.assertEqual(len(self.messages()), 2)

    def test_page_receipt_accepts_a_saved_recipient_projection_without_a_legacy_base_alias(self):
        self.add_customer(uuid4(), 2, 8)
        row = self.row("1", direction="out", actor="automation")
        row["customers"].append({"branch_id": 2, "customer_id": 8, "name": "Other", "status": "archive"})
        self.project(row)
        self.db.execute(text("DELETE FROM source_records WHERE workspace_id=:w AND connection_id=:c "
                             "AND namespace='alfa_max' AND external_record_id='out:1:1'"),
                        {"w": self.workspace, "c": self.channel})
        result = {"messages": [row], "has_more": False}
        verify_saved_pages(self.db, self.workspace, self.channel, [(1, "sent", 0)], [result])
        self.assertNotIn("error", result)

    def test_progress_detects_an_active_worker_and_only_counts_its_channel(self):
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id,lease_owner,lease_until) "
                             "VALUES (:w,:c,:lease,clock_timestamp()+interval '1 minute')"),
                        {"w": self.workspace, "c": self.channel, "lease": uuid4()})
        connection, conversation, message_id = uuid4(), uuid4(), uuid4()
        self.db.execute(text("INSERT INTO connections(workspace_id,id,kind,name) VALUES (:w,:c,'max_bot','Bot')"),
                        {"w": self.workspace, "c": connection})
        self.db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,external_chat_id) VALUES (:w,:id,:c,'bot')"),
                        {"w": self.workspace, "id": conversation, "c": connection})
        self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,direction,actor_type,text,occurred_at) "
                             "VALUES (:w,:id,:c,'in','contact','Unrelated',now())"),
                        {"w": self.workspace, "id": message_id, "c": conversation})
        report = self.service.progress(None, self.workspace)
        self.assertTrue(report["syncing"])
        self.assertEqual(report["messages"], 1)
        self.assertEqual(report["conversations"], 1)
        self.assertEqual(report["oldest_at"], self.at)

    def test_settings_change_during_browser_read_cannot_publish_stale_history(self):
        def changed_settings(origin, session, requests, **kwargs):
            self.db.execute(text("UPDATE alfa_max_settings SET version=version+1 WHERE workspace_id=:w"),
                            {"w": self.workspace})
            return ([{"messages": [self.row()], "has_more": False} for _ in requests], {})
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION}, browser_effect=changed_settings)
        self.assertTrue(cursor.pop("client_repair")["complete"])
        self.assertEqual(cursor, {"projection_version": PROJECTION_VERSION})
        self.assertEqual(self.messages(), [])

    def test_journal_context_does_not_create_an_unlinked_copy_of_a_known_customer(self):
        self.project(self.row("100", branch=2), branch=2)
        message = self.messages()[0]
        self.assertEqual(self.conversation()["contact_id"], self.contact)
        self.assertEqual(message["payload"]["customer_branch_id"], 1)
        self.assertEqual(message["payload"]["branch_id"], 2)

    def test_incoming_branch_mirrors_retain_all_source_rows_and_one_visible_event(self):
        for branch in (4, 2, 3, 1):
            self.project(self.row(str(100 + branch), branch=branch), branch=branch)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.messages()[0]["payload"]["branch_id"], 1)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM messages WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), 4)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM source_records WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), 4)
        cursor = {"projection_version": PROJECTION_VERSION, "streams": []}
        for branch in (1, 2, 3, 4):
            for direction in ("receive", "sent"):
                key = f"{branch}:{direction}"
                cursor["streams"].append(key)
                cursor[f"complete:{key}"] = True
                cursor[f"total:{key}"] = int(direction == "receive")
        report = history_coverage(self.db, self.workspace, self.channel, cursor, [1, 2, 3, 4])
        self.assertTrue(report["history_complete"])
        for branch in (1, 2, 3, 4):
            result = {"messages": [self.row(str(100 + branch), branch=branch)], "has_more": False}
            verify_saved_pages(self.db, self.workspace, self.channel, [(branch, "receive", 0)], [result])
            self.assertNotIn("error", result)

    def test_repeated_identical_messages_in_one_minute_survive_branch_mirror_reconciliation(self):
        for branch in (2, 1):
            for occurrence in range(2):
                self.project(self.row(str(100 + occurrence * 4 + branch), branch=branch), branch=branch)
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual({m["payload"]["branch_id"] for m in self.messages()}, {1})

    def test_outgoing_and_different_customer_ids_are_not_branch_mirrors(self):
        self.add_customer(self.contact, 2, 8)
        self.project(self.row("101", branch=1))
        self.project(self.row("102", branch=2, customer=8), branch=2)
        self.project(self.row("201", direction="out", branch=1))
        self.project(self.row("202", direction="out", branch=2), branch=2)
        self.assertEqual(len(self.messages()), 4)

    def test_same_number_assigned_to_different_crm_contacts_is_not_merged(self):
        self.add_customer(uuid4(), 2, 7)
        self.project(self.row("101", branch=1))
        self.project(self.row("102", branch=2), branch=2)
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual(len({m["conversation_id"] for m in self.messages()}), 2)
        self.assertTrue(all(not m["payload"]["crm_global_verified"] for m in self.messages()))

    def test_legacy_row_identity_is_recovered_and_exact_copy_hides_without_deletion(self):
        self.project(self.row("100"))
        current = self.messages()[0]
        legacy_id = uuid4()
        legacy = dict(current["payload"])
        for key in ("crm_record_id", "crm_global_verified", "crm_customer_ids", "crm_identity_restored"):
            legacy.pop(key, None)
        self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,provider_message_id,"
                             "direction,actor_type,text,payload,occurred_at) "
                             "VALUES (:w,:id,:c,'legacy-provider','in','contact','Old text',CAST(:p AS jsonb),:at)"),
                        {"w": self.workspace, "id": legacy_id, "c": current["conversation_id"],
                         "p": json.dumps(legacy), "at": self.at})
        self.project(self.row("100"))
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.messages()[0]["id"], current["id"])
        old = self.db.execute(text("SELECT * FROM messages WHERE workspace_id=:w AND id=:id"),
                              {"w": self.workspace, "id": legacy_id}).mappings().one()
        self.assertEqual(old["payload"]["crm_record_id"], "100")
        self.assertEqual(old["payload"]["superseded_by"], str(current["id"]))
        self.assertEqual(old["payload"]["original_provider_message_id"], "legacy-provider")
        self.assertEqual(old["text"], "Old text")

    def test_existing_unlinked_chats_are_repaired_without_rereading_the_crm_archive(self):
        self.published.return_value = None
        for branch in (1, 2, 3, 4):
            self.project(self.row(str(100 + branch), branch=branch), branch=branch)
        old_ids = {m["conversation_id"] for m in self.messages()}
        self.assertEqual(len(old_ids), 4)
        self.published.return_value = self.snapshot
        cursor = {"projection_version": PROJECTION_VERSION, "2:sent": 75}
        self.assertTrue(repair_batch(self.service, self.db, self.workspace, self.channel, cursor))
        self.assertTrue(cursor["client_repair"]["complete"])
        self.assertEqual(cursor["2:sent"], 75)
        self.assertEqual(len(self.messages()), 1)
        listing = self.reader.listing(None, self.workspace)["conversations"]
        self.assertEqual(len(listing), 1)
        canonical = self.messages()[0]["conversation_id"]
        for old in old_ids:
            redirected = self.reader.history(None, self.workspace, old)
            self.assertEqual(redirected["conversation"]["id"], str(canonical))
        self.assertFalse(repair_batch(self.service, self.db, self.workspace, self.channel, cursor))

    def test_handled_marker_follows_the_visible_incoming_copy(self):
        self.project(self.row("102", branch=2), branch=2)
        conversation = self.messages()[0]["conversation_id"]
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation)
        self.project(self.row("101", branch=1))
        self.assertEqual(len(self.messages()), 1)
        handled = self.db.execute(text("SELECT handled_inbound_id FROM conversations WHERE workspace_id=:w AND id=:c"),
                                  {"w": self.workspace, "c": conversation}).scalar()
        self.assertEqual(handled, self.messages()[0]["id"])

    def test_repair_settles_a_completed_journal_gap_after_recovering_legacy_ids(self):
        self.project(self.row("100"))
        self.db.execute(text("UPDATE messages SET payload=(payload-'crm_record_id')-'crm_global_verified' "
                             "WHERE workspace_id=:w"), {"w": self.workspace})
        cursor = {"projection_version": PROJECTION_VERSION, "streams": ["1:receive"], "1:receive": 9,
                  "complete:1:receive": False, "total:1:receive": 1, "gap:1:receive": 1,
                  "error:1:receive": "history_incomplete", "finished:1:receive": self.at.isoformat(),
                  "started:1:receive": (self.at + timedelta(minutes=1)).isoformat()}
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        self.assertEqual(cursor["1:receive"], 0)
        self.assertTrue(cursor["complete:1:receive"])
        self.assertNotIn("gap:1:receive", cursor)
        self.assertNotIn("error:1:receive", cursor)
        self.assertEqual(self.messages()[0]["payload"]["crm_record_id"], "100")

    def test_repair_does_not_cancel_a_periodic_scan_for_late_old_rows(self):
        self.project(self.row("100"))
        cursor = {"projection_version": PROJECTION_VERSION, "streams": ["1:receive"], "1:receive": 9,
                  "complete:1:receive": False, "total:1:receive": 1,
                  "finished:1:receive": self.at.isoformat(),
                  "started:1:receive": (self.at + RECHECK_INTERVAL).isoformat()}
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        self.assertEqual(cursor["1:receive"], 9)
        self.assertFalse(cursor["complete:1:receive"])

    def test_preferred_customer_branch_is_available(self):
        self.add_customer(self.contact, 2, 7)
        self.db.execute(text("UPDATE alfa_branches SET available=false WHERE workspace_id=:w AND external_id=1"),
                        {"w": self.workspace})
        self.project(self.row("100", branch=2), branch=2)
        self.assertEqual(self.messages()[0]["payload"]["customer_branch_id"], 2)

    def test_client_repair_checkpoint_resumes_and_does_not_create_empty_dialogs(self):
        self.project(self.row("100"))
        for index in range(1, 22):
            self.add_customer(UUID(int=index), 1, 700 + index)
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 5})
        self.assertFalse(cursor["client_repair"]["complete"])
        self.assertEqual(cursor["client_repair"]["processed"], 20)
        self.assertEqual(cursor["1:receive"], 5)
        self.service.browser.read_histories.assert_not_called()
        cursor = self.run_worker(cursor)
        self.assertTrue(cursor["client_repair"]["complete"])
        self.assertEqual(cursor["client_repair"]["processed"], 22)
        self.assertEqual(cursor["1:receive"], 6)
        self.service.browser.read_histories.assert_called_once()
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM conversations WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), 1)

    def test_old_rows_without_identity_are_kept_and_reported_for_review(self):
        self.project(self.row("100"))
        self.db.execute(text("UPDATE messages SET payload=(payload-'crm_record_id')-'crm_order' WHERE workspace_id=:w"),
                        {"w": self.workspace})
        cursor = {"projection_version": PROJECTION_VERSION, "streams": ["1:receive"],
                  "complete:1:receive": True, "complete:1:sent": True}
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        self.assertEqual(len(self.messages()), 1)
        report = history_coverage(self.db, self.workspace, self.channel, cursor, [1])
        self.assertFalse(report["history_complete"])
        self.assertEqual(report["unidentified_rows"], 1)

    def test_reported_four_branch_topology_repairs_saved_history_and_live_updates(self):
        other = uuid4()
        self.add_customer(other, 2, 8)
        self.add_customer(self.contact, 2, 7)
        self.published.return_value = None
        source = defaultdict(list)
        for branch in (1, 2, 3, 4):
            for customer in (7, 8):
                for event in range(3):
                    row = self.row(str(10000 + customer * 100 + event * 4 + branch), branch=branch,
                                   customer=customer, body=f"Private event {event}", at=self.at + timedelta(minutes=event))
                    self.project(row, branch=branch)
                    source[branch].append(row)
        current = self.messages()
        for message in current:
            if message["occurred_at"] == self.at:
                legacy = {key: value for key, value in message["payload"].items()
                          if key not in {"crm_record_id", "crm_identity_restored", "crm_customer_ids"}}
                self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,direction,actor_type,text,payload,occurred_at) "
                                     "VALUES (:w,:id,:c,'in','contact',:body,CAST(:p AS jsonb),:at)"),
                                {"w": self.workspace, "id": uuid4(), "c": message["conversation_id"],
                                 "body": message["text"], "p": json.dumps(legacy), "at": self.at})
        self.assertEqual(len(self.messages()), 32)
        self.published.return_value = self.snapshot
        cursor = {"projection_version": PROJECTION_VERSION, "streams": []}
        for branch in (1, 2, 3, 4):
            for direction in ("receive", "sent"):
                key = f"{branch}:{direction}"
                cursor["streams"].append(key)
                cursor[f"complete:{key}"] = True
                cursor[f"total:{key}"] = 6 if direction == "receive" else 0
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        self.assertEqual(len(self.messages()), 6)
        self.assertEqual(len(self.reader.listing(None, self.workspace)["conversations"]), 2)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM messages WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), 32)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM source_records WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), 24)
        self.assertTrue(history_coverage(self.db, self.workspace, self.channel, cursor, [1, 2, 3, 4])["history_complete"])
        for branch, rows in source.items():
            result = {"messages": rows, "has_more": False}
            verify_saved_pages(self.db, self.workspace, self.channel, [(branch, "receive", 0)], [result])
            self.assertNotIn("error", result)
            self.project(self.row(str(20000 + branch), branch=branch, body="New live event"), branch=branch)
        self.assertEqual(len(self.messages()), 7)
        self.assertEqual(sum(m["text"] == "New live event" for m in self.messages()), 1)
        progress = self.service.progress(None, self.workspace)
        self.assertEqual(progress["messages"], 7)
        self.assertEqual(progress["stored_messages"], 36)

    def test_loading_old_history_does_not_invalidate_a_current_reply(self):
        self.project(self.row("20"))
        current = self.conversation()["inbound_seq"]
        self.project(self.row("1", at=self.at - timedelta(days=1)), self.row("2"))
        self.assertEqual(self.conversation()["inbound_seq"], current)

    def test_cursor_ahead_of_restored_database_resets_to_current_history(self):
        self.project(self.row())
        result = self.reader.history(None, self.workspace, self.conversation()["id"], after_seq=10_000)
        self.assertTrue(result["reset"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertLess(result["next_seq"], 10_000)

    def test_moved_pagination_boundary_resets_to_a_consistent_snapshot(self):
        self.project(self.row())
        result = self.reader.history(None, self.workspace, self.conversation()["id"],
                                     before_time=self.at, before_id=uuid4())
        self.assertTrue(result["reset"])
        self.assertEqual(len(result["messages"]), 1)

    def test_max_bot_replayed_create_does_not_duplicate(self):
        bot = uuid4()
        self.db.execute(text("INSERT INTO connections(workspace_id,id,kind,name) VALUES (:w,:c,'max_bot','Bot')"),
                        {"w": self.workspace, "c": bot})
        event = {"update_type": "message_created", "timestamp": 1792800000000,
                 "message": {"timestamp": 1792800000000,
                             "sender": {"user_id": 10, "is_bot": False, "first_name": "Parent"},
                             "recipient": {"chat_type": "dialog", "user_id": 99},
                             "body": {"mid": "stable-mid", "text": "Hello"}}}
        project_event(self.db, self.workspace, bot, 99, event)
        project_event(self.db, self.workspace, bot, 99, event)
        self.assertEqual(len(self.messages()), 1)

    def test_late_send_confirmation_removes_already_imported_echo(self):
        self.project(self.row())
        conversation = self.conversation()["id"]
        message = self.local_reply(conversation)
        self.db.execute(text("UPDATE send_commands SET state='sending' WHERE workspace_id=:w"),
                        {"w": self.workspace})
        outgoing = self.row("20", direction="out", body="Reply", at=self.at + timedelta(seconds=5))
        self.project(outgoing)
        echo = next(m["id"] for m in self.messages() if m["direction"] == "out" and m["id"] != message)
        before = self.reader.history(None, self.workspace, conversation)["next_seq"]
        self.db.execute(text("UPDATE send_commands SET state='accepted' WHERE workspace_id=:w"),
                        {"w": self.workspace})
        self.project(outgoing)
        self.assertEqual(len(self.messages()), 2)
        delta = self.reader.history(None, self.workspace, conversation, after_seq=before)
        self.assertIn(str(echo), delta["removed_ids"])
        self.assertEqual([m["id"] for m in delta["messages"]], [str(message)])
        self.assertEqual(delta["total"], 2)

    def test_moved_message_is_removed_from_old_history_cache(self):
        self.published.return_value = None
        self.project(self.row())
        message = self.messages()[0]
        old = self.reader.history(None, self.workspace, message["conversation_id"])
        self.published.return_value = self.snapshot
        self.project(self.row())
        delta = self.reader.history(None, self.workspace, message["conversation_id"], after_seq=old["next_seq"])
        self.assertEqual(delta["messages"], [])
        self.assertEqual(delta["removed_ids"], [str(message["id"])])

    def test_unread_count_includes_new_message_in_the_same_second(self):
        self.project(self.row("1"))
        conversation = self.conversation()["id"]
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation)
        self.project(self.row("2", body="Another question"))
        listing = self.reader.listing(None, self.workspace)
        self.assertTrue(listing["conversations"][0]["unread"])
        self.assertEqual(listing["conversations"][0]["unread_count"], 1)

    def test_import_restores_distinct_rows_previously_collapsed_by_content(self):
        self.project(self.row("1"))
        message = self.messages()[0]
        self.db.execute(text("INSERT INTO source_records(workspace_id,id,connection_id,message_id,namespace,external_record_id) "
                             "VALUES (:w,:id,:c,:m,'alfa_max','in:1:2')"),
                        {"w": self.workspace, "id": uuid4(), "c": self.channel, "m": message["id"]})
        self.project(self.row("2"))
        self.assertEqual(len(self.messages()), 2)

    def test_message_serialization_keeps_crm_bigint_and_attachment_names(self):
        self.project(self.row("9007199254740993") | {"attachments": [{"name": "schedule.pdf"}]})
        result = self.reader.history(None, self.workspace, self.conversation()["id"])
        message = result["messages"][0]
        self.assertEqual(message["crm_order"], "9007199254740993")
        self.assertEqual(message["attachments"], ["file"])
        self.assertEqual(message["attachment_names"], ["schedule.pdf"])

    def test_one_verified_parent_has_one_chat_across_branches(self):
        self.add_customer(self.contact, 2, 9)
        self.project(self.row("1"))
        self.project(self.row("2", branch=2, customer=9, body="Second branch"), branch=2)
        self.assertEqual(len({m["conversation_id"] for m in self.messages()}), 1)
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 1)

    def test_old_branch_chats_merge_with_pending_sends_and_handled_state(self):
        self.project(self.row("1"))
        canonical = self.conversation()["id"]
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, canonical)
        old = uuid4()
        self.db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id,peer_name) "
                             "VALUES (:w,:id,:c,:contact,:external,'Parent')"),
                        {"w": self.workspace, "id": old, "c": self.channel,
                         "contact": self.contact, "external": f"alfa-contact:2:{self.contact}"})
        pending = self.local_reply(old)
        self.project(self.row("1"))
        messages = self.messages()
        self.assertTrue(all(m["conversation_id"] == canonical for m in messages))
        self.assertEqual(self.db.execute(text("SELECT conversation_id FROM send_commands WHERE workspace_id=:w"),
                                        {"w": self.workspace}).scalar_one(), canonical)
        self.assertFalse(self.reader.listing(None, self.workspace)["conversations"][0]["unread"])
        redirected = self.reader.history(None, self.workspace, old)
        self.assertEqual(redirected["conversation"]["id"], str(canonical))
        self.assertIn(str(pending), [m["id"] for m in redirected["messages"]])

    def test_merging_proven_provider_echo_retains_local_send(self):
        self.project(self.row("1"))
        canonical = self.conversation()["id"]
        outgoing = self.row("20", direction="out", body="Reply", at=self.at + timedelta(seconds=5))
        self.project(outgoing)
        old = uuid4()
        self.db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id,peer_name) "
                             "VALUES (:w,:id,:c,:contact,:external,'Parent')"),
                        {"w": self.workspace, "id": old, "c": self.channel,
                         "contact": self.contact, "external": f"alfa-contact:2:{self.contact}"})
        local = self.local_reply(old)
        self.db.execute(text("UPDATE messages SET provider_message_id='alfa:out:1:20' WHERE workspace_id=:w AND id=:id"),
                        {"w": self.workspace, "id": local})
        self.project(self.row("1"))
        self.assertEqual(len(self.messages()), 2)
        self.assertIn(local, [m["id"] for m in self.messages()])
        self.assertEqual(self.db.execute(text("SELECT message_id FROM source_records WHERE workspace_id=:w "
                                             "AND external_record_id='out:1:20'"),
                                        {"w": self.workspace}).scalar_one(), local)

    def test_automation_is_not_correlated_to_a_human_send(self):
        self.project(self.row("1"))
        self.local_reply(self.conversation()["id"])
        self.project(self.row("20", direction="out", body="Reply", actor="automation",
                              at=self.at + timedelta(seconds=5)))
        self.assertEqual(len(self.messages()), 3)
        self.assertEqual(sum(m["actor_type"] == "automation" for m in self.messages()), 1)

    def test_retry_of_a_send_in_a_merged_chat_is_still_idempotent(self):
        self.project(self.row("1"))
        old = uuid4()
        self.db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id,peer_name) "
                             "VALUES (:w,:id,:c,:contact,:external,'Parent')"),
                        {"w": self.workspace, "id": old, "c": self.channel,
                         "contact": self.contact, "external": f"alfa-contact:2:{self.contact}"})
        self.local_reply(old)
        command = self.db.execute(text("SELECT * FROM send_commands WHERE workspace_id=:w"),
                                  {"w": self.workspace}).mappings().one()
        self.project(self.row("1"))
        model = SendInput(request_id=UUID(command["client_request_id"]), text="Reply", seen_inbound_seq=0)
        result = self.reader.send(SimpleNamespace(user_id=self.user), self.workspace, old, model)
        self.assertEqual(result["command_id"], str(command["id"]))
        with self.assertRaises(AuthError) as error:
            self.reader.send(SimpleNamespace(user_id=self.user), self.workspace, old,
                             model.model_copy(update={"text": "Different message"}))
        self.assertEqual(error.exception.status, 409)

    def test_one_saved_message_reports_that_older_crm_history_is_loading(self):
        self.project(self.row())
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "cursor": json.dumps({"projection_version": PROJECTION_VERSION, "1:receive": 4})})
        conversation = self.conversation()["id"]
        result = self.reader.history(None, self.workspace, conversation)
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["conversation"]["history_loading"])
        self.assertFalse(result["has_more"])
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "cursor": json.dumps({"projection_version": PROJECTION_VERSION, "1:receive": 0,
                                                                  "complete:1:receive": True, "streams": ["1:receive"]})})
        result = self.reader.history(None, self.workspace, conversation)
        self.assertFalse(result["conversation"]["history_loading"])

    def test_crm_history_error_is_visible_in_the_conversation_metadata(self):
        self.project(self.row())
        self.db.execute(text("UPDATE alfa_max_settings SET last_error_code='network' WHERE workspace_id=:w"),
                        {"w": self.workspace})
        meta = self.reader.history(None, self.workspace, self.conversation()["id"])["conversation"]
        self.assertIn("AlfaCRM", meta["history_error"])
        self.assertIsNotNone(self.reader.listing(None, self.workspace)["conversations"][0]["history_error"])


if __name__ == "__main__":
    unittest.main()
