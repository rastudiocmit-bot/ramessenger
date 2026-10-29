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
from center_messenger.alfa_history import PROJECTION_VERSION, RECHECK_INTERVAL, advance_history, history_coverage, history_pending, plan_history, retry_history, verify_saved_pages, next_history_delay
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser, _message_time, parse_history_html
from bs4 import BeautifulSoup
from center_messenger.chat_api import ChatReader, SendInput, HandleInput, BulkHandleInput
from center_messenger.chat_store import changed, project_event, refresh_unread
from center_messenger.messaging import Messaging
from center_messenger.crm_api import DirectoryReader
from center_messenger.auth_security import AuthError
from center_messenger.database import log_database_error
from center_messenger.history_audit import compare_source_history
from center_messenger.alfa_repair import repair_batch
from center_messenger import alfa_shared_parent, card_preference
from sqlalchemy.exc import OperationalError


class HistoryCursorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, tzinfo=UTC)

    def test_completed_journals_are_not_rewound_after_six_hours(self):
        original = {"projection_version": PROJECTION_VERSION, "1:receive": 0,
                    "complete:1:receive": True, "finished:1:receive": self.now.isoformat()}
        cursor, requests = plan_history(original, [1], self.now + RECHECK_INTERVAL)
        self.assertTrue(cursor["complete:1:receive"])
        advance_history(cursor, requests, [{"messages": [], "has_more": True} for _ in requests], self.now)
        self.assertEqual(cursor["1:receive"], 0)

    def test_a_new_branch_is_incomplete_until_both_journals_are_read(self):
        cursor, requests = plan_history({}, [1], self.now)
        advance_history(cursor, requests, [{"messages": [], "has_more": False} for _ in requests], self.now)
        self.assertFalse(history_pending(cursor))
        cursor, _ = plan_history(cursor, [1, 2], self.now)
        self.assertTrue(history_pending(cursor))

    def test_retry_cannot_erase_known_missing_ids_from_a_finished_journal(self):
        original={"complete:1:receive":True,"finished:1:receive":self.now.isoformat(),
                  "missing_ids:1:receive":["7"],"streams":["1:receive","1:sent"]}
        cursor, requests=plan_history(retry_history(original,["1:receive"]),[1],self.now)
        advance_history(cursor,requests,[{"messages":[],"has_more":False} for _ in requests],self.now)
        self.assertEqual(cursor["missing_ids:1:receive"],["7"])

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
        browser.read_histories.side_effect = lambda origin, state, requests, **kwargs: ([pages[r[2]] for r in requests], state)
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

    def test_catchup_uses_previous_head_ids_not_an_unrelated_saved_row(self):
        cursor, _ = plan_history({'sync_policy':1,'head:1:receive':'30','head_ids:1:receive':['30','29'],
            'complete:1:receive':True,'imported:1:receive':True},[1],self.now)
        def read(page, ids, known):
            advance_history(cursor,[(1,'receive',page)],[{'messages':[{'record_id':n} for n in ids],
                'has_more':True,'known_ids':known}],self.now)
        read(0,['40','10'],['10'])
        self.assertEqual(cursor['catchup:1:receive'],1)
        read(1,['39','10'],['10'])
        self.assertEqual(cursor['catchup:1:receive'],2)
        read(2,['38','29'],['29'])  # The old first row 30 was removed, but its neighbour remains.
        self.assertEqual(cursor['catchup:1:receive'],0)
        self.assertTrue(cursor['complete:1:receive'])


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

    def test_actor_provenance_overrides_text_templates_and_staff_accounts(self):
        def parse(metadata, body):
            html = f'<table class="crm-table"><tbody><tr data-key="1" {metadata}><td></td><td></td><td data-employee-id="42">Employee</td><td>{body}</td><td>24.10.2026 03:10</td></tr></tbody></table>'
            return parse_history_html(html, origin='https://test.s20.online', branch=1, direction='sent')['messages'][0]
        manual = parse('', 'Цитата: системное сообщение. Отвечаю вручную.')
        self.assertEqual(manual['actor_type'], 'human')
        self.assertEqual(manual['actor_evidence']['reason'], 'crm_employee')
        automatic = parse('data-campaign-id="13"', 'Здравствуйте')
        self.assertEqual(automatic['actor_type'], 'automation')
        self.assertEqual(automatic['actor_evidence']['reason'], 'crm_automation')


class LoaderPlanningTests(unittest.TestCase):
    def test_all_heads_and_catchups_precede_old_archive_pages(self):
        cursor, requests = plan_history({"1:receive": 9, "1:sent": 4,
            "catchup:1:receive": 2}, [1, 2])
        self.assertEqual(requests[:4], [(1, "receive", 0), (1, "sent", 0), (2, "receive", 0), (2, "sent", 0)])
        self.assertEqual(requests[4], (1, "receive", 2))
        self.assertEqual(requests[5:], [(1, "receive", 9), (1, "sent", 4)])

    def test_archive_pause_keeps_heads_and_live_catchup_and_does_not_move_saved_history(self):
        original = {"archive_paused": True, "1:receive": 9, "catchup:1:receive": 2,
                    "complete:1:sent": True, "finished:1:sent": datetime.now(UTC).isoformat()}
        cursor, requests = plan_history(original, [1])
        self.assertEqual(requests, [(1, "receive", 0), (1, "sent", 0), (1, "receive", 2)])
        advance_history(cursor, [(1, "receive", 0)], [{"messages": [{"record_id": "30"}], "has_more": True}])
        self.assertEqual(cursor["1:receive"], 9)
        self.assertTrue(cursor["complete:1:sent"])
        self.assertIn("live_checked:1:receive", cursor)

    def test_one_saved_archive_page_does_not_mark_other_journals_live_or_advance_them(self):
        cursor, _ = plan_history({"1:receive": 4, "1:sent": 7}, [1, 2])
        advance_history(cursor, [(1, "receive", 4)], [{"messages": [], "has_more": True}])
        self.assertEqual(cursor["1:receive"], 5)
        self.assertEqual(cursor["1:sent"], 7)
        self.assertNotIn("live_checked:1:receive", cursor)
        self.assertNotIn("live_checked:2:sent", cursor)

    def test_failed_page_has_its_own_retry_and_cannot_delay_healthy_heads(self):
        now = datetime.now(UTC)
        cursor, _ = plan_history({"1:receive": 4, "complete:1:sent": True}, [1], now)
        advance_history(cursor, [(1, "receive", 4)], [{"error": "crm_unavailable"}], now)
        cursor, requests = plan_history(cursor, [1], now + timedelta(seconds=1))
        self.assertNotIn((1, "receive", 4), requests)
        self.assertIn((1, "receive", 0), requests)
        self.assertIn((1, "sent", 0), requests)
        self.assertEqual(next_history_delay(cursor, now), 0)  # Healthy heads have not been read yet.
        retry = retry_history(cursor, ["1:receive"])
        self.assertNotIn("retry_archive:1:receive", retry)
        self.assertEqual(retry["1:receive"], 4)
        self.assertTrue(retry["complete:1:sent"])

    def test_browser_commits_a_page_before_reading_the_next_and_skips_paused_archive(self):
        manager, browser, context, page = [Mock() for _ in range(4)]
        context.storage_state.return_value = {}
        timeline = []
        def read(page, origin, branch, direction, number):
            timeline.append(("read", direction, number))
            return {"messages": [], "has_more": True}
        def commit(request, result, state):
            timeline.append(("skip" if result.get("skipped") else "commit", request[1], request[2]))
        with patch.object(AlfaMaxBrowser, "_open", return_value=(manager, browser, context, page)), \
             patch.object(AlfaMaxBrowser, "_read_history_page", side_effect=read):
            results, _ = AlfaMaxBrowser().read_histories("https://test.s20.online", {},
                [(1, "receive", 0), (1, "receive", 4), (1, "sent", 0)],
                before_read=lambda request: request[2] == 0, on_result=commit)
        self.assertEqual(timeline, [("read", "receive", 0), ("commit", "receive", 0),
            ("skip", "receive", 4), ("read", "sent", 0), ("commit", "sent", 0)])
        self.assertTrue(results[1]["skipped"])

    def test_callback_database_error_is_not_mislabelled_as_a_browser_failure(self):
        manager, browser, context, page = [Mock() for _ in range(4)]
        context.storage_state.return_value = {}
        failure = RuntimeError("save failed")
        with patch.object(AlfaMaxBrowser, "_open", return_value=(manager, browser, context, page)), \
             patch.object(AlfaMaxBrowser, "_read_history_page", return_value={"messages": [], "has_more": False}):
            with self.assertRaises(RuntimeError) as raised:
                AlfaMaxBrowser().read_histories("https://test.s20.online", {}, [(1, "receive", 0)],
                    on_result=Mock(side_effect=failure))
        self.assertIs(raised.exception, failure)


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
            if kwargs.get("snapshot"):
                # Requests use separate committed transactions in production.
                # This rollback fixture flushes their deferred invalidations before a read.
                self.db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                self.db.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            with self.db.begin_nested():
                yield self.db

        self.membership = {"role": "owner", "sections": ["overview", "messages", "security"], "timezone": "Europe/Moscow"}
        self.accounts = SimpleNamespace(transaction=transaction, throttle=Mock(), _workspace=lambda *args, **kwargs: self.membership)
        self.service = AlfaMaxConnections(SimpleNamespace(accounts=self.accounts, vault=Mock(), _transaction=transaction), browser=Mock())
        maximum = SimpleNamespace(chat_transaction=transaction, accounts=self.accounts)
        self.reader = ChatReader(SimpleNamespace(max=maximum, alfa_max=self.service, enqueue=Messaging.enqueue))

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

    def customer_phone(self, customer, *, phone="+79160000000", parent="Один родитель", status="client"):
        self.db.execute(text("UPDATE alfa_customers SET phones=CAST(:phones AS jsonb),legal_name=:parent,status=:status "
                             "WHERE workspace_id=:w AND run_id=:r AND external_id=:customer"),
                        {"w": self.workspace, "r": self.run, "customer": customer,
                         "phones": json.dumps([phone]), "parent": parent, "status": status})

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

    def checkpoint(self, conversation):
        return HandleInput(seen_checkpoint=self.reader.history(None, self.workspace, conversation)["conversation"]["seen_checkpoint"])

    def prepare_bulk(self):
        return self.reader.prepare_bulk_handled(SimpleNamespace(user_id=self.user),self.workspace)

    def confirm_bulk(self, preview, *, actor=None, workspace=None):
        return self.reader.confirm_bulk_handled(actor or SimpleNamespace(user_id=self.user),workspace or self.workspace,
            BulkHandleInput(operation_id=preview['operation_id'],confirmed=True))

    def test_bulk_preview_captures_all_pages_and_changes_no_message_status(self):
        rows=[self.row()]
        for customer in range(8,47):
            self.add_customer(uuid4(),1,customer)
            rows.append(self.row(str(customer),customer=customer))
        self.project(*rows)
        self.assertEqual(len(self.reader.listing(None,self.workspace)['conversations']),30)
        preview=self.prepare_bulk()
        self.assertEqual(preview['conversation_count'],40)
        self.assertEqual(preview['client_count'],40)
        self.assertEqual(preview['message_count'],40)
        self.assertTrue(all(m['payload'].get('handled_receipt') is None for m in self.messages()))

    def test_bulk_confirmation_preserves_new_incoming_new_chats_and_late_history(self):
        self.add_customer(uuid4(),1,8)
        self.project(self.row('1',body='Known one'),self.row('2',customer=8,body='Known two'))
        preview=self.prepare_bulk()
        self.add_customer(uuid4(),1,9)
        self.project(self.row('3',body='New incoming',at=self.at+timedelta(minutes=1)),
                     self.row('4',customer=9,body='New chat'),
                     self.row('5',body='Late history',at=self.at-timedelta(days=100)))
        result=self.confirm_bulk(preview)
        self.assertEqual(result['processed'],2)
        self.assertEqual(len(self.messages()),5)
        for message in self.messages():
            self.assertEqual(message['payload'].get('handled_receipt') is not None,message['text'].startswith('Known'))
        self.assertEqual(self.reader.listing(None,self.workspace)['unread'],2)

    def test_bulk_confirmation_replay_never_marks_later_incoming(self):
        self.project(self.row())
        preview=self.prepare_bulk()
        first=self.confirm_bulk(preview)
        self.project(self.row('2',body='Keep unread',at=self.at+timedelta(minutes=1)))
        latest=self.db.execute(text('SELECT max(seq) FROM message_changes WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one()
        self.assertEqual(self.confirm_bulk(preview),first)
        self.assertEqual(self.db.execute(text('SELECT max(seq) FROM message_changes WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one(),latest)
        self.assertIsNone(next(m for m in self.messages() if m['text']=='Keep unread')['payload'].get('handled_receipt'))
        self.assertTrue(self.reader.history(None,self.workspace,self.conversation()['id'])['conversation']['unread'])

    def test_bulk_snapshot_is_bound_to_actor_workspace_and_current_write_permission(self):
        self.project(self.row())
        preview=self.prepare_bulk()
        for kwargs in ({'actor':SimpleNamespace(user_id=uuid4())},{'workspace':uuid4()}):
            with self.subTest(kwargs=kwargs),self.assertRaises(AuthError) as raised:
                self.confirm_bulk(preview,**kwargs)
            self.assertEqual(raised.exception.status,404)
        self.membership['role']='observer'
        with self.assertRaises(AuthError) as raised:self.confirm_bulk(preview)
        self.assertEqual(raised.exception.status,403)
        with self.assertRaises(AuthError):self.prepare_bulk()
        self.assertIsNone(self.messages()[0]['payload'].get('handled_receipt'))

    def test_bulk_confirmation_requires_explicit_true_and_rejects_expired_preview(self):
        from pydantic import ValidationError
        self.project(self.row())
        preview=self.prepare_bulk()
        for body in ({'operation_id':preview['operation_id']},{'operation_id':preview['operation_id'],'confirmed':False}):
            with self.subTest(body=body),self.assertRaises(ValidationError):BulkHandleInput(**body)
        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{expires_at}',to_jsonb(CAST(:at AS text))) WHERE id=:id"),
            {'id':preview['operation_id'],'at':'2000-01-01T00:00:00+00:00'})
        with self.assertRaises(AuthError) as raised:self.confirm_bulk(preview)
        self.assertEqual(raised.exception.status,409)
        self.assertIsNone(self.messages()[0]['payload'].get('handled_receipt'))

    def test_bulk_skips_a_chat_relinked_after_preview(self):
        self.project(self.row())
        preview=self.prepare_bulk()
        other=uuid4()
        self.db.execute(text("INSERT INTO contacts(workspace_id,id,display_name) VALUES(:w,:c,'Other')"),{'w':self.workspace,'c':other})
        self.db.execute(text('UPDATE conversations SET contact_id=:c WHERE workspace_id=:w'),{'w':self.workspace,'c':other})
        result=self.confirm_bulk(preview)
        self.assertEqual(result['skipped'],1)
        self.assertEqual(result['processed'],0)
        self.assertIsNone(self.messages()[0]['payload'].get('handled_receipt'))

    def test_bulk_id_snapshot_does_not_cover_a_late_commit_in_an_earlier_sequence_gap(self):
        self.project(self.row('1',direction='out',actor='automation',body='System'),
                     self.row('2',body='Known',at=self.at+timedelta(minutes=5)))
        system=next(m for m in self.messages() if m['text']=='System')
        gap=self.db.execute(text('SELECT min(seq) FROM message_changes WHERE message_id=:m'),{'m':system['id']}).scalar_one()
        self.db.execute(text('DELETE FROM message_changes WHERE message_id=:m'),{'m':system['id']})
        preview=self.prepare_bulk()
        self.project(self.row('3',body='Late commit',at=self.at+timedelta(minutes=1)))
        late=next(m for m in self.messages() if m['text']=='Late commit')
        seq=self.db.execute(text('SELECT min(seq) FROM message_changes WHERE message_id=:m'),{'m':late['id']}).scalar_one()
        self.db.execute(text('INSERT INTO message_changes(workspace_id,conversation_id,message_id,new_inbound,seq) OVERRIDING SYSTEM VALUE '
            'SELECT workspace_id,conversation_id,message_id,new_inbound,:gap FROM message_changes WHERE message_id=:m AND seq=:seq'),
            {'m':late['id'],'gap':gap,'seq':seq})
        self.db.execute(text('DELETE FROM message_changes WHERE message_id=:m AND seq=:seq'),{'m':late['id'],'seq':seq})
        self.confirm_bulk(preview)
        self.assertIsNone(next(m for m in self.messages() if m['text']=='Late commit')['payload'].get('handled_receipt'))
        self.assertIsNotNone(next(m for m in self.messages() if m['text']=='Known')['payload'].get('handled_receipt'))

    def test_bulk_failure_rolls_back_all_chats_and_keeps_the_same_confirmation_retryable(self):
        self.add_customer(uuid4(),1,8)
        self.project(self.row(),self.row('2',customer=8))
        preview=self.prepare_bulk()
        apply=self.reader._apply_handled
        calls=0
        def fail_second(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('synthetic database failure')
            return apply(*args,**kwargs)
        with patch.object(self.reader,'_apply_handled',side_effect=fail_second),self.assertRaises(RuntimeError):
            self.confirm_bulk(preview)
        self.assertTrue(all(m['payload'].get('handled_receipt') is None for m in self.messages()))
        self.assertEqual(self.confirm_bulk(preview)['processed'],2)

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
        changed(self.db, self.workspace, conversation, message)
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

    def test_listing_preview_ignores_automation(self):
        self.project(self.row("1", body="Question"),
                     self.row("2", body="Automated reminder", direction="out", actor="automation",
                              at=self.at + timedelta(minutes=1)))
        result = self.reader.listing(None, self.workspace)
        self.assertEqual(result["conversations"][0]["preview"], "Question")

    def test_dialog_order_is_unread_then_read_newest_first_across_all_loaded_pages(self):
        entries = []
        for n in range(38):
            contact = uuid4()
            self.add_customer(contact, 1, 100+n)
            self.project(self.row(str(100+n), customer=100+n, at=self.at + timedelta(minutes=n)))
            conversation = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND contact_id=:contact"),
                                           {"w": self.workspace, "contact": contact}).scalar_one()
            unread = n % 6 != 0
            if not unread:
                self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, self.checkpoint(conversation))
            entries.append((unread, n, str(conversation)))
        expected = [c for _, _, c in sorted(entries, key=lambda x: (not x[0], -x[1]))]
        first = self.reader.listing(None, self.workspace)
        second = self.reader.listing(None, self.workspace, page=1)
        self.assertEqual([c["id"] for c in first["conversations"] + second["conversations"]], expected)
        self.assertEqual([c["id"] for c in self.reader.listing(None, self.workspace, through_page=1)["conversations"]], expected)
        self.assertEqual(first["unread"], 31)
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, UUID(expected[0]), self.checkpoint(UUID(expected[0])))
        refreshed = self.reader.listing(None, self.workspace, through_page=1)["conversations"]
        self.assertEqual([c["id"] for c in refreshed if c["unread"]], expected[1:31])

    def test_dialog_order_and_time_use_visible_messages_even_with_stale_cached_time(self):
        self.add_customer(uuid4(), 1, 8)
        self.project(self.row("1", customer=7, body="Older"),
                     self.row("2", customer=8, body="Newer", at=self.at + timedelta(microseconds=1)),
                     self.row("3", customer=7, direction="out", actor="automation", body="Hidden", at=self.at + timedelta(days=1)))
        self.db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('deleted',true) "
                             "WHERE workspace_id=:w AND payload->>'crm_record_id'='3'"), {"w": self.workspace})
        rows = self.reader.listing(None, self.workspace)["conversations"]
        self.assertEqual([c["preview"] for c in rows], ["Newer", "Older"])
        self.assertEqual(rows[1]["last_message_at"], self.at)
        self.assertEqual(int(rows[0]["last_message_sort_time"]) - int(rows[1]["last_message_sort_time"]), 1)

    def test_outgoing_only_clients_are_visible_in_the_all_messages_list(self):
        self.project(self.row("1", direction="out", actor="automation", body="Only outgoing"))
        result = self.reader.listing(None, self.workspace)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["conversations"][0]["preview"], "Переписка ещё не началась")
        self.assertIsNone(result["conversations"][0]["last_message_at"])

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

    def run_worker(self, cursor, page_result=None, browser_effect=None, *, after_seconds=0):
        # Simulate elapsed scheduling time between worker restarts without a
        # real sleep or changing the database/lease clock of this rollback fixture.
        cursor = json.loads(json.dumps(cursor))
        if after_seconds:
            for key, value in list(cursor.items()):
                if key.startswith(('due_', 'live_checked:')):
                    cursor[key] = (datetime.fromisoformat(value) - timedelta(seconds=after_seconds)).isoformat()
                elif key.startswith('retry_') and isinstance(value, dict) and value.get('next_at'):
                    value['next_at'] = (datetime.fromisoformat(value['next_at']) - timedelta(seconds=after_seconds)).isoformat()
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "cursor": json.dumps(cursor)})
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id) VALUES (:w,:c) "
                             "ON CONFLICT(workspace_id) DO UPDATE SET due_at='-infinity',lease_until='-infinity',lease_owner=NULL"),
                        {"w": self.workspace, "c": self.channel})
        self.service._row = Mock(side_effect=lambda db, w: db.execute(
            text("SELECT *, 'https://test.s20.online' AS crm_origin,crm_connection_id AS current_crm_connection_id,"
                 "'Test' AS name FROM alfa_max_settings WHERE workspace_id=:w"),
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

    def test_otp_page_in_background_requests_confirmation_without_password_login(self):
        cursor = self.run_worker({'projection_version': PROJECTION_VERSION}, browser_effect=AlfaBrowserError('otp_required'))
        self.assertTrue(cursor['session_control']['needs_check'])
        self.assertEqual(cursor['session_control']['phase'], 'checking')
        self.service.browser.login.assert_not_called()

    def test_completed_backfill_does_not_restart_on_every_poll(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 2},
                                 page_result=lambda request: {"messages": [], "has_more": request[2] == 0})
        self.assertEqual(cursor["1:receive"], 0)
        self.assertTrue(cursor["complete:1:receive"])
        self.service.browser.read_histories.reset_mock()
        cursor = self.run_worker(cursor)
        self.assertEqual(cursor["1:receive"], 0)
        self.service.browser.read_histories.assert_not_called()
        cursor = self.run_worker(cursor, after_seconds=16)
        requests = self.service.browser.read_histories.call_args.args[2]
        self.assertNotIn((1, "receive", 1), requests)

    def test_legacy_unfinished_history_keeps_its_checkpoint(self):
        cursor = self.run_worker({"1:receive": 9})
        self.assertEqual(cursor["projection_version"], PROJECTION_VERSION)
        self.assertEqual(cursor["1:receive"], 10)
        self.assertIn((1, "receive", 9), self.service.browser.read_histories.call_args.args[2])

    def test_initial_identity_repair_preserves_earlier_completed_history(self):
        cursor=self.run_worker({"1:receive":100,"complete:1:receive":False,
            "finished:1:receive":self.at.isoformat(),"error:1:receive":"history_gap"})
        self.assertTrue(cursor["complete:1:receive"])
        self.assertEqual(cursor["proof:1:receive"]["status"],"legacy")
        self.assertEqual(cursor["1:receive"],0)
        self.assertNotIn((1,"receive",100),self.service.browser.read_histories.call_args.args[2])

    def test_new_message_burst_reopens_backfill_near_the_head(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 0,
                                  "complete:1:receive": True, "head:1:receive": "old"},
                                 page_result=lambda request: {"messages": [self.row("10")], "has_more": True})
        self.assertEqual(cursor["1:receive"], 0)
        self.assertEqual(cursor["catchup:1:receive"], 1)
        self.assertTrue(cursor["complete:1:receive"])

    def test_new_bursts_do_not_restart_the_archive_before_it_reaches_old_clients(self):
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 5,
                                  "head:1:receive": "9"},
                                 page_result=lambda request: {"messages": [self.row("10")], "has_more": True})
        self.assertEqual(cursor["1:receive"], 5)  # Newly discovered catchup takes the next turn.
        self.assertEqual(cursor["catchup:1:receive"], 1)
        cursor = self.run_worker(cursor, after_seconds=3,
                                 page_result=lambda request: {"messages": [self.row("10")], "has_more": True})
        self.assertEqual(cursor["1:receive"], 6)
        self.assertEqual(cursor["catchup:1:receive"], 2)  # Saved 10 is not the previous boundary 9.
        cursor = self.run_worker(cursor, after_seconds=3,
                                 page_result=lambda request: {"messages": [self.row("9")], "has_more": True})
        self.assertEqual(cursor["catchup:1:receive"], 0)
        self.assertEqual(cursor["1:receive"], 7)

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

    def test_shared_incoming_sender_has_one_dialog_for_three_distinct_crm_children(self):
        second, third = uuid4(), uuid4()
        self.add_customer(second, 1, 8)
        self.add_customer(third, 1, 9)
        for customer, status in ((7, "archive"), (8, "client"), (9, "client")):
            self.customer_phone(customer, status=status,
                                parent="Второе написание родителя" if customer == 8 else "Первое написание родителя")
        row = self.row("300", customer=7)
        row["customers"] += [self.row("300", customer=8)["customers"][0],
                             self.row("300", customer=9)["customers"][0]]
        self.project(row)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 1)
        message = self.messages()[0]
        self.assertEqual(message["payload"]["crm_customer_ids"], [7, 8, 9])
        self.assertEqual(set(message["payload"]["crm_family_contacts"]),
                         {str(self.contact), str(second), str(third)})
        self.assertEqual(self.reader.listing(None, self.workspace)["conversations"][0]["unread_count"], 1)
        # Following rows can refer to any single child of the same proven peer.
        self.project(self.row("301", customer=9))  # Same text and minute, different source ID.
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 1)
        self.assertEqual(len(self.messages()), 2)
        self.project(row)
        self.assertEqual(len(self.messages()), 2)

    def test_a_shared_phone_without_a_shared_incoming_row_never_merges_chats(self):
        second = uuid4()
        self.add_customer(second, 1, 8)
        self.customer_phone(7, parent="Первый родитель")
        self.customer_phone(8, parent="Другой родитель")
        row = self.row("300", direction="out", actor="automation")
        row["customers"].append(self.row("300", customer=8)["customers"][0])
        self.project(row)
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 2)
        self.assertEqual(len(self.messages()), 2)

    def test_shared_incoming_row_with_different_phones_keeps_recipients_separate(self):
        second = uuid4()
        self.add_customer(second, 1, 8)
        self.customer_phone(7)
        self.customer_phone(8, phone="+79160000001")
        row = self.row("300")
        row["customers"].append(self.row("300", customer=8)["customers"][0])
        self.project(row)
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 2)

    def test_manual_crm_link_blocks_automatic_shared_parent_merge(self):
        second = uuid4()
        self.add_customer(second, 1, 8)
        self.customer_phone(7)
        self.customer_phone(8)
        self.project(self.row("200", customer=8))
        self.db.execute(text("UPDATE conversations SET crm_link_manual=true WHERE workspace_id=:w "
                             "AND contact_id=:contact"), {"w": self.workspace, "contact": second})
        row = self.row("300")
        row["customers"].append(self.row("300", customer=8)["customers"][0])
        self.project(row)
        self.assertEqual(self.reader.listing(None, self.workspace)["total"], 2)
        self.assertIsNone(self.db.execute(text("SELECT 1 FROM outbox_events WHERE workspace_id=:w "
            "AND topic=:topic LIMIT 1"), {"w": self.workspace,
                "topic": alfa_shared_parent.TOPIC}).scalar_one_or_none())

    def test_saved_source_copies_merge_without_deleting_provenance(self):
        second, third = uuid4(), uuid4()
        self.add_customer(second, 1, 8)
        self.add_customer(third, 1, 9)
        for customer in (7, 8, 9):
            self.customer_phone(customer)
            self.project(self.row("300", customer=customer))
        before = self.reader.listing(None, self.workspace)
        self.assertEqual((before["total"], before["unread"]), (3, 3))
        cursor = {}
        self.assertTrue(alfa_shared_parent.repair_batch(
            self.service, self.db, self.workspace, self.channel, self.snapshot, cursor))
        after = self.reader.listing(None, self.workspace)
        self.assertEqual((after["total"], after["unread"]), (1, 1))
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM source_records WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar_one(), 3)
        self.assertTrue(cursor["shared_parent_repair"]["complete"])

    def test_shared_parent_preference_keeps_all_children_and_active_status(self):
        phone, first, second = "+79160000000", str(uuid4()), str(uuid4())
        def card(contact, customer, status, parent):
            return {"contact_id": contact, "external_id": customer, "branch_id": 1,
                    "name": f"Ученик {customer}", "legal_name": parent, "status": status,
                    "legal_type": 1, "phones": [phone], "dob": None, "student_id": None,
                    "guard_piece": str(customer)}
        rows = {first: [card(first, 7, "archive", "Отец")],
                second: [card(second, 8, "client", "Мать")]}
        snapshot = {"today": self.at.date(), "timezone": "Europe/Moscow",
                    "connection_id": self.crm, "run": self.run, "version": 1}
        result = card_preference.choose(first, rows, {phone: rows[first] + rows[second]},
                                        {}, defaultdict(list), snapshot, verified_contacts=[first, second])
        self.assertEqual(result["status"], "client")
        self.assertEqual(result["contacts"], sorted([first, second]))
        self.assertIsNone(result["ambiguous"])

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
            cursor = self.run_worker(cursor, page_result=page, after_seconds=16 if cycle else 0)
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
        cursor = self.run_worker(cursor, page_result=page, after_seconds=16)
        self.assertEqual(len(self.messages()), len(expected))
        self.assertTrue(all(r[2] == 0 for r in self.service.browser.read_histories.call_args.args[2]))
        # A new source ID with an old timestamp is still imported when it appears in the feed.
        journals[(1, "receive")].pop()
        journals[(1, "receive")].insert(0, late)
        cursor = self.run_worker(cursor, page_result=page, after_seconds=16)
        self.assertEqual(len(self.messages()), len(expected) + 1)
        self.assertEqual(min(m["occurred_at"] for m in self.messages()).year, 1999)

    def test_unlinked_customers_with_equal_numbers_in_different_branches_stay_separate(self):
        self.published.return_value = None
        self.project(self.row("1", branch=1, customer=9))
        self.project(self.row("2", branch=2, customer=9))
        self.assertEqual(len({m["conversation_id"] for m in self.messages()}), 2)

    def test_raw_count_difference_is_not_a_missing_id_or_reimport(self):
        def page(request):
            return {"messages": [self.row("1")] if request == (1,"receive",0) else [], "has_more":False,
                    "total":2 if request[:2] == (1,"receive") else 0}
        cursor = self.run_worker({}, page_result=page)
        self.assertTrue(cursor["complete:1:receive"])
        report = history_coverage(self.db,self.workspace,self.channel,cursor,[1,2])
        self.assertEqual(report["streams"][0]["missing_rows"],0)
        self.assertEqual(report["streams"][0]["row_difference"],1)
        self.assertTrue(report["history_loaded"])
        self.assertFalse(report["totals_verified"])
        self.assertFalse(history_pending(cursor))
        for _ in range(3):
            cursor = self.run_worker(cursor, page_result=page)
            self.assertTrue(all(r[2] == 0 for r in self.service.browser.read_histories.call_args.args[2]))
            self.assertNotIn("paused:1:receive",cursor)
        self.assertEqual(len(self.messages()),1)

    def test_source_ambiguity_does_not_block_new_messages(self):
        def page(request):
            return {"messages":[self.row("1")] if request == (1,"receive",0) else [], "has_more":False,
                    "total":2 if request[:2] == (1,"receive") else 0}
        cursor=self.run_worker({},page_result=page)
        def fresh(request):
            return {"messages":[self.row("3",body="New live message")],"has_more":False,"total":3} if request==(1,"receive",0) else page(request)
        cursor=self.run_worker(cursor,page_result=fresh,after_seconds=16)
        self.assertEqual({m["payload"]["crm_record_id"] for m in self.messages()},{"1","3"})
        self.assertFalse(history_pending(cursor))
        self.assertTrue(history_coverage(self.db,self.workspace,self.channel,cursor,[1,2])["history_loaded"])

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
        self.assertTrue(report["history_loaded"])
        self.assertEqual(len(self.messages()), 2)

    def test_projection_failure_cannot_advance_past_missing_source_ids(self):
        def page(request):
            return {"messages": [self.row("20")] if request == (1, "receive", 4) else [], "has_more": True}
        cursor = {"projection_version": PROJECTION_VERSION, "1:receive": 4}
        with patch.object(self.service, "_project_page"):
            for _ in range(3):
                if cursor.get("retry_archive:1:receive"):
                    cursor["retry_archive:1:receive"]["next_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                cursor = self.run_worker(cursor, page_result=page)
                self.assertEqual(cursor["1:receive"], 4)
                self.assertEqual(cursor["error:1:receive"], "history_projection")
                self.assertEqual(cursor["missing_ids:1:receive"], ["20"])
        self.assertIn("paused:1:receive", cursor)
        cursor = self.run_worker(cursor, page_result=page, after_seconds=16)
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
        self.assertTrue(report["history_loaded"])
        self.assertFalse(report["totals_verified"])
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

    def test_manual_sync_does_not_cancel_an_active_worker_or_rewind_its_cursor(self):
        lease = uuid4()
        cursor = {"projection_version": PROJECTION_VERSION, "streams": ["1:receive"],
                  "1:receive": 12, "paused:1:receive": (datetime.now(UTC)+RECHECK_INTERVAL).isoformat()}
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:cursor AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "cursor": json.dumps(cursor)})
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id,lease_owner,lease_until) "
                             "VALUES (:w,:c,:lease,clock_timestamp()+interval '1 minute')"),
                        {"w": self.workspace, "c": self.channel, "lease": lease})
        result = self.service.sync(SimpleNamespace(user_id=self.user), self.workspace)
        self.assertTrue(result["already_running"])
        saved = self.db.execute(text("SELECT lease_owner FROM alfa_max_sync_queue WHERE workspace_id=:w"), {"w": self.workspace}).scalar_one()
        self.assertEqual(saved, lease)
        self.assertEqual(self.db.execute(text("SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar_one(), cursor)

    def test_unchanged_directory_snapshot_does_not_restart_saved_history_repair(self):
        self.project(self.row())
        for n in range(25):
            self.add_customer(uuid4(), 1, 100+n)
        cursor = {}
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        self.assertEqual(cursor["client_repair"]["processed"], 20)
        next_run = uuid4()
        self.db.execute(text("INSERT INTO alfa_sync_runs(workspace_id,id,connection_id,settings_version,requested_by,state) "
                             "VALUES (:w,:r,:c,1,:u,'succeeded')"), {"w": self.workspace, "r": next_run, "c": self.crm, "u": self.user})
        self.db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,status,contact_id) "
                             "SELECT workspace_id,:next,branch_id,external_id,name,status,contact_id FROM alfa_customers "
                             "WHERE workspace_id=:w AND run_id=:old"), {"w": self.workspace, "old": self.run, "next": next_run})
        self.snapshot["run"] = next_run
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        self.assertEqual(cursor["client_repair"]["processed"], 26)
        self.assertTrue(cursor["client_repair"]["complete"])

    def test_changed_customer_mapping_restarts_repair_and_updates_its_total(self):
        self.project(self.row())
        cursor = {}
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor)
        original = cursor["client_repair"]["mapping"]
        self.add_customer(uuid4(), 1, 99)
        self.assertTrue(repair_batch(self.service, self.db, self.workspace, self.channel, cursor))
        self.assertNotEqual(cursor["client_repair"]["mapping"], original)
        self.assertEqual(cursor["client_repair"]["processed"], 2)
        self.assertEqual(cursor["client_repair"]["total"], 2)

    def test_rescheduling_preserves_a_live_lease_for_the_same_connection(self):
        lease = uuid4()
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id,lease_owner,lease_until) "
                             "VALUES (:w,:c,:lease,clock_timestamp()+interval '1 minute')"),
                        {"w": self.workspace, "c": self.channel, "lease": lease})
        self.service._schedule(self.db, self.workspace, self.channel)
        self.assertEqual(self.db.execute(text("SELECT lease_owner FROM alfa_max_sync_queue WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar_one(), lease)

    def test_missing_crm_credentials_are_not_reported_as_an_active_loader(self):
        self.db.execute(text("UPDATE alfa_settings SET secret_ciphertext=NULL WHERE workspace_id=:w"), {"w": self.workspace})
        self.assertEqual(self.service.progress(None, self.workspace)["state"], "reauth_required")
        with self.assertRaises(AuthError) as error:
            self.service.sync(SimpleNamespace(user_id=self.user), self.workspace)
        self.assertEqual(error.exception.status, 409)

    def test_page_heartbeat_exposes_movement_before_source_rows_are_saved(self):
        reports = []
        def read(origin, session, requests, **kwargs):
            for request in requests:
                kwargs["on_progress"]()
                reports.append(self.service.progress(None, self.workspace)["worker"])
            return ([{"messages": [], "has_more": True} for request in requests], {})
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION, "1:receive": 4}, browser_effect=read)
        self.assertTrue(all(report["phase"] == "reading" for report in reports))
        self.assertEqual([report["completed_pages"] for report in reports], list(range(len(reports))))
        self.assertTrue(all(report["page"] == 0 for report in reports[:4]))
        self.assertEqual(reports[-1]["page"], 4)
        self.assertEqual(cursor["activity"]["phase"], "waiting")
        self.assertEqual(cursor["activity"]["completed_pages"], len(reports))

    def test_repair_reports_actual_customer_ids_and_positions_before_its_checkpoint_advances(self):
        self.project(self.row())
        cursor, events = {}, []
        repair_batch(self.service, self.db, self.workspace, self.channel, cursor, on_progress=events.append)
        self.assertEqual(events[0]["operation"], "check_client")
        self.assertEqual(events[0]["position"], 1)
        self.assertEqual(events[0]["client"]["name"], "Parent")
        self.assertEqual(events[0]["client"]["cards"][0]["customer_id"], "7")
        self.assertEqual(events[-1]["event"]["checked_messages"], 1)
        self.assertTrue(cursor["client_repair"]["complete"])

    def test_live_projection_exposes_real_client_and_record_before_commit_then_retains_confirmed_results(self):
        original, reports = self.service._project_page, []
        def project(*args, **kwargs):
            callback = kwargs["on_progress"]
            def report(value):
                callback(value)
                if value["operation"] == "save_message":
                    reports.append(self.service.progress(None, self.workspace, activity_only=True))
            return original(*args, **(kwargs | {"on_progress": report}))
        self.service._project_page = project
        cursor = self.run_worker({}, page_result=lambda req: {"messages": [self.row("777", direction="in" if req[1] == "receive" else "out")], "has_more": False, "total": 1})
        self.assertTrue(reports)
        self.assertTrue(all(r["worker"]["live"] for r in reports))
        self.assertEqual(reports[0]["worker"]["client"]["cards"][0]["customer_id"], "7")
        self.assertEqual(reports[0]["worker"]["record_id"], "777")
        self.assertNotIn("messages", reports[0])
        self.assertTrue(cursor["activity"]["recent"])
        self.assertTrue(all(e["committed"] for e in cursor["activity"]["recent"]))
        self.assertFalse(self.service.progress(None, self.workspace, activity_only=True)["worker"]["live"])

    def test_light_monitor_skips_history_counts_and_rejects_a_stale_worker_lease(self):
        lease = uuid4()
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id,lease_owner,lease_until) "
                             "VALUES (:w,:c,:lease,clock_timestamp()+interval '1 minute')"),
                        {"w": self.workspace, "c": self.channel, "lease": lease})
        self.service.monitor.begin(self.workspace, self.channel, lease, {}, {"client": {"name": "Actual"}})
        with patch.object(self.db, "execute", wraps=self.db.execute) as execute:
            report = self.service.progress(None, self.workspace, activity_only=True)
        self.assertTrue(report["worker"]["live"])
        self.assertFalse(any("count(" in str(c.args[0]).lower() for c in execute.call_args_list))
        self.db.execute(text("UPDATE alfa_max_sync_queue SET lease_owner=:other WHERE workspace_id=:w"), {"w": self.workspace, "other": uuid4()})
        self.assertFalse(self.service.progress(None, self.workspace, activity_only=True)["worker"]["live"])

    def test_chat_context_is_safe_metadata_for_message_members(self):
        self.membership["role"] = "operator"
        self.membership["sections"] = ["messages"]
        result = self.reader.context(SimpleNamespace(user_id=self.user), self.workspace)
        self.assertEqual(len(result["channels"]), 1)
        self.assertEqual(result["channels"][0]["kind"], "alfa_max_browser")
        self.assertEqual(len(result["branches"]), 2)
        self.assertTrue(result["can_write"])
        self.assertTrue(result["channels"][0]["enabled"])
        self.assertFalse(result["send_attachments"])
        self.assertNotIn("ciphertext", json.dumps(result))
        self.assertNotIn("test.s20.online", json.dumps(result))

    def test_observer_cannot_send_handle_or_relink_and_history_is_read_only(self):
        from center_messenger.auth_security import AuthError
        from center_messenger.chat_api import SendInput, LinkInput
        self.project(self.row())
        self.membership["role"] = "observer"
        conversation = self.conversation()["id"]
        actor = SimpleNamespace(user_id=self.user)
        self.assertFalse(self.reader.context(actor, self.workspace)["can_write"])
        self.assertFalse(self.reader.history(actor, self.workspace, conversation)["conversation"]["can_send"])
        self.assertFalse(self.reader.listing(actor, self.workspace)["conversations"][0]["can_send"])
        operations = [lambda: self.reader.send(actor, self.workspace, conversation, SendInput(request_id=uuid4(), text="Reply", seen_inbound_seq=1)),
                      lambda: self.reader.handle(actor, self.workspace, conversation, self.checkpoint(conversation)),
                      lambda: self.reader.link(actor, self.workspace, conversation, LinkInput(contact_id=None))]
        for operation in operations:
            with self.assertRaises(AuthError) as error:
                operation()
            self.assertEqual(error.exception.status, 403)
        self.assertEqual(len(self.messages()), 1)

    def test_read_only_client_projection_is_limited_to_contacts_with_accessible_chats(self):
        from center_messenger.auth_security import AuthError
        self.project(self.row())
        other = uuid4()
        self.add_customer(other, 1, 88)
        self.snapshot.update(today=self.at.date(), origin="https://test.s20.online", version=1, finished_at=self.at)
        self.membership["role"] = "observer"
        self.membership["sections"] = ["messages", "overview"]
        @contextmanager
        def denied(*args, **kwargs):
            raise AuthError(403, "Admin only")
            yield
        @contextmanager
        def chat_read(*args, **kwargs):
            with self.db.begin_nested():
                yield self.db
        directory = DirectoryReader(SimpleNamespace(_transaction=denied, chat_transaction=chat_read, accounts=self.accounts))
        with self.assertRaises(AuthError):
            directory.listing(None, self.workspace)
        result = directory.listing(None, self.workspace, chat_only=True)
        self.assertEqual([row["id"] for row in result["contacts"]], [str(self.contact)])
        detail = directory.detail(None, self.workspace, self.contact, conversation=self.conversation()["id"])
        self.assertEqual(detail["id"], str(self.contact))
        with self.assertRaises(AuthError) as wrong:
            directory.detail(None, self.workspace, other, conversation=self.conversation()["id"])
        self.assertEqual(wrong.exception.status, 404)
        self.membership["role"] = "operator"
        with self.assertRaises(AuthError) as operator:
            directory.listing(None, self.workspace, chat_only=True)
        self.assertEqual(operator.exception.status, 403)

    def test_family_routes_do_not_read_timeline_even_with_missing_queue_projection(self):
        from sqlalchemy import event
        from center_messenger import chat_api
        self.project(self.row())
        conversation = self.conversation()["id"]
        self.db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w AND topic='chat.list.state'"),
                        {"w": self.workspace})
        directory = SimpleNamespace(prepare=Mock(return_value={"id": str(self.contact), "cache": {"ready": True}}))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(directory=directory)))
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lower())
        event.listen(self.db, "before_cursor_execute", observe)
        try:
            with patch.object(chat_api, "components", return_value=(None, self.reader)), \
                 patch.object(chat_api, "identity", return_value=SimpleNamespace(user_id=self.user)):
                value = chat_api.chat_family(self.workspace, conversation, request, cached_only=True, compact=True, known_version=None)
                self.assertEqual(value["id"], str(self.contact))
                chat_api.refresh_chat_family(self.workspace, conversation, request, compact=True)
        finally:
            event.remove(self.db, "before_cursor_execute", observe)
        self.assertEqual(directory.prepare.call_count, 2)
        self.assertTrue(directory.prepare.call_args.kwargs["force"])
        self.assertFalse(any("from messages" in q or "join messages" in q or "message_changes" in q for q in statements))
        self.assertTrue(any("from conversations" in q for q in statements))

    def test_family_target_resolves_existing_alias_and_preserves_manual_binding(self):
        self.project(self.row())
        canonical = self.conversation()["id"]
        alias = uuid4()
        self.db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id) "
            "VALUES(:w,:id,:c,:contact,'alfa-customer:1:7')"),
            {"w": self.workspace, "id": alias, "c": self.channel, "contact": self.contact})
        self.assertEqual(self.reader._family_conversation(self.db, self.workspace, alias)["id"], canonical)
        self.db.execute(text("UPDATE conversations SET crm_link_manual=true WHERE workspace_id=:w AND id=:id"),
                        {"w": self.workspace, "id": alias})
        self.assertEqual(self.reader._family_conversation(self.db, self.workspace, alias)["id"], alias)

    def test_family_target_rejects_foreign_workspace_group_and_unconfigured_channel(self):
        self.project(self.row())
        conversation = self.conversation()["id"]
        with self.assertRaises(AuthError) as error:
            self.reader._family_conversation(self.db, uuid4(), conversation)
        self.assertEqual(error.exception.status, 404)
        for query in ("UPDATE conversations SET kind='group' WHERE workspace_id=:w",
                      "DELETE FROM alfa_max_settings WHERE workspace_id=:w"):
            with self.db.begin_nested() as tx:
                self.db.execute(text(query), {"w": self.workspace})
                with self.assertRaises(AuthError) as error:
                    self.reader._family_conversation(self.db, self.workspace, conversation)
                self.assertEqual(error.exception.status, 404)
                tx.rollback()

    def test_family_route_checks_membership_and_link_before_preparing_private_card(self):
        from center_messenger import chat_api
        self.project(self.row("99", customer=99))
        conversation = self.conversation()["id"]
        directory = SimpleNamespace(prepare=Mock())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(directory=directory)))
        with patch.object(chat_api, "components", return_value=(None, self.reader)), \
             patch.object(chat_api, "identity", return_value=SimpleNamespace(user_id=self.user)):
            with self.assertRaises(AuthError) as error:
                chat_api.chat_family(self.workspace, conversation, request)
            self.assertEqual(error.exception.status, 404)
            with patch.object(self.reader.max, "chat_transaction", side_effect=AuthError(403, "No access")):
                with self.assertRaises(AuthError) as denied:
                    chat_api.chat_family(self.workspace, conversation, request)
                self.assertEqual(denied.exception.status, 403)
        directory.prepare.assert_not_called()

    def test_summary_counts_visible_history_and_first_confirmed_human_response(self):
        from center_messenger.cabinet_summary import summary
        self.project(self.row("10"), branch=1)
        self.project(self.row("11", branch=2), branch=2)
        self.project(self.row("12", direction="out", actor="automation", at=self.at+timedelta(minutes=1)),
                     self.row("13", direction="out", actor="human", at=self.at+timedelta(minutes=3)),
                     self.row("14", direction="out", actor="human", at=self.at+timedelta(minutes=5)),
                     self.row("15", at=self.at-timedelta(days=8), body="Previous week"))
        report = summary(self.reader.max, None, self.workspace, now=self.at+timedelta(hours=12))
        self.assertEqual(report["incoming"], 1)
        self.assertEqual(report["previous_incoming"], 1)
        self.assertEqual(sum(day["incoming"] for day in report["days"]), 1)
        self.assertEqual(sum(branch["incoming"] for branch in report["branches"]), 1)
        self.assertEqual(sum(operator["answered"] for operator in report["operators"]), 2)
        self.assertEqual(report["average_response_seconds"], 180)
        self.assertEqual(report["waiting"], 0)
        self.assertEqual(report["new_leads"], 0)

    def test_summary_uses_workspace_calendar_days_and_rejects_operator(self):
        from center_messenger.cabinet_summary import summary
        from center_messenger.auth_security import AuthError
        boundary = datetime(2026, 10, 17, 21, tzinfo=UTC)
        self.project(self.row("1", at=boundary-timedelta(seconds=1)), self.row("2", at=boundary, body="New day"))
        report = summary(self.reader.max, None, self.workspace, now=self.at+timedelta(hours=12))
        self.assertEqual(report["incoming"], 1)
        self.assertEqual(report["previous_incoming"], 1)
        self.assertEqual(report["days"][0]["date"], "2026-10-18")
        self.assertEqual(report["days"][0]["incoming"], 1)
        self.assertIsNone(report["average_response_seconds"])
        self.membership["role"] = "operator"
        with self.assertRaises(AuthError) as error:
            summary(self.reader.max, None, self.workspace, now=self.at)
        self.assertEqual(error.exception.status, 403)

    def test_summary_ignores_old_projection_and_rebuilds_missing_state_correctly(self):
        from center_messenger.cabinet_summary import summary
        from center_messenger.chat_queue import PROJECTION_VERSION as queue_version
        self.project(self.row("1"))
        self.db.execute(text("UPDATE outbox_events SET payload=payload||CAST(:p AS jsonb) "
            "WHERE workspace_id=:w AND topic='chat.list.state'"),
            {"w": self.workspace, "p": json.dumps({"projection_version": queue_version - 1, "needs_reply": False})})
        self.assertEqual(summary(self.reader.max, None, self.workspace, now=self.at)["waiting"], 1)
        self.db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w AND topic='chat.list.state'"), {"w": self.workspace})
        self.assertEqual(summary(self.reader.max, None, self.workspace, now=self.at)["waiting"], 1)

    def test_summary_carries_preperiod_wait_but_not_preperiod_answer_into_first_reply(self):
        from center_messenger.cabinet_summary import summary
        now = self.at + timedelta(hours=12)
        boundary = datetime(2026, 10, 17, 21, tzinfo=UTC)
        self.project(self.row("1", at=boundary-timedelta(minutes=10)),
                     self.row("2", direction="out", at=boundary+timedelta(minutes=2)),
                     self.row("3", direction="out", at=boundary+timedelta(minutes=5)))
        report = summary(self.reader.max, None, self.workspace, now=now)
        self.assertEqual(report["average_response_seconds"], 720)
        self.project(self.row("4", direction="out", at=boundary-timedelta(minutes=5)))
        report = summary(self.reader.max, None, self.workspace, now=now)
        self.assertIsNone(report["average_response_seconds"])
        self.assertEqual(report["operators"][0]["answered"], 2)

    def test_summary_same_timestamp_uses_source_order_and_ignores_failed_or_automatic_answers(self):
        from center_messenger.cabinet_summary import summary
        self.project(self.row("1", direction="out"), self.row("2"),
                     self.row("3", direction="out", actor="automation"),
                     dict(self.row("4", direction="out"), failed=True),
                     self.row("5", direction="out", at=self.at+timedelta(seconds=60)),
                     self.row("6", direction="out", at=self.at+timedelta(seconds=120)))
        report = summary(self.reader.max, None, self.workspace, now=self.at+timedelta(hours=1))
        self.assertEqual(report["average_response_seconds"], 60)
        self.assertEqual(report["operators"][0]["answered"], 3)

    def test_summary_previous_week_starts_at_local_midnight_across_dst(self):
        from center_messenger.cabinet_summary import summary
        self.membership["timezone"] = "Europe/Berlin"
        # Current week begins after the autumn transition; previous week before it.
        now = datetime(2026, 11, 1, 12, tzinfo=UTC)
        previous_start = datetime(2026, 10, 18, 22, tzinfo=UTC)
        self.project(self.row("1", at=previous_start + timedelta(minutes=30)))
        self.assertEqual(summary(self.reader.max, None, self.workspace, now=now)["previous_incoming"], 1)

    def test_summary_excludes_unconfirmed_and_ambiguous_native_sends(self):
        from center_messenger.cabinet_summary import summary
        self.project(self.row("1"),
                     self.row("2", direction="out", at=self.at+timedelta(seconds=60)),
                     self.row("3", direction="out", at=self.at+timedelta(seconds=120)),
                     self.row("4", direction="out", at=self.at+timedelta(seconds=180)))
        for record, patch_payload in (("2", {"delivery_state": "unknown"}),
                                      ("3", {"possible_send_command_id": str(uuid4())})):
            self.db.execute(text("UPDATE messages SET payload=payload||CAST(:p AS jsonb) WHERE workspace_id=:w "
                "AND payload->>'crm_record_id'=:record"),
                {"w": self.workspace, "p": json.dumps(patch_payload), "record": record})
        refresh_unread(self.db, self.workspace, self.conversation()["id"])
        result = summary(self.reader.max, None, self.workspace, now=self.at+timedelta(hours=1))
        self.assertEqual(result["average_response_seconds"], 180)
        self.assertEqual(result["operators"][0]["answered"], 1)

    def test_settings_change_during_browser_read_cannot_publish_stale_history(self):
        def changed_settings(origin, session, requests, **kwargs):
            self.db.execute(text("UPDATE alfa_max_settings SET version=version+1 WHERE workspace_id=:w"),
                            {"w": self.workspace})
            return ([{"messages": [self.row()], "has_more": False} for _ in requests], {})
        cursor = self.run_worker({"projection_version": PROJECTION_VERSION}, browser_effect=changed_settings)
        self.assertTrue(cursor.pop("client_repair")["complete"])
        cursor.pop("activity", None)
        self.assertEqual(cursor['streams'], ["1:receive","1:sent","2:receive","2:sent"])
        self.assertEqual(cursor['projection_version'], PROJECTION_VERSION)
        for key in cursor['streams']:
            self.assertEqual(cursor.get(key, 0), 0)
            self.assertEqual(cursor.get(f'scan_pages:{key}', 0), 0)
            self.assertNotIn(f'live_checked:{key}', cursor)
            self.assertNotIn(f'complete:{key}', cursor)
        self.assertEqual(self.db.execute(text('SELECT session_ciphertext FROM alfa_max_settings WHERE workspace_id=:w'),
                                         {'w': self.workspace}).scalar_one(), 'test')
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
        self.assertTrue(report["history_loaded"])
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
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, self.checkpoint(conversation))
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

    def test_outgoing_echo_uses_original_sending_branch_after_customer_routing_repair(self):
        self.add_customer(self.contact, 2, 7)
        self.project(self.row("1"))
        local = self.local_reply(self.conversation()["id"])
        self.db.execute(text("UPDATE messages SET payload=payload||CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                        {"w": self.workspace, "id": local, "p": json.dumps({"crm_branch": 2})})
        self.db.execute(text("UPDATE send_commands SET state='unknown' WHERE workspace_id=:w"), {"w": self.workspace})
        self.project(self.row("20", direction="out", branch=1, body="Reply", at=self.at + timedelta(seconds=10)))
        self.assertEqual(self.db.execute(text("SELECT state FROM send_commands WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), "unknown")
        self.project(self.row("21", direction="out", branch=2, body="Reply", at=self.at + timedelta(seconds=10)), branch=2)
        self.assertEqual(len(self.messages()), 3)
        self.assertEqual(self.db.execute(text("SELECT message_id FROM source_records WHERE workspace_id=:w "
                                             "AND external_record_id='out:2:21'"), {"w": self.workspace}).scalar(), local)
        self.assertEqual(self.db.execute(text("SELECT state FROM send_commands WHERE workspace_id=:w"),
                                         {"w": self.workspace}).scalar(), "accepted")

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
        self.assertTrue(history_coverage(self.db, self.workspace, self.channel, cursor, [1, 2, 3, 4])["history_loaded"])
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
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, self.checkpoint(conversation))
        self.project(self.row("2", body="Another question"))
        listing = self.reader.listing(None, self.workspace)
        self.assertTrue(listing["conversations"][0]["unread"])
        self.assertEqual(listing["conversations"][0]["unread_count"], 1)

    def test_unified_queue_counts_dialogs_beyond_the_loaded_page_and_honors_explicit_filters(self):
        self.project(self.row("1"), self.row("2", body="Second question"))
        for n in range(35):
            self.add_customer(uuid4(), 1, 100+n)
            self.project(self.row(str(100+n), customer=100+n, body=f"Lead {n}"))
        self.db.execute(text("UPDATE alfa_customers SET status='lead' WHERE workspace_id=:w"), {"w": self.workspace})
        listing = self.reader.listing(None, self.workspace)
        self.assertEqual(len(listing["conversations"]), 30)
        self.assertTrue(listing["has_more"])
        self.assertEqual(listing["unread"], 36)
        self.assertEqual(self.reader.listing(None, self.workspace, page=1)["unread"], 36)
        self.assertEqual(self.reader.listing(None, self.workspace, status="client")["unread"], 0)
        self.assertEqual(self.reader.listing(None, self.workspace, branch=2)["unread"], 0)
        self.assertEqual(self.reader.listing(None, self.workspace, connection=uuid4())["unread"], 0)
        self.assertEqual(self.reader.listing(None, self.workspace, query="Not present")["unread"], 0)

    def test_unified_queue_counter_honors_handled_messages_and_confirmed_replies(self):
        self.project(self.row("1"), self.row("2", body="Second question"))
        self.db.execute(text("UPDATE alfa_customers SET status='lead' WHERE workspace_id=:w"), {"w": self.workspace})
        self.assertEqual(self.reader.listing(None, self.workspace)["conversations"][0]["unread_count"], 2)
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, self.conversation()["id"], self.checkpoint(self.conversation()["id"]))
        self.assertEqual(self.reader.listing(None, self.workspace)["unread"], 0)
        self.project(self.row("3", body="New question"))
        self.assertEqual(self.reader.listing(None, self.workspace, view="unread")["conversations"][0]["unread_count"], 1)
        self.project(self.row("4", direction="out", actor="automation", at=self.at+timedelta(minutes=1)))
        self.assertEqual(self.reader.listing(None, self.workspace)["conversations"][0]["unread_count"], 1)
        self.project(self.row("5", direction="out", at=self.at+timedelta(minutes=2)))
        self.assertEqual(self.reader.listing(None, self.workspace)["unread"], 0)

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
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, canonical, self.checkpoint(canonical))
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

    def test_card_final_401_blocks_dependent_sections_and_settings_resume_keeps_cache(self):
        from center_messenger.alfa_client import AlfaError, AlfaSettingsInput
        reader, worker, _ = self.family_worker_fixture(include_groups=True)
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,refreshed_at) "
            "VALUES(:w,:c,1,7,1,123.45,now()-interval '1 hour')"), {"w":self.workspace,"c":self.crm})
        worker.schedule(self.workspace)
        job = worker.claim(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance', side_effect=AlfaError('unauthorized',401)):
            worker.process(self.workspace,job)
        self.assertIsNone(worker.claim(self.workspace))
        cache = reader.detail(None,self.workspace,self.contact)['cache']
        self.assertTrue(cache['paused']);self.assertFalse(cache['refreshing'])
        self.assertEqual(cache['failures'][0]['code'],'credentials_required')
        self.assertIsNone(cache['retry_at'])
        self.assertEqual(str(self.family_facts()['balance']),'123.45')
        before = [(m['id'],m['payload']) for m in self.messages()]
        old = worker.connections._row(self.db,self.workspace)
        self.db.execute(text("UPDATE alfa_settings SET api_email='test@example.org' WHERE workspace_id=:w"),{'w':self.workspace})
        worker.connections.save(SimpleNamespace(user_id=self.user),self.workspace,AlfaSettingsInput(
            origin=old['origin'],email='test@example.org',api_key='synthetic-api-key-12345'))
        self.assertEqual(str(self.family_facts()['balance']),'123.45')
        self.assertEqual(self.family_facts()['settings_version'],2)
        resumed = worker.claim(self.workspace)
        self.assertIsNotNone(resumed);self.assertEqual(resumed['version'],2)
        self.assertNotIn('checkpoint',resumed)
        self.assertEqual(before,[(m['id'],m['payload']) for m in self.messages()])

    def test_card_403_blocks_only_one_branch_operation_and_force_can_recheck(self):
        from center_messenger.alfa_client import AlfaError
        from center_messenger import group_links
        reader,worker,_=self.family_worker_fixture(include_groups=True)
        self.project(self.row());worker.schedule(self.workspace)
        first=worker.claim(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',side_effect=AlfaError('forbidden',403)):
            worker.process(self.workspace,first)
        second=worker.claim(self.workspace)
        self.assertIsNotNone(second);self.assertEqual(second['topic'],group_links.TOPIC)
        with patch('center_messenger.group_links.fetch',return_value=({},[])):
            worker.process(self.workspace,second)
        self.assertIsNone(worker.claim(self.workspace))
        reader.prepare(None,self.workspace,self.contact,conversation=self.conversation()['id'],force=True)
        retry=worker.claim(self.workspace)
        self.assertIsNotNone(retry);self.assertEqual(retry['topic'],'crm.card.refresh')
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,retry)
        from center_messenger.card_jobs import blocks
        self.assertFalse(blocks(self.db,self.workspace,self.crm,1))

    def test_card_429_blocks_sibling_jobs_until_the_source_deadline(self):
        from center_messenger.alfa_client import AlfaError
        reader,worker,_=self.family_worker_fixture(include_groups=True)
        self.project(self.row());worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',side_effect=AlfaError('rate_limited',429,'900')):
            worker.process(self.workspace,worker.claim(self.workspace))
        self.assertIsNone(worker.claim(self.workspace))
        data=reader.detail(None,self.workspace,self.contact)['cache']
        self.assertFalse(data['paused']);self.assertEqual(data['failures'][0]['http_status'],429)
        self.assertGreater(datetime.fromisoformat(data['retry_at']),datetime.now(UTC)+timedelta(seconds=895))

    def test_card_checkpoint_survives_late_page_failure_and_worker_restart(self):
        from center_messenger.alfa_client import AlfaError
        from center_messenger.family_cache import FamilyCacheWorker
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,refreshed_at) "
            "VALUES(:w,:c,1,7,1,123.45,now()-interval '1 hour')"),{'w':self.workspace,'c':self.crm})
        worker.schedule(self.workspace)
        wire=Mock();calls=[];failed=False
        def call(path,body):
            nonlocal failed
            calls.append((path,dict(body)))
            if path.endswith('/customer/index'):
                items=[{'id':7,'balance':'450.00','paid_lesson_count':3}];total=1
            elif '/customer-tariff/' in path:
                items=[] if body['dead'] else [{'id':40,'customer_id':7,'tariff_id':9,'balance':'200'}];total=len(items)
            else:
                if body['page']==2 and not failed:
                    failed=True
                    raise AlfaError('network')
                items=[{'id':9+body['page'],'name':'Tariff','type':1}];total=3
            return {'items':items,'total':total,'page':body['page']}
        wire.call.side_effect=call;worker.connections.client_factory.return_value=wire
        # Three finance pages are saved; tariffs wait for another bounded turn.
        worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(str(self.family_facts()['balance']),'123.45')
        checkpoint=self.family_jobs()[0]['payload']['checkpoint']
        self.assertEqual(checkpoint['pages'],3)
        restarted=FamilyCacheWorker(worker.connections)
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]):
            # Old lesson jobs can run before the yielded finance turn.
            for _ in range(5):
                job=restarted.claim(self.workspace)
                if job is None:break
                restarted.process(self.workspace,job)
                if failed:break
        self.assertTrue(failed)
        self.assertEqual(str(self.family_facts()['balance']),'123.45')
        self.assertEqual(self.family_jobs()[0]['payload']['checkpoint']['pages'],5)
        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{available_at}',to_jsonb((now()-interval '1 second')::text)) "
            "WHERE workspace_id=:w AND topic='crm.card.refresh'"),{'w':self.workspace})
        restarted=FamilyCacheWorker(worker.connections)
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]):
            for _ in range(6):
                job=restarted.claim(self.workspace)
                if job is None:break
                restarted.process(self.workspace,job)
                if self.family_jobs()[0]['state']=='published':break
        self.assertEqual(str(self.family_facts()['balance']),'450.00')
        self.assertNotIn('checkpoint',self.family_jobs()[0]['payload'])
        self.assertEqual(len([p for p,b in calls if p.endswith('/customer/index')]),1)
        self.assertEqual([b['page'] for p,b in calls if p.endswith('/tariff/index')],[0,1,2,2])

    def test_card_manual_request_during_checkpoint_is_not_lost(self):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row());worker.schedule(self.workspace)
        job=worker.claim(self.workspace)
        self.db.execute(text("UPDATE outbox_events SET payload=payload||CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':job['id'],'p':json.dumps({'checkpoint':{'pages':1}})})
        reader.prepare(None,self.workspace,self.contact,conversation=self.conversation()['id'],force=True)
        worker.finish(self.db,self.workspace,job)
        self.assertEqual(self.family_jobs()[0]['state'],'pending')
        self.assertNotIn('checkpoint',self.family_jobs()[0]['payload'])
        self.assertIsNotNone(worker.claim(self.workspace))

    def test_card_expired_lease_cannot_save_a_page_or_replace_good_facts(self):
        _,worker,_=self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,refreshed_at) "
            "VALUES(:w,:c,1,7,1,123.45,now()-interval '1 hour')"),{'w':self.workspace,'c':self.crm})
        worker.schedule(self.workspace);job=worker.claim(self.workspace)
        wire=Mock()
        def call(path,body):
            self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{lease_until}',to_jsonb((now()-interval '1 second')::text)) "
                "WHERE workspace_id=:w AND id=:id"),{'w':self.workspace,'id':job['id']})
            return {'items':[{'id':7,'balance':'9999'}],'total':1,'page':0}
        wire.call.side_effect=call;worker.connections.client_factory.return_value=wire
        worker.process(self.workspace,job)
        self.assertNotIn('checkpoint',self.family_jobs()[0]['payload'])
        self.assertEqual(str(self.family_facts()['balance']),'123.45')

    def test_card_rate_limit_uses_the_shared_host_clock_without_sending_http(self):
        from center_messenger.alfa_service import AlfaConnections
        from center_messenger.alfa_client import AlfaError
        connections=AlfaConnections(self.accounts,Mock(),Mock())
        with self.assertRaises(AlfaError):
            with connections._host_gate('https://test.s20.online'):
                raise AlfaError('rate_limited',429,'900')
        self.db.execute(text("UPDATE alfa_request_leases SET lease_until=now()-interval '1 second'"))
        with self.assertRaises(AlfaError) as caught:
            with connections._host_gate('https://test.s20.online') as gate:
                gate()
        self.assertEqual(caught.exception.status,429)
        self.assertGreater(float(caught.exception.retry_after),895)

    def test_group_checkpoint_publishes_memberships_only_after_all_metadata_is_verified(self):
        from center_messenger import group_links
        _,worker,_=self.family_worker_fixture(include_groups=True)
        group_links.enqueue(self.db,self.workspace,self.snapshot,contact=self.contact)
        job=worker.claim(self.workspace);self.assertEqual(job['topic'],group_links.TOPIC)
        wire=Mock();calls=[]
        def call(path,body):
            calls.append((path,dict(body)))
            items=([{'id':100+n,'customer_id':7,'group_id':10+n} for n in range(4)]
                   if '/cgi/customer' in path else [{'id':body['id'],'name':'Group'}])
            return {'items':items,'total':len(items),'page':body['page']}
        wire.call.side_effect=call;worker.connections.client_factory.return_value=wire
        worker.process(self.workspace,job)
        count=lambda:self.db.execute(text('SELECT count(*) FROM alfa_memberships WHERE workspace_id=:w'),{'w':self.workspace}).scalar()
        self.assertEqual(count(),0)
        worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(count(),4)
        self.assertEqual(len([p for p,b in calls if '/cgi/customer' in p]),1)

    def test_card_quota_and_aging_let_groups_run_under_finance_pressure(self):
        from center_messenger import group_links
        _,worker,_=self.family_worker_fixture(include_groups=True)
        self.project(self.row());worker.schedule(self.workspace)
        observed=[]
        for _ in range(5):
            job=worker.claim(self.workspace);observed.append(job['topic']);worker.yield_job(self.workspace,job)
        self.assertIn(group_links.TOPIC,observed)
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('manual',true) "
            "WHERE workspace_id=:w AND topic='crm.card.refresh'"),{'w':self.workspace})
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('served_at',now()-interval '16 minutes') "
            "WHERE workspace_id=:w AND topic=:t"),{'w':self.workspace,'t':group_links.TOPIC})
        self.assertEqual(worker.claim(self.workspace)['topic'],group_links.TOPIC)

    def test_targeted_preference_catalog_preserves_candidates_without_loading_other_clients(self):
        policy,_,_,active,_,_=self.preferred_card_fixture()
        unrelated=uuid4();self.add_customer(unrelated,1,12345)
        self.db.execute(text("UPDATE alfa_customers SET phones='[\"+79999999999\"]' WHERE workspace_id=:w AND contact_id=:c"),
            {'w':self.workspace,'c':unrelated})
        full=policy._catalog(self.db,self.workspace,self.snapshot)
        bounded=policy._catalog(self.db,self.workspace,self.snapshot,[self.contact])
        self.assertNotIn(str(unrelated),bounded[0]);self.assertIn(str(active),bounded[0])
        self.assertEqual(policy._cohort(str(self.contact),*full),policy._cohort(str(self.contact),*bounded))

    def test_client_audit_reports_matches_without_message_bodies_or_writes(self):
        from center_messenger.client_audit import inspect
        _,worker,_=self.family_worker_fixture()
        self.db.execute(text("UPDATE alfa_customers SET legal_name='Кашук Мария Васильевна' WHERE workspace_id=:w"),{'w':self.workspace})
        self.project(self.row(body='private message contents'))
        before=self.db.execute(text('SELECT count(*) FROM outbox_events WHERE workspace_id=:w'),{'w':self.workspace}).scalar()
        result=inspect(self.db,self.workspace,'Кашук Мария Васильевна')
        self.assertEqual(result['status'],'found');self.assertEqual(len(result['matches']),1)
        self.assertEqual(result['conversations'][0]['messages']['visible'],1)
        self.assertNotIn('private message contents',json.dumps(result,default=str))
        foreign=inspect(self.db,uuid4(),'Кашук Мария Васильевна')
        self.assertFalse(foreign['matches']);self.assertFalse(foreign['conversations']);self.assertFalse(foreign['jobs'])
        with patch.object(DirectoryReader,'_published',return_value=None):
            self.assertEqual(inspect(self.db,uuid4(),'Кашук Мария Васильевна')['status'],'directory_unavailable')
        self.assertEqual(before,self.db.execute(text('SELECT count(*) FROM outbox_events WHERE workspace_id=:w'),{'w':self.workspace}).scalar())

    def family_worker_fixture(self, *, include_groups=False):
        from center_messenger.alfa_service import AlfaConnections
        from center_messenger.family_cache import FamilyCacheWorker
        reader, student = self.family_balance_fixture()
        self.db.execute(text("INSERT INTO alfa_sync_control(workspace_id,connection_id,requested_by,published_id) "
            "VALUES (:w,:c,:u,:r)"), {'w':self.workspace,'c':self.crm,'u':self.user,'r':self.run})
        vault = Mock()
        vault.open.return_value = {'token':'synthetic'}
        vault.seal.return_value = 'synthetic-encrypted'
        connections = AlfaConnections(self.accounts, vault, Mock())
        @contextmanager
        def gate(*args):
            yield Mock()
        connections._host_gate = gate
        worker = FamilyCacheWorker(connections)
        if not include_groups:
            # Finance/lesson regressions isolate this independent queue. The
            # group lifecycle tests below enable all normal worker capabilities.
            stub=patch('center_messenger.group_links.enqueue',return_value=0)
            stub.start();self.addCleanup(stub.stop)
        return reader, worker, student

    def catalog_worker(self, handler):
        from center_messenger.catalog_review import CatalogReviewWorker
        worker, client = self.active_review_worker(handler)
        return CatalogReviewWorker(worker.connections), client

    def catalog_rows(self):
        from center_messenger.catalog_review import TOPICS
        return list(self.db.execute(text('SELECT topic,state,payload FROM outbox_events WHERE workspace_id=:w '
            'AND topic=ANY(CAST(:t AS text[])) ORDER BY topic,event_key'),{'w':self.workspace,'t':list(TOPICS)}).mappings())

    def test_catalog_six_hour_cycle_only_reads_branches_statuses_and_keeps_completed_journals(self):
        original={'sync_policy':1,'streams':['1:receive','1:sent'],'complete:1:receive':True,
            'complete:1:sent':True,'finished:1:receive':self.at.isoformat(),'2:sent':17}
        self.db.execute(text('UPDATE alfa_max_settings SET history_cursor=CAST(:p AS jsonb) WHERE workspace_id=:w'),
            {'w':self.workspace,'p':json.dumps(original)})
        def handler(path,body):
            self.assertNotIn('customer',path);self.assertNotIn('lesson',path)
            items=[{'id':n,'name':f'Branch {n}'} for n in (1,2,3)] if '/branch/index' in path else [{'id':1,'name':'Updated status'}]
            return {'items':items,'total':len(items),'page':body['page']}
        worker,client=self.catalog_worker(handler)
        for _ in range(7):self.assertTrue(worker.step(self.workspace))
        self.assertFalse(worker.step(self.workspace));self.assertEqual(client.call.call_count,7)
        self.assertEqual(len(self.catalog_rows()),7)
        self.assertTrue(all(r['state']=='published' for r in self.catalog_rows()))
        saved=self.db.execute(text('SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one()
        self.assertEqual(saved,original)
        cursor,_=plan_history(saved,[1,2,3],self.at+timedelta(days=1))
        self.assertTrue(cursor['complete:1:receive']);self.assertEqual(cursor['2:sent'],17)
        self.assertFalse(cursor.get('complete:3:sent',False))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_statuses WHERE workspace_id=:w'),{'w':self.workspace}).scalar(),6)
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('next_at',now()-interval '1 second') "
            "WHERE workspace_id=:w AND topic LIKE 'crm.catalog.%'"),{'w':self.workspace})
        for _ in range(7):self.assertTrue(worker.step(self.workspace))
        self.assertEqual(client.call.call_count,14)
        self.assertEqual(self.messages(),[])

    def test_catalog_checkpoint_keeps_old_branches_until_every_page_is_valid(self):
        from center_messenger.alfa_client import AlfaError
        fail=[True];calls=[]
        def handler(path,body):
            calls.append((path,body['page']))
            if '/branch/index' in path:
                if body['page']==3 and fail[0]:raise AlfaError('network')
                return {'items':[{'id':body['page']+10,'name':'New branch'}], 'total':5,'page':body['page']}
            return {'items':[],'total':0,'page':body['page']}
        worker,client=self.catalog_worker(handler)
        branches=lambda:list(self.db.execute(text('SELECT external_id FROM alfa_branches WHERE workspace_id=:w AND available ORDER BY external_id'),{'w':self.workspace}).scalars())
        self.assertTrue(worker.step(self.workspace));self.assertEqual(branches(),[1,2])
        # The fixture holds one outer transaction (now() is frozen); make the
        # yielded turn due before simulating the next real worker transaction.
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('available_at',now()-interval '1 second') "
            "WHERE workspace_id=:w AND topic='crm.catalog.branches'"),{'w':self.workspace})
        self.assertTrue(worker.step(self.workspace));self.assertEqual(branches(),[1,2])
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('available_at',now()-interval '1 second') "
            "WHERE workspace_id=:w AND topic='crm.catalog.branches'"),{'w':self.workspace})
        fail[0]=False
        from center_messenger.catalog_review import CatalogReviewWorker
        self.assertTrue(CatalogReviewWorker(worker.connections).step(self.workspace))
        self.assertEqual(branches(),[10,11,12,13,14])
        self.assertEqual([page for path,page in calls if '/branch/index' in path],[0,1,2,3,3,4])

    def test_catalog_forbidden_status_isolated_from_other_branches_and_max_session(self):
        from center_messenger.alfa_client import AlfaError
        def handler(path,body):
            if path=='/v2api/1/study-status/index':raise AlfaError('forbidden',403)
            items=[{'id':n,'name':'Branch'} for n in (1,2)] if '/branch/index' in path else [{'id':1,'name':'Status'}]
            return {'items':items,'total':len(items),'page':body['page']}
        worker,client=self.catalog_worker(handler)
        for _ in range(6):worker.step(self.workspace)
        rows=self.catalog_rows()
        failed=[r for r in rows if r['state']=='pending']
        self.assertEqual(len(failed),1);self.assertEqual(failed[0]['payload']['error'],'access_denied')
        self.assertGreater(datetime.fromisoformat(failed[0]['payload']['available_at']),datetime.now(UTC)+timedelta(hours=5))
        self.assertTrue(all(r['state']=='published' for r in rows if r not in failed))
        native=self.db.execute(text('SELECT state,session_ciphertext,history_cursor FROM alfa_max_settings WHERE workspace_id=:w'),{'w':self.workspace}).mappings().one()
        self.assertEqual(native['state'],'active');self.assertEqual(native['session_ciphertext'],'test')
        self.assertEqual(native['history_cursor'],{})

    def test_catalog_expired_lease_and_changed_settings_cannot_publish_old_pages(self):
        def handler(path,body):
            self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('lease_until',now()-interval '1 second') "
                "WHERE workspace_id=:w AND topic='crm.catalog.branches'"),{'w':self.workspace})
            return {'items':[{'id':9,'name':'Stale'}],'total':1,'page':0}
        worker,client=self.catalog_worker(handler)
        self.assertFalse(worker.step(self.workspace))
        self.assertFalse(any(r['payload'].get('checkpoint') for r in self.catalog_rows()))
        def changed(path,body):
            self.db.execute(text('UPDATE alfa_settings SET version=version+1 WHERE workspace_id=:w'),{'w':self.workspace})
            return {'items':[{'id':9,'name':'Stale'}],'total':1,'page':0}
        client.call.side_effect=changed
        self.assertFalse(worker.step(self.workspace))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_branches WHERE workspace_id=:w AND external_id=9'),{'w':self.workspace}).scalar(),0)

    def test_catalog_401_waits_for_new_settings_then_resumes(self):
        from center_messenger.alfa_client import AlfaError
        worker,client=self.catalog_worker(lambda p,b:(_ for _ in ()).throw(AlfaError('unauthorized',401)))
        self.assertTrue(worker.step(self.workspace));self.assertFalse(worker.step(self.workspace))
        self.assertEqual(client.call.call_count,1)
        self.db.execute(text('UPDATE alfa_settings SET version=2 WHERE workspace_id=:w'),{'w':self.workspace})
        client.call.side_effect=lambda p,b:{'items':[{'id':1,'name':'Branch'}],'total':1,'page':0}
        self.assertTrue(worker.step(self.workspace));self.assertEqual(client.call.call_count,2)

    def test_active_review_adds_only_new_branch_without_moving_existing_six_hour_deadline(self):
        from center_messenger import active_review
        def handler(path,body):
            items=[{'id':7,'name':'Child','is_study':1}] if path.startswith('/v2api/1/') else []
            return {'items':items,'total':len(items),'page':body['page']}
        worker,client=self.active_review_worker(handler)
        for _ in range(4):self.assertTrue(worker.step(self.workspace))
        deadline=active_review.status(self.db,self.workspace,self.crm)['next_at']
        self.db.execute(text("INSERT INTO alfa_branches(workspace_id,connection_id,external_id,name) VALUES(:w,:c,3,'New')"),{'w':self.workspace,'c':self.crm})
        client.call.reset_mock()
        for _ in range(2):self.assertTrue(worker.step(self.workspace))
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['/v2api/3/customer/index'])
        result=active_review.status(self.db,self.workspace,self.crm)
        self.assertEqual(result['next_at'],deadline);self.assertEqual(result['known_branches'],[1,2,3])
        self.assertFalse(worker.step(self.workspace))

    def test_active_review_forbidden_branch_does_not_block_other_branches(self):
        from center_messenger import active_review
        from center_messenger.alfa_client import AlfaError
        def handler(path,body):
            if path.startswith('/v2api/1/'):raise AlfaError('forbidden',403)
            return {'items':[{'id':8,'name':'Another child','is_study':1}],'total':1,'page':0}
        worker,client=self.active_review_worker(handler)
        self.assertFalse(worker.step(self.workspace))
        self.assertTrue(worker.step(self.workspace));self.assertTrue(worker.step(self.workspace))
        result=active_review.status(self.db,self.workspace,self.crm)
        self.assertEqual(result['state'],'complete');self.assertEqual(result['branch_errors']['1']['error'],'forbidden')
        self.assertEqual(self.db.execute(text('SELECT name FROM alfa_customers WHERE workspace_id=:w AND external_id=8'),{'w':self.workspace}).scalar(),'Another child')

    def test_active_review_network_retries_keep_the_page_after_three_failures(self):
        from center_messenger import active_review
        from center_messenger.alfa_client import AlfaError
        fail=[True]
        def handler(path,body):
            if body['page']==1 and fail[0]:raise AlfaError('network')
            return {'items':[{'id':7+body['page'],'name':'Child','is_study':1}],'total':2,'page':body['page']}
        worker,client=self.active_review_worker(handler)
        self.assertTrue(worker.step(self.workspace))
        run=active_review.status(self.db,self.workspace,self.crm)['run']
        for _ in range(3):
            self.assertFalse(worker.step(self.workspace))
            value=active_review.status(self.db,self.workspace,self.crm)
            self.assertEqual(value['page'],1);self.assertEqual(value['run'],run)
            active_review.store(self.db,self.workspace,self.crm,dict(value,next_at=self.at.isoformat()))
        fail[0]=False;self.assertTrue(worker.step(self.workspace))
        self.assertEqual([c.args[1]['page'] for c in client.call.call_args_list],[0,1,1,1,1])

    def seed_lesson_cadence(self, *, recent_minutes=0):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        today=datetime.now(UTC).date()
        lesson={'id':71,'date':(today+timedelta(days=3)).isoformat(),'status':1,'branch_id':1,
            'customer_id':7,'customer_verified':True,'subject_id':1,'lesson_type_id':1}
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,lessons,"
            "lessons_refreshed_at,lessons_full_refreshed_at) VALUES(:w,:c,1,7,1,123.45,CAST(:l AS jsonb),"
            "now()-(:minutes * interval '1 minute'),now()-interval '25 hours')"),
            {'w':self.workspace,'c':self.crm,'l':json.dumps([lesson]),'minutes':recent_minutes})
        return reader,worker,today,lesson

    def test_daily_lessons_reuse_fresh_recent_window_without_fetching_future_again(self):
        from center_messenger.family_cache import enqueue,HISTORY
        _,worker,today,lesson=self.seed_lesson_cadence()
        before=dict(self.family_facts())
        enqueue(self.db,self.workspace,self.snapshot,topics=(HISTORY,))
        client=SimpleNamespace(call=Mock(return_value={'items':[],'total':0,'page':0}))
        worker.connections.client_factory.return_value=client
        worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(client.call.call_count,1)
        body=client.call.call_args.args[1]
        self.assertEqual(body['status'],3);self.assertEqual(body['date_to'],(today-timedelta(days=31)).isoformat())
        facts=self.family_facts()
        self.assertEqual(facts['lessons'],[lesson]);self.assertEqual(facts['lessons_refreshed_at'],before['lessons_refreshed_at'])
        self.assertGreater(facts['lessons_full_refreshed_at'],before['lessons_full_refreshed_at'])
        self.assertEqual(facts['lessons_history_through'],today-timedelta(days=31))

    def test_daily_lessons_fetch_future_when_recent_coverage_is_stale(self):
        from center_messenger.family_cache import enqueue,HISTORY
        _,worker,today,lesson=self.seed_lesson_cadence(recent_minutes=31)
        enqueue(self.db,self.workspace,self.snapshot,topics=(HISTORY,))
        client=SimpleNamespace(call=Mock(return_value={'items':[],'total':0,'page':0}))
        worker.connections.client_factory.return_value=client
        worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual([c.args[1]['status'] for c in client.call.call_args_list],[3,1])
        self.assertEqual(self.family_facts()['lessons'],[])
        self.assertEqual(self.family_facts()['lessons_history_through'],today+timedelta(days=30))

    def test_late_yearly_result_preserves_a_newer_recent_cancellation(self):
        from center_messenger.family_cache import enqueue,HISTORY
        _,worker,today,lesson=self.seed_lesson_cadence(recent_minutes=31)
        enqueue(self.db,self.workspace,self.snapshot,topics=(HISTORY,))
        old={**lesson,'id':70,'status':3,'date':(today-timedelta(days=100)).isoformat()}
        def fetch(*args,**kwargs):
            self.db.execute(text("UPDATE alfa_family_details SET lessons='[]',lessons_refreshed_at=clock_timestamp() WHERE workspace_id=:w"),{'w':self.workspace})
            return [old,lesson]
        with patch('center_messenger.family_cache.fetch_family_lessons',side_effect=fetch):
            worker.process(self.workspace,worker.claim(self.workspace))
        facts=self.family_facts()
        self.assertEqual([item['id'] for item in facts['lessons']],[70])
        self.assertGreater(facts['lessons_refreshed_at'],facts['lessons_full_refreshed_at'])
        self.assertEqual(facts['lessons_history_through'],today-timedelta(days=31))

    def test_cached_only_card_does_not_enqueue_or_wait_for_crm(self):
        reader,worker,_,_=self.seed_lesson_cadence(recent_minutes=31)
        before=dict(self.family_facts());conversation=self.conversation()['id']
        value=reader.prepare(None,self.workspace,self.contact,conversation=conversation,cached_only=True)
        self.assertEqual(value['students'][0]['balance'],'123.45');self.assertEqual(self.family_jobs(),[])
        worker.connections.client_factory.assert_not_called()
        reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(dict(self.family_facts()),before)
        self.assertTrue(value['cache']['ready'])
        self.assertTrue(self.db.execute(text("SELECT 1 FROM outbox_events WHERE workspace_id=:w AND topic='crm.card.lessons' AND state='pending'"),{'w':self.workspace}).scalar())
        worker.connections.client_factory.assert_not_called()

    def test_archived_customer_new_messages_keep_distinct_ids_and_skip_saved_source(self):
        from center_messenger.history_receipts import new_records
        self.db.execute(text("UPDATE alfa_customers SET status='archive' WHERE workspace_id=:w"),{'w':self.workspace})
        first=self.row('100',body='Same text');second=self.row('101',body='Same text')
        self.project(first)
        fresh=new_records(self.db,self.workspace,self.channel,1,[first,second])
        self.assertEqual([r['record_id'] for r in fresh],['101'])
        self.project(*fresh)
        self.assertEqual(len(self.messages()),2)
        self.assertEqual(new_records(self.db,self.workspace,self.channel,1,[first,second]),[])

    def test_proven_history_peer_backfills_the_saved_row_without_duplicating_it(self):
        from center_messenger.history_receipts import new_records
        original = self.row('100', body='Same text')
        self.project(original)
        self.assertIsNone(self.messages()[0]['payload'].get('max_user_id'))
        proven = dict(original, max_user_id='77')
        fresh = new_records(self.db, self.workspace, self.channel, 1, [proven])
        self.assertEqual([row['record_id'] for row in fresh], ['100'])
        self.project(*fresh)
        self.assertEqual(len(self.messages()), 1)

        self.assertEqual(self.messages()[0]['payload']['max_user_id'], '77')
        self.assertEqual(new_records(self.db, self.workspace, self.channel, 1, [proven]), [])
        self.project(proven)
        self.assertEqual(len(self.messages()), 1)

    def test_same_incoming_source_relinked_to_active_customer_keeps_body_and_history(self):
        from center_messenger.history_receipts import new_records
        self.snapshot['version'] = 1
        second = uuid4()
        self.add_customer(second, 1, 8)
        self.customer_phone(7, parent='Old spelling', status='lead')
        self.customer_phone(8, parent='Current parent', status='client')
        old = self.row('52289', customer=7, body='Original body')
        self.project(old)
        identifier, happened = self.messages()[0]['id'], self.messages()[0]['occurred_at']
        source = self.row('52289', customer=8, body='Upstream edit must not replace body')
        fresh = new_records(self.db, self.workspace, self.channel, 1, [source])
        self.assertTrue(fresh[0]['source_metadata_refresh'])
        self.project(*fresh)
        self.assertEqual(len(self.messages()), 1)
        message = self.messages()[0]
        self.assertEqual((message['id'], message['text'], message['occurred_at']),
                         (identifier, 'Original body', happened))
        self.assertEqual(message['payload']['customer_id'], 8)
        self.assertEqual(self.reader.listing(None, self.workspace)['total'], 1)
        target = self.service.send_target(self.workspace, self.channel, message['conversation_id'])
        self.assertEqual((target['branch'], target['customer']), (1, 8))
        self.assertEqual(new_records(self.db, self.workspace, self.channel, 1, [source]), [])

    def test_relink_witness_routes_reply_while_another_branch_still_has_old_card(self):
        from center_messenger.history_receipts import new_records
        self.snapshot['version'] = 1
        second = uuid4()
        self.add_customer(second, 1, 8)
        self.customer_phone(7, parent='Old spelling', status='lead')
        self.customer_phone(8, parent='Current parent', status='client')
        old = self.row('52289', customer=7)
        self.project(old)
        self.project(old, branch=2)
        fresh = new_records(self.db, self.workspace, self.channel, 2, [self.row('52289', customer=8)])
        self.project(*fresh, branch=2)
        self.assertEqual(len(self.messages()), 1)
        conversation = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w "
            "AND external_chat_id=:external"), {'w': self.workspace,
            'external': 'alfa-contact:' + str(min(self.contact, second))}).scalar_one()
        target = self.service.send_target(self.workspace, self.channel, conversation)
        self.assertEqual((target['branch'], target['customer']), (1, 8))
        refreshed = new_records(self.db, self.workspace, self.channel, 1, [self.row('52289', customer=8)])
        self.project(*refreshed)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.messages()[0]['payload']['customer_id'], 8)

    def test_stale_valeria_selected_card_is_repaired_after_list_already_changed(self):
        from center_messenger.history_receipts import new_records
        self.snapshot['version'] = 1
        old_contact, current_contact = uuid4(), uuid4()
        self.add_customer(old_contact, 2, 9805)
        self.add_customer(current_contact, 1, 9751)
        self.customer_phone(9805, parent='Valeria', status='lead')
        self.customer_phone(9751, parent='Current parent', status='client')
        old = self.row('52289', customer=9805, branch=4, body='Original incoming')
        self.project(old, branch=4)
        message = self.messages()[0]
        # A partially repaired legacy projection may already list the current
        # source customer while the sender still points to the old lead.
        self.db.execute(text("UPDATE messages SET payload=payload||CAST(:p AS jsonb) "
            "WHERE workspace_id=:w AND id=:id"), {'w': self.workspace, 'id': message['id'],
            'p': json.dumps({'crm_customer_ids': [9751], 'crm_observed_customers': [[4, 9751]]})})
        current = self.row('52289', customer=9751, branch=4, body='Upstream changed text')
        fresh = new_records(self.db, self.workspace, self.channel, 4, [current])
        self.assertEqual(len(fresh), 1)
        self.project(*fresh, branch=4)
        saved = self.messages()
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]['id'], saved[0]['text'], saved[0]['occurred_at']),
                         (message['id'], message['text'], message['occurred_at']))
        self.assertEqual(saved[0]['payload']['customer_id'], 9751)
        self.assertEqual(self.reader.listing(None, self.workspace)['total'], 1)
        target = self.service.send_target(self.workspace, self.channel, saved[0]['conversation_id'])
        self.assertEqual((target['branch'], target['customer']), (1, 9751))
        self.assertEqual(new_records(self.db, self.workspace, self.channel, 4, [current]), [])

    def test_projector_rechecks_selected_branch_even_when_source_signature_matches(self):
        original = self.row('52289')
        self.project(original)
        message = self.messages()[0]
        self.db.execute(text("UPDATE messages SET payload=jsonb_set(payload,'{customer_branch_id}','2') "
            "WHERE workspace_id=:w AND id=:id"), {'w': self.workspace, 'id': message['id']})
        self.project(dict(original, source_metadata_refresh=True))
        self.assertEqual(self.messages()[0]['payload']['customer_branch_id'], 1)

    def family_jobs(self):
        return list(self.db.execute(text("SELECT * FROM outbox_events WHERE workspace_id=:w AND topic='crm.card.refresh'"),
            {'w':self.workspace}).mappings())

    def family_facts(self):
        return self.db.execute(text("SELECT * FROM alfa_family_details WHERE workspace_id=:w"),
            {'w':self.workspace}).mappings().one_or_none()

    @staticmethod
    def live_family_facts():
        return {'balance':'4123.75','paid_lesson_count':5,'subscriptions':[{'id':40,'balance':'2725.00','subject_ids':[],'lesson_type_ids':[]}],'lessons':[]}

    def finish_family_lessons(self, worker):
        # A fresh balance is committed before either lesson request starts.
        for expected in ('crm.card.lessons', 'crm.card.lesson-history'):
            job = worker.claim(self.workspace)
            self.assertEqual(job['topic'], expected)
            with patch('center_messenger.family_cache.fetch_family_lessons', return_value=[]) as remote:
                worker.process(self.workspace, job)
            remote.assert_called_once()

    def test_family_cache_worker_prepares_pending_cards_without_any_operator_session(self):
        reader, worker, student = self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        worker.schedule(self.workspace)
        self.assertEqual(len(self.family_jobs()),1)
        job = worker.claim(self.workspace)
        self.assertIsNone(worker.claim(self.workspace))
        def fetch(*args,**kwargs):
            self.assertIsNone(self.db.get_nested_transaction(), 'Network IO must not hold a database transaction')
            return self.live_family_facts()
        with patch('center_messenger.family_cache.fetch_family_finance',side_effect=fetch) as remote:
            worker.process(self.workspace,job)
        remote.assert_called_once()
        person = reader.detail(None,self.workspace,self.contact)['students'][0]
        self.assertEqual(person['balance'],'4123.75')
        self.assertEqual(person['cards'][0]['subscriptions'][0]['balance'],'2725.00')
        self.assertEqual(self.family_jobs()[0]['state'],'published')
        self.finish_family_lessons(worker)
        worker.schedule(self.workspace)
        self.assertIsNone(worker.claim(self.workspace), 'Fresh cards must not refetch on every poll')

    def test_family_card_30_minute_clock_does_not_restart_with_each_new_message(self):
        reader, worker, student = self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,refreshed_at,lessons_refreshed_at,lessons_full_refreshed_at) "
            "VALUES (:w,:c,1,7,1,100,now()-interval '29 minutes',now(),now())"),{'w':self.workspace,'c':self.crm})
        worker.schedule(self.workspace)
        self.assertEqual(self.family_jobs(),[])
        self.project(self.row('2',at=self.at+timedelta(minutes=1)))
        worker.schedule(self.workspace)
        self.assertEqual(self.family_jobs(),[])
        self.db.execute(text("UPDATE alfa_family_details SET refreshed_at=now()-interval '31 minutes' WHERE workspace_id=:w"),{'w':self.workspace})
        worker.schedule(self.workspace)
        self.assertEqual(len(self.family_jobs()),1)
        original = self.family_jobs()[0]['payload']['due_at']
        self.project(self.row('3',at=self.at+timedelta(minutes=2)))
        worker.schedule(self.workspace)
        self.assertEqual(self.family_jobs()[0]['payload']['due_at'],original)

    def test_family_cache_recovers_an_expired_lease_and_fences_the_old_worker(self):
        from center_messenger.family_cache import FamilyCacheWorker
        reader, worker, _ = self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        old_job = worker.claim(self.workspace)
        restarted = FamilyCacheWorker(worker.connections)
        self.assertIsNone(restarted.claim(self.workspace))
        self.db.execute(text("UPDATE outbox_events SET payload=payload||CAST(:p AS jsonb) "
            "WHERE workspace_id=:w AND topic='crm.card.refresh'"),
            {'w':self.workspace,'p':json.dumps({'lease_until':'2000-01-01T00:00:00+00:00'})})
        new_job = restarted.claim(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,old_job)
            self.assertIsNone(self.family_facts())
            restarted.process(self.workspace,new_job)
        self.assertEqual(str(self.family_facts()['balance']),'4123.75')

    def test_failed_family_refresh_retains_good_facts_and_retry_deadline(self):
        from center_messenger.alfa_client import AlfaError
        reader, worker, _ = self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,refreshed_at,lessons_refreshed_at,lessons_full_refreshed_at) "
            "VALUES (:w,:c,1,7,1,123.45,now()-interval '31 minutes',now(),now())"),{'w':self.workspace,'c':self.crm})
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',side_effect=AlfaError('network')):
            worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(str(self.family_facts()['balance']),'123.45')
        original = dict(self.family_jobs()[0]['payload'])
        worker.schedule(self.workspace)
        self.assertEqual(self.family_jobs()[0]['payload']['available_at'],original['available_at'])
        self.assertIsNone(worker.claim(self.workspace))
        card = reader.detail(None,self.workspace,self.contact)
        self.assertTrue(card['cache']['ready'])
        self.assertTrue(card['cache']['overdue'])
        self.assertIsNotNone(card['cache']['error'])

    def test_answered_family_stops_automatic_work_but_keeps_disk_cache(self):
        _, worker, _ = self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        self.project(self.row('2',direction='out',at=self.at+timedelta(minutes=1)))
        with patch('center_messenger.family_cache.fetch_family_finance') as remote:
            worker.process(self.workspace,worker.claim(self.workspace))
        remote.assert_not_called()
        self.assertEqual(self.family_jobs()[0]['state'],'published')
        worker.schedule(self.workspace)
        self.assertIsNone(worker.claim(self.workspace))

    def test_explicit_open_promotes_an_existing_automatic_job_after_the_client_is_answered(self):
        reader,worker,_ = self.family_worker_fixture()
        self.project(self.row())
        conversation = self.conversation()['id']
        worker.schedule(self.workspace)
        job = worker.claim(self.workspace)
        self.project(self.row('2',direction='out',at=self.at+timedelta(minutes=1)))
        reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        saved = self.family_jobs()[0]['payload']
        self.assertTrue(saved['manual'])
        self.assertEqual(saved['lease_owner'],job['lease_owner'])
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()) as remote:
            worker.process(self.workspace,job)
        remote.assert_called_once()
        self.assertIsNotNone(self.family_facts())

    def test_read_family_only_prepares_after_explicit_open_and_cached_poll_does_not_enqueue(self):
        reader, worker, _ = self.family_worker_fixture()
        self.project(self.row('1',direction='out',actor='automation'))
        conversation = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND contact_id=:c"),
            {'w':self.workspace,'c':self.contact}).scalar_one()
        self.assertTrue(reader.prepare(None,self.workspace,self.contact,conversation=conversation,auto=True)['deferred'])
        self.assertEqual(self.family_jobs(),[])
        reader.prepare(None,self.workspace,self.contact,conversation=conversation,cached_only=True)
        self.assertEqual(self.family_jobs(),[])
        result = reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(result['cache']['state'],'queued')
        self.assertTrue(result['cache']['pending'])
        self.assertFalse(result['cache']['refreshing'])
        self.assertFalse(result['cache']['auto_refresh'])
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        self.finish_family_lessons(worker)
        self.db.execute(text("UPDATE alfa_family_details SET refreshed_at=now()-interval '31 minutes' WHERE workspace_id=:w"),{'w':self.workspace})
        worker.schedule(self.workspace)
        reader.prepare(None,self.workspace,self.contact,conversation=conversation,cached_only=True)
        self.assertIsNone(worker.claim(self.workspace))
        with self.assertRaises(AuthError):
            reader.prepare(None,self.workspace,uuid4(),conversation=conversation)

    def test_family_settings_change_cannot_publish_old_facts_or_lose_new_manual_request(self):
        reader, worker, _ = self.family_worker_fixture()
        self.project(self.row('1',direction='out',actor='automation'))
        conversation = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND contact_id=:c"),
            {'w':self.workspace,'c':self.contact}).scalar_one()
        reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        job = worker.claim(self.workspace)
        self.snapshot['version'] = 2
        self.db.execute(text('UPDATE alfa_settings SET version=2 WHERE workspace_id=:w'),{'w':self.workspace})
        reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        rebased=self.family_jobs()[0]['payload']
        self.assertEqual(rebased['version'],2)
        self.assertTrue(rebased['manual'])
        self.assertEqual(rebased['source_contact'],str(self.contact))
        self.assertEqual(rebased['conversation'],str(conversation))
        self.assertNotIn('lease_owner',rebased)
        worker.process(self.workspace,job)
        self.assertIsNone(self.family_facts())
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(self.family_facts()['settings_version'],2)

    def test_slow_or_failed_lesson_history_cannot_hold_live_money_or_its_next_refresh(self):
        from center_messenger.family_cache import TOPIC, HISTORY
        from center_messenger.alfa_client import AlfaError
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        first=reader.detail(None,self.workspace,self.contact)
        self.assertEqual(first['students'][0]['balance'],'4123.75')
        self.assertIsNone(first['cache']['lessons_updated_at'])
        self.assertEqual(first['cache']['state'],'queued')
        self.assertTrue(first['cache']['pending'])
        self.assertFalse(first['cache']['refreshing'])
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]):
            worker.process(self.workspace,worker.claim(self.workspace))
        financial_time=self.family_facts()['refreshed_at']
        history=worker.claim(self.workspace)
        self.assertEqual(history['topic'],HISTORY)
        with patch('center_messenger.family_cache.fetch_family_lessons',side_effect=AlfaError('network')):
            worker.process(self.workspace,history)
        cache=reader.detail(None,self.workspace,self.contact)['cache']
        self.assertIn('Баланс и абонементы обновляются отдельно',cache['error'])
        self.assertEqual(self.family_facts()['refreshed_at'],financial_time)
        self.assertIsNone(self.family_facts()['lessons_full_refreshed_at'])
        self.db.execute(text("UPDATE alfa_family_details SET refreshed_at=now()-interval '31 minutes' WHERE workspace_id=:w"),{'w':self.workspace})
        worker.schedule(self.workspace)
        money=worker.claim(self.workspace)
        self.assertEqual(money['topic'],TOPIC)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value={**self.live_family_facts(),'balance':'5000.00'}):
            worker.process(self.workspace,money)
        self.assertEqual(reader.detail(None,self.workspace,self.contact)['students'][0]['balance'],'5000.00')

    def test_card_status_distinguishes_queue_active_lease_and_busy_retry(self):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row('1',direction='out',actor='automation'))
        conversation=self.conversation()['id']
        queued=reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(queued['cache']['state'],'queued')
        self.assertFalse(queued['cache']['loading'])
        self.assertEqual(queued['cache']['sections']['finance']['status'],'queued')
        self.assertEqual(queued['cache']['sections']['finance']['source_customer_ids'],['7'])
        job=worker.claim(self.workspace)
        loading=reader.detail(None,self.workspace,self.contact)
        self.assertEqual(loading['cache']['state'],'loading')
        self.assertTrue(loading['cache']['loading'])
        self.assertIsNotNone(loading['cache']['sections']['finance']['started_at'])
        self.assertGreaterEqual(loading['cache']['sections']['finance']['queue_wait_ms'],0)
        worker.retry(self.workspace,job,busy=True,error=AuthError(409,'busy'))
        busy=reader.detail(None,self.workspace,self.contact)['cache']
        self.assertEqual(busy['state'],'busy')
        self.assertFalse(busy['loading'])
        self.assertTrue(busy['busy'])
        self.assertEqual(busy['sections']['finance']['status'],'busy')
        self.assertIsNotNone(busy['sections']['finance']['retry_at'])

    def test_financial_refresh_preserves_lesson_proof_and_the_yearly_check_clock(self):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        lesson={'id':10,'date':'2026-03-01','time':'12:00','branch_id':2,'subject_id':11,'lesson_type_id':1,
                'customer_id':7,'customer_verified':True,'subscription_id':40,'group_ids':[5],'status':3}
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,lessons,"
            "refreshed_at,lessons_refreshed_at,lessons_full_refreshed_at) VALUES(:w,:c,1,7,1,100,CAST(:l AS jsonb),"
            "now()-interval '31 minutes',now(),now()-interval '23 hours')"),{'w':self.workspace,'c':self.crm,'l':json.dumps([lesson])})
        before=dict(self.family_facts())
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        after=self.family_facts()
        self.assertEqual(after['lessons'],[lesson])
        self.assertEqual(after['lessons_refreshed_at'],before['lessons_refreshed_at'])
        self.assertEqual(after['lessons_full_refreshed_at'],before['lessons_full_refreshed_at'])
        self.assertIsNone(worker.claim(self.workspace))
        self.db.execute(text("UPDATE alfa_family_details SET lessons_full_refreshed_at=now()-interval '25 hours' WHERE workspace_id=:w"),{'w':self.workspace})
        worker.schedule(self.workspace)
        job=worker.claim(self.workspace)
        self.assertEqual(job['topic'],'crm.card.lesson-history')
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]) as remote:
            worker.process(self.workspace,job)
        self.assertTrue(remote.call_args.kwargs['full'])
        self.assertEqual(self.family_facts()['lessons'],[],'A complete daily read must remove deleted old lessons')
        self.assertEqual(self.family_facts()['refreshed_at'],after['refreshed_at'])

    def test_recent_lesson_job_commits_cancellations_without_erasing_older_proof(self):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        today=datetime.now(UTC).date()
        old={'id':10,'date':(today-timedelta(days=100)).isoformat(),'status':3,'time':'12:00','branch_id':2,
             'subject_id':11,'lesson_type_id':1,'customer_id':7,'customer_verified':True,'subscription_id':40,'group_ids':[5]}
        removed={**old,'id':11,'date':today.isoformat()}
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,lessons,"
            "lessons_refreshed_at,lessons_full_refreshed_at) VALUES(:w,:c,1,7,1,100,CAST(:l AS jsonb),"
            "now()-interval '31 minutes',now())"),{'w':self.workspace,'c':self.crm,'l':json.dumps([old,removed])})
        before=dict(self.family_facts())
        worker.schedule(self.workspace)
        job=worker.claim(self.workspace)
        self.assertEqual(job['topic'],'crm.card.lessons')
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]) as remote:
            worker.process(self.workspace,job)
        self.assertFalse(remote.call_args.kwargs['full'])
        self.assertEqual(self.family_facts()['lessons'],[old])
        self.assertEqual(self.family_facts()['refreshed_at'],before['refreshed_at'])
        self.assertEqual(self.family_facts()['lessons_full_refreshed_at'],before['lessons_full_refreshed_at'])

    def test_separate_lesson_job_survives_restart_and_stale_owner_cannot_publish(self):
        from center_messenger.family_cache import FamilyCacheWorker
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        old=worker.claim(self.workspace)
        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{lease_until}',to_jsonb(CAST(:at AS text))) WHERE id=:id"),
            {'id':old['id'],'at':'2000-01-01T00:00:00+00:00'})
        restarted=FamilyCacheWorker(worker.connections)
        job=restarted.claim(self.workspace)
        self.assertEqual(job['id'],old['id'])
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]) as remote:
            worker.process(self.workspace,old)
            remote.assert_not_called()
            self.assertIsNone(self.family_facts()['lessons_refreshed_at'])
            restarted.process(self.workspace,job)
        self.assertIsNotNone(self.family_facts()['lessons_refreshed_at'])
        self.assertEqual(str(self.family_facts()['balance']),'4123.75')

    def test_force_request_during_lesson_io_preserves_lease_and_runs_again(self):
        from center_messenger.family_cache import enqueue, LESSONS, HISTORY
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        job=worker.claim(self.workspace)
        def request_again(*args,**kwargs):
            enqueue(self.db,self.workspace,self.snapshot,contact=self.contact,force=True,topics=(LESSONS,HISTORY))
            saved=self.db.execute(text('SELECT payload FROM outbox_events WHERE id=:id'),{'id':job['id']}).scalar_one()
            self.assertEqual(saved['lease_owner'],job['lease_owner'])
            return []
        with patch('center_messenger.family_cache.fetch_family_lessons',side_effect=request_again):
            worker.process(self.workspace,job)
        again=worker.claim(self.workspace)
        self.assertEqual(again['id'],job['id'])
        self.assertIsNotNone(again['force_at'])
        with patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]) as remote:
            worker.process(self.workspace,again)
        remote.assert_called_once()

    def test_changing_crm_version_drops_old_lesson_proof_with_the_financial_cache(self):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,lessons,"
            "lessons_refreshed_at,lessons_full_refreshed_at) VALUES(:w,:c,1,7,0,999,CAST(:l AS jsonb),now(),now())"),
            {'w':self.workspace,'c':self.crm,'l':json.dumps([{'id':99}])})
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(self.family_facts()['lessons'],[])
        self.assertIsNone(self.family_facts()['lessons_refreshed_at'])
        self.assertIsNone(self.family_facts()['lessons_full_refreshed_at'])

    def test_card_migration_keeps_existing_lesson_read_times_and_is_reversible(self):
        self.db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        path=ROOT/'app/backend/migrations/versions/0015_family_refresh_tiers.py'
        spec=importlib.util.spec_from_file_location('family_tiers',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        context=MigrationContext.configure(self.db)
        with Operations.context(context):
            module.downgrade()
            self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,refreshed_at) "
                "VALUES(:w,:c,1,7,1,100,:at)"),{'w':self.workspace,'c':self.crm,'at':self.at})
            module.upgrade()
        saved=self.family_facts()
        self.assertEqual(saved['refreshed_at'],self.at)
        self.assertEqual(saved['lessons_refreshed_at'],self.at)
        self.assertEqual(saved['lessons_full_refreshed_at'],self.at)

    def test_lesson_response_from_a_replaced_crm_connection_cannot_be_published(self):
        reader,worker,_=self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,worker.claim(self.workspace))
        job=worker.claim(self.workspace)
        def change_settings(*args,**kwargs):
            self.assertIsNone(self.db.get_nested_transaction())
            self.db.execute(text('UPDATE alfa_settings SET version=2 WHERE workspace_id=:w'),{'w':self.workspace})
            self.snapshot['version']=2
            return [{'id':99,'date':datetime.now(UTC).date().isoformat(),'status':1,'time':'12:00'}]
        with patch('center_messenger.family_cache.fetch_family_lessons',side_effect=change_settings):
            worker.process(self.workspace,job)
        self.assertEqual(self.family_facts()['lessons'],[])
        self.assertIsNone(self.family_facts()['lessons_refreshed_at'])
        worker.schedule(self.workspace)
        self.assertEqual(worker.claim(self.workspace)['topic'],'crm.card.refresh')

    def test_existing_cross_id_student_aliases_are_repaired_in_bounded_idempotent_batches(self):
        from center_messenger.family_identity import repair
        reader,worker,shared=self.family_worker_fixture()
        self.project(self.row())
        for customer in (8,9):
            self.add_customer(self.contact,1,customer)
        self.db.execute(text('UPDATE alfa_customers SET student_id=:s WHERE workspace_id=:w'),{'w':self.workspace,'s':shared})
        self.db.execute(text("INSERT INTO contact_student_links(workspace_id,contact_id,student_id,managed_by_alfa) VALUES(:w,:c,:s,false)"),
            {'w':self.workspace,'c':self.contact,'s':shared})
        messages_before=[m['id'] for m in self.messages()]
        self.assertEqual(repair(self.db,self.workspace,self.snapshot,limit=1),1)
        self.assertEqual(repair(self.db,self.workspace,self.snapshot,limit=1),1)
        self.assertEqual(repair(self.db,self.workspace,self.snapshot,limit=1),1)
        self.assertEqual(repair(self.db,self.workspace,self.snapshot,limit=1),0)
        rows=list(self.db.execute(text('SELECT external_id,student_id FROM alfa_customers WHERE workspace_id=:w ORDER BY external_id'),{'w':self.workspace}).mappings())
        self.assertEqual(len({r['student_id'] for r in rows}),3)
        self.assertEqual(rows[0]['student_id'],shared)
        self.assertFalse(self.db.execute(text('SELECT managed_by_alfa FROM contact_student_links WHERE workspace_id=:w AND student_id=:s'),
            {'w':self.workspace,'s':shared}).scalar_one())
        self.assertEqual([m['id'] for m in self.messages()],messages_before)
        self.assertEqual(len(reader.detail(None,self.workspace,self.contact)['students']),3)

    def test_current_review_clears_legacy_flags_without_reimporting_or_changing_identities(self):
        from center_messenger import crm_review
        reader,_,_=self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text('UPDATE alfa_directory SET needs_review=true WHERE workspace_id=:w'),{'w':self.workspace})
        self.db.execute(text('UPDATE alfa_customers SET needs_review=true WHERE workspace_id=:w'),{'w':self.workspace})
        before=[(m['id'],m['conversation_id'],m['payload']) for m in self.messages()]
        identities=list(self.db.execute(text('SELECT contact_id,student_id FROM alfa_customers WHERE workspace_id=:w'),{'w':self.workspace}))
        current=reader.detail(None,self.workspace,self.contact)
        self.assertFalse(current['needs_review']);self.assertEqual(current['review_reasons'],[])
        self.assertTrue(self.db.execute(text('SELECT needs_review FROM alfa_directory WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one())
        self.assertEqual(crm_review.repair(self.db,self.workspace,self.snapshot,limit=1),1)
        self.assertFalse(self.db.execute(text('SELECT needs_review FROM alfa_directory WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one())
        self.assertFalse(self.db.execute(text('SELECT needs_review FROM alfa_customers WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one())
        self.assertEqual(crm_review.repair(self.db,self.workspace,self.snapshot,limit=1),0)
        self.assertEqual(list(self.db.execute(text('SELECT contact_id,student_id FROM alfa_customers WHERE workspace_id=:w'),{'w':self.workspace})),identities)
        self.assertEqual([(m['id'],m['conversation_id'],m['payload']) for m in self.messages()],before)

    def test_current_review_preserves_real_cross_branch_conflicts_and_ignores_closed_branches(self):
        from center_messenger import crm_review
        from center_messenger.family_identity import repair
        reader,_,_=self.family_worker_fixture()
        self.add_customer(self.contact,2,7)
        self.db.execute(text("UPDATE alfa_customers SET name=CASE branch_id WHEN 1 THEN 'First child' ELSE 'Other child' END,"
            "dob=CASE branch_id WHEN 1 THEN DATE '2016-01-01' ELSE DATE '2017-02-02' END WHERE workspace_id=:w"),{'w':self.workspace})
        repair(self.db,self.workspace,self.snapshot)
        current=reader.detail(None,self.workspace,self.contact)
        self.assertEqual({r['code'] for r in current['review_reasons']},{'customer_name_conflict','customer_birthdate_conflict'})
        crm_review.repair(self.db,self.workspace,self.snapshot)
        self.assertTrue(self.db.execute(text('SELECT needs_review FROM alfa_directory WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one())
        self.db.execute(text('UPDATE alfa_branches SET available=false WHERE workspace_id=:w AND external_id=2'),{'w':self.workspace})
        self.assertFalse(reader.detail(None,self.workspace,self.contact)['needs_review'])

    def test_cached_review_finds_a_cross_family_student_collision_without_disclosing_foreign_data(self):
        from center_messenger.family_identity import repair
        reader,_,student=self.family_worker_fixture()
        foreign=uuid4();self.add_customer(foreign,1,8)
        self.db.execute(text("UPDATE alfa_customers SET student_id=:s,name='Foreign child',legal_name='Foreign parent',"
            "phones='[\"+79990000099\"]'::jsonb WHERE workspace_id=:w AND external_id=8"),{'w':self.workspace,'s':student})
        current=reader.detail(None,self.workspace,self.contact)
        reasons=current['review_reasons']
        self.assertEqual({r['code'] for r in reasons},{'shared_student_binding'})
        self.assertEqual(reasons[0]['customer_ids'],['7'])
        self.assertNotIn('Foreign',json.dumps(reasons))
        repair(self.db,self.workspace,self.snapshot)
        self.assertFalse(reader.detail(None,self.workspace,self.contact)['needs_review'])

    def test_preferred_active_card_does_not_inherit_an_old_review_flag(self):
        policy,reader,_,_,_,conversation=self.preferred_card_fixture()
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.db.execute(text('UPDATE alfa_directory SET needs_review=true WHERE workspace_id=:w'),{'w':self.workspace})
        current=reader.detail(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(current['selection']['customer'],'9160')
        self.assertFalse(current['needs_review'])
        self.assertEqual(current['students'][0]['balance'],'487.50')

    def test_review_of_a_fixed_phone_cohort_does_not_trust_a_stale_ambiguity_hint(self):
        policy,reader,_,active,_,conversation=self.preferred_card_fixture()
        self.db.execute(text('UPDATE alfa_customers SET legal_type=2 WHERE workspace_id=:w AND contact_id=:c'),{'w':self.workspace,'c':active})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        flagged=reader.detail(None,self.workspace,self.contact,conversation=conversation)
        self.assertIn('shared_business_phone',{r['code'] for r in flagged['review_reasons']})
        self.db.execute(text('UPDATE alfa_customers SET legal_type=1 WHERE workspace_id=:w AND contact_id=:c'),{'w':self.workspace,'c':active})
        self.assertEqual(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)['ambiguous'],'shared_business_phone')
        self.assertFalse(reader.detail(None,self.workspace,self.contact,conversation=conversation)['needs_review'])

    def test_a_refused_card_redirect_keeps_its_current_conflict_without_exposing_the_candidate(self):
        policy,reader,_,active,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,legal_name,"
            "legal_type,dob,phones,status,contact_id,student_id) SELECT workspace_id,run_id,1,external_id,'Different child',legal_name,"
            "legal_type,dob,phones,status,contact_id,student_id FROM alfa_customers "
            "WHERE workspace_id=:w AND branch_id=2 AND external_id=9160"),{'w':self.workspace})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        current=reader.detail(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(current['selection']['ambiguous'],'conflicting_customer')
        reasons=current['review_reasons']
        self.assertIn('conflicting_customer',{r['code'] for r in reasons})
        self.assertNotIn('Different child',json.dumps(reasons))
        self.assertNotIn('9160',json.dumps(reasons))
        self.db.execute(text("UPDATE alfa_customers SET name='Мария Забунова' WHERE workspace_id=:w AND contact_id=:c"),
                        {'w':self.workspace,'c':active})
        self.assertFalse(reader.detail(None,self.workspace,self.contact,conversation=conversation)['needs_review'])

    def test_derived_review_repair_is_scoped_and_bounded_under_runtime_rls(self):
        from center_messenger import crm_review
        self.family_worker_fixture()
        for customer in (8,9):self.add_customer(uuid4(),1,customer)
        self.db.execute(text('UPDATE alfa_directory SET needs_review=true WHERE workspace_id=:w'),{'w':self.workspace})
        self.db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'));self.db.execute(text('SET CONSTRAINTS ALL DEFERRED'))
        self.db.execute(text(f'GRANT USAGE ON SCHEMA "{self.schema}" TO cm_app'))
        self.db.execute(text('SET LOCAL ROLE cm_app'))
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(self.workspace)})
        for expected in (2,1):
            self.assertEqual(crm_review.repair(self.db,self.workspace,self.snapshot,limit=1),1)
            self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_directory WHERE needs_review')).scalar_one(),expected)
        self.assertEqual(crm_review.repair(self.db,self.workspace,self.snapshot,limit=1),1)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_directory WHERE needs_review')).scalar_one(),0)

    def test_review_cleanup_waits_for_an_in_progress_identity_repair(self):
        from center_messenger import crm_review
        from center_messenger.family_identity import repair
        self.family_worker_fixture()
        self.db.execute(text('UPDATE alfa_directory SET needs_review=true WHERE workspace_id=:w'),{'w':self.workspace})
        self.db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,'crm.student.identity',CAST(:conn AS text),CAST(:p AS jsonb),'published')"),
            {'w':self.workspace,'id':uuid4(),'conn':self.crm,
             'p':json.dumps({'run':str(self.run),'version':1,'complete':False,'processed':0,'after':0})})
        self.assertEqual(crm_review.repair(self.db,self.workspace,self.snapshot),0)
        repair(self.db,self.workspace,self.snapshot)
        self.assertEqual(crm_review.repair(self.db,self.workspace,self.snapshot),1)

    def test_same_customer_entering_another_branch_keeps_its_parent_and_child_without_optional_branch_ids(self):
        from center_messenger.active_review import apply_customer
        reader,worker,student=self.family_worker_fixture()
        settings={'connection_id':self.crm}
        fact=apply_customer(self.db,self.workspace,settings,self.snapshot,
            {'id':7,'name':'Child corrected','legal_name':'Parent','is_study':1,'dob':'2015-02-03','_branch':2})
        self.assertEqual(fact['student_id'],str(student))
        self.assertEqual(fact['contact_id'],str(self.contact))
        self.assertEqual(self.db.execute(text('SELECT dob FROM alfa_customers WHERE workspace_id=:w AND branch_id=2'),
            {'w':self.workspace}).scalar_one(),datetime(2015,2,3).date())
        apply_customer(self.db,self.workspace,settings,self.snapshot,
            {'id':7,'name':'Child corrected again','legal_name':'Parent','is_study':1,'_branch':2})
        self.assertEqual(self.db.execute(text('SELECT dob FROM alfa_customers WHERE workspace_id=:w AND branch_id=2'),
            {'w':self.workspace}).scalar_one(),datetime(2015,2,3).date())

    def test_group_refresh_replaces_only_the_selected_customer_and_branch(self):
        from center_messenger import group_links
        reader,worker,student=self.family_worker_fixture(include_groups=True)
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        job=worker.claim(self.workspace)
        self.assertEqual(job['topic'],group_links.TOPIC)
        self.add_customer(self.contact,2,7)
        self.add_customer(self.contact,1,8)
        for branch,customer,member in ((1,7,100),(1,8,101),(2,7,100)):
            self.db.execute(text("INSERT INTO alfa_groups(workspace_id,run_id,branch_id,external_id,name) VALUES(:w,:r,:b,10,'Old') ON CONFLICT DO NOTHING"),
                {'w':self.workspace,'r':self.run,'b':branch})
            self.db.execute(text('INSERT INTO alfa_memberships(workspace_id,run_id,branch_id,group_id,external_id,customer_id) VALUES(:w,:r,:b,10,:id,:c)'),
                {'w':self.workspace,'r':self.run,'b':branch,'id':member,'c':customer})
        def fetch(*args):
            self.assertIsNone(self.db.get_nested_transaction())
            return {20:'New group'},[{'id':200,'group_id':20,'begin':self.at.date(),'end':None}]
        with patch('center_messenger.group_links.fetch',side_effect=fetch):
            worker.process(self.workspace,job)
        values=list(self.db.execute(text('SELECT branch_id,customer_id,group_id FROM alfa_memberships WHERE workspace_id=:w ORDER BY branch_id,customer_id'),
            {'w':self.workspace}))
        self.assertEqual(values,[(1,7,20),(1,8,10),(2,7,10)])
        card=reader.detail(None,self.workspace,self.contact)
        child=next(s for s in card['students'] if s['id']==str(student))
        self.assertEqual(child['groups'][0]['id'],'20')
        self.assertEqual(child['groups'][0]['memberships'][0]['begin'],'2026-10-24')
        self.assertIsNotNone(child['cards'][0]['groups_refreshed_at'])
        self.assertIsNone(self.family_facts(),'A light group refresh must not fetch a read client financial card')

    def test_group_refresh_only_schedules_active_clients_and_repeats_after_six_hours(self):
        from center_messenger import group_links
        _,worker,student=self.family_worker_fixture(include_groups=True)
        self.add_customer(self.contact,1,8)
        self.db.execute(text("UPDATE alfa_customers SET status='archive',student_id=:s WHERE workspace_id=:w AND external_id=8"),{'w':self.workspace,'s':student})
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        job=worker.claim(self.workspace)
        self.assertEqual(job['customer'],'7')
        with patch('center_messenger.group_links.fetch',return_value=({},[])):
            worker.process(self.workspace,job)
        self.assertIsNone(worker.claim(self.workspace))
        self.assertEqual(group_links.enqueue(self.db,self.workspace,self.snapshot),0)
        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{refreshed_at}',to_jsonb(CAST(:at AS text))) WHERE id=:id"),
            {'id':job['id'],'at':(datetime.now(UTC)-timedelta(hours=7)).isoformat()})
        self.assertEqual(group_links.enqueue(self.db,self.workspace,self.snapshot),1)
        self.assertEqual(worker.claim(self.workspace)['customer'],'7')

    def test_failed_group_refresh_retains_enrollments_last_success_and_its_retry_deadline(self):
        from center_messenger import group_links
        from center_messenger.alfa_client import AlfaError
        reader,worker,_=self.family_worker_fixture(include_groups=True)
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        job=worker.claim(self.workspace)
        with patch('center_messenger.group_links.fetch',return_value=({10:'Kept'},[{'id':100,'group_id':10,'begin':None,'end':None}])):
            worker.process(self.workspace,job)
        at=(datetime.now(UTC)-timedelta(hours=7)).isoformat()
        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{refreshed_at}',to_jsonb(CAST(:at AS text))) WHERE id=:id"),{'id':job['id'],'at':at})
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        with patch('center_messenger.group_links.fetch',side_effect=AlfaError('network')):
            worker.process(self.workspace,worker.claim(self.workspace))
        before=self.db.execute(text('SELECT payload FROM outbox_events WHERE id=:id'),{'id':job['id']}).scalar_one()
        self.assertEqual(before['refreshed_at'],at)
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        after=self.db.execute(text('SELECT payload FROM outbox_events WHERE id=:id'),{'id':job['id']}).scalar_one()
        self.assertEqual(after['available_at'],before['available_at'])
        self.assertIsNone(worker.claim(self.workspace))
        details=reader.detail(None,self.workspace,self.contact)
        self.assertEqual(details['students'][0]['groups'][0]['name'],'Kept')
        self.assertIsNotNone(details['cache']['groups_updated_at'])
        self.assertIn('Не удалось обновить группы',details['cache']['error'])

    def test_an_archived_client_groups_only_refresh_after_explicit_open(self):
        from center_messenger import group_links
        reader,worker,_=self.family_worker_fixture(include_groups=True)
        self.db.execute(text("UPDATE alfa_customers SET status='archive' WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertEqual(group_links.enqueue(self.db,self.workspace,self.snapshot),0)
        self.assertEqual(group_links.enqueue(self.db,self.workspace,self.snapshot,contact=self.contact),1)
        with patch('center_messenger.group_links.fetch',return_value=({},[])),patch('center_messenger.family_cache.fetch_family_finance') as money:
            worker.process(self.workspace,worker.claim(self.workspace))
        money.assert_not_called()
        self.assertEqual(group_links.enqueue(self.db,self.workspace,self.snapshot),0)

    def test_group_response_cannot_publish_after_settings_or_source_mapping_changes(self):
        from center_messenger import group_links
        _,worker,_=self.family_worker_fixture(include_groups=True)
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        job=worker.claim(self.workspace)
        def change(*args):
            self.db.execute(text('UPDATE alfa_settings SET version=2 WHERE workspace_id=:w'),{'w':self.workspace})
            return {10:'Stale'},[{'id':100,'group_id':10,'begin':None,'end':None}]
        with patch('center_messenger.group_links.fetch',side_effect=change):
            worker.process(self.workspace,job)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_memberships WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one(),0)

    def test_force_request_during_group_io_runs_again_and_preserves_the_live_lease(self):
        from center_messenger import group_links
        _,worker,_=self.family_worker_fixture(include_groups=True)
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        job=worker.claim(self.workspace)
        def request(*args):
            group_links.enqueue(self.db,self.workspace,self.snapshot,contact=self.contact,force=True)
            return {},[]
        with patch('center_messenger.group_links.fetch',side_effect=request):
            worker.process(self.workspace,job)
        again=worker.claim(self.workspace)
        self.assertEqual(again['id'],job['id'])
        self.assertIsNotNone(again['force_at'])
        with patch('center_messenger.group_links.fetch',return_value=({},[])) as fetch:
            worker.process(self.workspace,again)
        fetch.assert_called_once()

    def test_group_member_id_collision_rolls_back_without_stealing_another_child(self):
        from center_messenger import group_links
        _,worker,_=self.family_worker_fixture(include_groups=True)
        self.db.execute(text("INSERT INTO alfa_groups(workspace_id,run_id,branch_id,external_id,name) VALUES(:w,:r,1,10,'Original')"),{'w':self.workspace,'r':self.run})
        self.db.execute(text('INSERT INTO alfa_memberships(workspace_id,run_id,branch_id,group_id,external_id,customer_id) VALUES(:w,:r,1,10,100,8)'),{'w':self.workspace,'r':self.run})
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        with patch('center_messenger.group_links.fetch',return_value=({10:'Wrong'},[{'id':100,'group_id':10,'begin':None,'end':None}])):
            worker.process(self.workspace,worker.claim(self.workspace))
        self.assertEqual(self.db.execute(text('SELECT customer_id FROM alfa_memberships WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one(),8)
        self.assertEqual(self.db.execute(text('SELECT name FROM alfa_groups WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one(),'Original')

    def test_group_progress_counts_one_global_customer_and_waits_for_every_branch(self):
        from center_messenger import group_links
        _,worker,student=self.family_worker_fixture(include_groups=True)
        self.add_customer(self.contact,2,7)
        self.db.execute(text('UPDATE alfa_customers SET student_id=:s WHERE workspace_id=:w'),{'w':self.workspace,'s':student})
        group_links.enqueue(self.db,self.workspace,self.snapshot)
        for ready in (0,1):
            with patch('center_messenger.group_links.fetch',return_value=({},[])):
                worker.process(self.workspace,worker.claim(self.workspace))
            report=group_links.progress(self.db,self.workspace,self.snapshot)
            self.assertEqual(report['total'],1)
            self.assertEqual(report['ready'],ready)
            self.assertEqual(report['queued'],1-ready)

    def test_list_window_is_unchanged_until_a_real_change_and_moves_rows_without_gaps(self):
        self.project(self.row())
        self.add_customer(uuid4(),1,8)
        self.project(self.row('2',customer=8))
        first = self.reader.listing(None,self.workspace,window=30)
        self.assertTrue(first['reset'])
        self.assertEqual(len(first['order']),2)
        same = self.reader.listing(None,self.workspace,window=30,window_token=first['window_token'])
        self.assertTrue(same['not_modified'])
        self.assertEqual(same['conversations'],[])
        # Resealing credentials during card refresh must not invalidate chat rows.
        self.db.execute(text("UPDATE alfa_settings SET secret_ciphertext='rotated-ciphertext' WHERE workspace_id=:w"),
            {'w':self.workspace})
        unchanged = self.reader.listing(None,self.workspace,window=30,window_token=first['window_token'])
        self.assertTrue(unchanged['not_modified'])
        self.project(self.row('3',body='New question',at=self.at+timedelta(minutes=3)))
        updated = self.reader.listing(None,self.workspace,window=30,window_token=first['window_token'])
        self.assertFalse(updated['reset'])
        self.assertEqual(len(updated['conversations']),1)
        expected = self.db.execute(text('SELECT id FROM conversations WHERE workspace_id=:w AND contact_id=:c'),
            {'w':self.workspace,'c':self.contact}).scalar_one()
        self.assertEqual(updated['order'][0],str(expected))
        self.assertEqual(set(updated['order']),set(first['order']))
        # Cursors never carry privileges or leak another workspace/filter.
        reset = self.reader.listing(None,self.workspace,window=30,window_token=updated['window_token'],query='nobody')
        self.assertTrue(reset['reset'])
        self.assertEqual(reset['order'],[])
        self.reader.list_windows.views.clear()
        reset = self.reader.listing(None,self.workspace,window=30,window_token=updated['window_token'])
        self.assertTrue(reset['reset'])
        self.assertEqual(len(reset['order']),2)

    def test_list_window_appends_only_new_rows_and_replaces_the_exact_ordered_prefix(self):
        from center_messenger.chat_queue import repair_batch as queue_repair
        for n in range(40):
            self.add_customer(uuid4(),1,100+n)
            self.project(self.row(str(100+n),customer=100+n,at=self.at+timedelta(minutes=n)))
        while queue_repair(self.db,self.workspace,self.channel): pass
        first = self.reader.listing(None,self.workspace,window=30)
        self.assertTrue(first['has_more'])
        second = self.reader.listing(None,self.workspace,window=60,window_token=first['window_token'])
        self.assertEqual(len(second['order']),40)
        self.assertEqual(len(second['conversations']),10)
        self.assertFalse(second['has_more'])
        # An old row moves into the first prefix as a newer question arrives.
        self.project(self.row('999',customer=100,at=self.at+timedelta(days=1)))
        moved = self.reader.listing(None,self.workspace,window=30,window_token=second['window_token'])
        reference = self.reader.listing(None,self.workspace,through_page=0)
        self.assertEqual(moved['order'],[r['id'] for r in reference['conversations']])
        self.assertEqual(len(set(moved['order'])),30)
        self.assertEqual(len(moved['conversations']),1)

    def family_balance_fixture(self):
        self.snapshot.update(origin='https://test.s20.online',version=1,finished_at=self.at+timedelta(hours=1),today=self.at.date())
        student=uuid4()
        self.db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES (:w,:id,'Child')"),{'w':self.workspace,'id':student})
        self.db.execute(text("UPDATE alfa_customers SET student_id=:student,balance=3962.50,paid_lesson_count=3 WHERE workspace_id=:w"),{'w':self.workspace,'student':student})
        return DirectoryReader(SimpleNamespace(_transaction=self.accounts.transaction,chat_transaction=self.accounts.transaction,accounts=self.accounts)),student

    def test_family_balance_is_customer_total_before_any_subscription_cache_is_loaded(self):
        reader,student=self.family_balance_fixture()
        result=reader.detail(None,self.workspace,self.contact)
        self.assertEqual(result['students'][0]['id'],str(student))
        self.assertEqual(result['students'][0]['balance'],'3962.50')
        self.assertNotIn('subscriptions',result['students'][0]['cards'][0])

    def test_family_response_combines_shared_subscription_from_existing_branch_caches(self):
        reader,student=self.family_balance_fixture()
        self.add_customer(self.contact,2,7)
        self.db.execute(text("UPDATE alfa_customers SET student_id=:student WHERE workspace_id=:w"),{'w':self.workspace,'student':student})
        subscription={'id':40,'tariff_id':9,'name':'Art school','balance':'8062.50','begin':'2026-10-01','end':'2027-07-04','archived':False,'subject_ids':[11],'lesson_type_ids':[1]}
        lesson={'id':10,'branch_id':2,'subject_id':11,'lesson_type_id':1,'customer_id':7,'customer_verified':True,'subscription_id':40,'group_ids':[],'date':'2026-10-25','time':'15:00','status':1}
        for branch in (1,2):
            self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,paid_lesson_count,subscriptions,lessons,refreshed_at) "
                "VALUES (:w,:c,:b,7,1,8062.50,9,CAST(:subs AS jsonb),CAST(:lessons AS jsonb),:at)"),
                {'w':self.workspace,'c':self.crm,'b':branch,'subs':json.dumps([subscription]),'lessons':json.dumps([lesson]),'at':self.at+timedelta(hours=2)})
        person=reader.detail(None,self.workspace,self.contact)['students'][0]
        self.assertEqual(len(person['cards']),2)
        self.assertEqual(len(person['subscriptions']),1)
        self.assertEqual(person['subscriptions'][0]['branches'][0]['id'],'2')
        self.assertEqual(len(person['subscriptions'][0]['branches']),1)
        self.assertEqual(len(person['subscriptions'][0]['observed_branches']),2)
        self.assertEqual(person['paid_lesson_count'],9)
        self.assertEqual(person['balance'],'8062.50')
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_family_details WHERE workspace_id=:w'),{'w':self.workspace}).scalar(),2)

    def test_fresh_legacy_family_cache_gets_one_mapping_upgrade_without_waiting_thirty_minutes(self):
        reader,worker,student=self.family_worker_fixture()
        self.project(self.row())
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,subscriptions,refreshed_at) "
            "VALUES (:w,:c,1,7,1,100,CAST(:subs AS jsonb),now())"),{'w':self.workspace,'c':self.crm,'subs':json.dumps([{'id':40,'balance':'100'}])})
        worker.schedule(self.workspace)
        job=worker.claim(self.workspace)
        self.assertIsNotNone(job)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()):
            worker.process(self.workspace,job)
        self.finish_family_lessons(worker)
        worker.schedule(self.workspace)
        self.assertIsNone(worker.claim(self.workspace))
        self.assertFalse(reader.detail(None,self.workspace,self.contact)['students'][0]['cards'][0]['needs_mapping_refresh'])

    def test_family_balance_and_paid_count_use_newer_live_customer_details(self):
        reader,student=self.family_balance_fixture()
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,balance,paid_lesson_count,subscriptions,refreshed_at) VALUES (:w,:c,1,7,1,4123.75,5,CAST(:subs AS jsonb),:at)"),{'w':self.workspace,'c':self.crm,'at':self.at+timedelta(hours=2),'subs':json.dumps([{'id':40,'balance':'2725.00'}])})
        person=reader.detail(None,self.workspace,self.contact)['students'][0]
        self.assertEqual(person['balance'],'4123.75')
        self.assertEqual(person['cards'][0]['paid_lesson_count'],5)
        self.assertEqual(person['cards'][0]['subscriptions'][0]['balance'],'2725.00')
        # PostgreSQL returns timestamptz in UTC; the API may preserve an equivalent
        # offset in other adapters. Compare the instant, not its spelling.
        self.assertEqual(datetime.fromisoformat(person['cards'][0]['balance_updated_at']),
                         self.at+timedelta(hours=2))
        # A newly published customer row supersedes an older detail-cache total.
        self.snapshot['finished_at']=self.at+timedelta(hours=3)
        person=reader.detail(None,self.workspace,self.contact)['students'][0]
        self.assertEqual(person['balance'],'3962.50')
        self.assertEqual(person['cards'][0]['paid_lesson_count'],3)

    def test_family_balance_counts_global_customer_once_and_never_another_child(self):
        reader,student=self.family_balance_fixture()
        self.add_customer(self.contact,2,7)
        sibling=uuid4()
        self.db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES (:w,:id,'Sibling')"),{'w':self.workspace,'id':sibling})
        self.add_customer(self.contact,1,8)
        self.db.execute(text("UPDATE alfa_customers SET student_id=:student,balance=3962.50 WHERE workspace_id=:w AND external_id=7"),{'w':self.workspace,'student':student})
        self.db.execute(text("UPDATE alfa_customers SET student_id=:student,balance=-100.25 WHERE workspace_id=:w AND external_id=8"),{'w':self.workspace,'student':sibling})
        people={p['id']:p for p in reader.detail(None,self.workspace,self.contact)['students']}
        self.assertEqual(people[str(student)]['balance'],'3962.50')
        self.assertEqual(len(people[str(student)]['cards']),2)
        self.assertEqual(people[str(sibling)]['balance'],'-100.25')

    def test_queue_uses_latest_unanswered_for_Arina_and_resets_wait_after_new_incoming(self):
        old = self.at - timedelta(days=12)
        self.project(self.row('1', body='Question 1', at=old), self.row('2', body='Question 2', at=old+timedelta(minutes=1)))
        conversation = self.conversation()['id']
        original = self.reader.history(None, self.workspace, conversation)['conversation']
        self.project(self.row('10', direction='out', actor='automation', body='Notification', at=self.at))
        meta = self.reader.listing(None, self.workspace)['conversations'][0]
        for field in ('unread_count', 'priority_at', 'waiting_since', 'preview', 'preview_message_id'):
            self.assertEqual(meta[field], original[field])
        self.assertEqual(meta['unread_count'], 2)
        self.project(self.row('11', body='New question', at=self.at+timedelta(hours=1)))
        meta = self.reader.history(None, self.workspace, conversation)['conversation']
        self.assertEqual(meta['unread_count'], 3)
        self.assertEqual(meta['preview'], 'New question')
        self.assertEqual(meta['waiting_since'], self.at+timedelta(hours=1))
        self.assertEqual(meta['priority_at'], meta['waiting_since'])
        self.assertEqual(len(self.messages()), 4)

    def test_queue_older_import_keeps_source_priority_and_opening_does_not_answer(self):
        self.project(self.row('20'))
        conversation = self.conversation()['id']
        self.project(self.row('1', at=self.at-timedelta(days=30)))
        for _ in range(2):
            meta = self.reader.history(None, self.workspace, conversation)['conversation']
            self.assertEqual(meta['unanswered_count'], 2)
            self.assertEqual(meta['waiting_since'], self.at)

    def test_queue_attachment_unknown_author_and_failed_reply(self):
        attachment = self.row('1', body='')
        attachment['attachments'] = [{'name':'Voice note.ogg'}]
        self.project(attachment, self.row('2', direction='out', actor='unknown', at=self.at+timedelta(minutes=1)))
        meta = self.reader.listing(None, self.workspace)['conversations'][0]
        self.assertEqual(meta['preview'], 'Voice note.ogg')
        self.assertEqual(meta['unanswered_count'], 1)
        failed = self.row('3', direction='out', at=self.at+timedelta(minutes=2)); failed['failed'] = True
        self.project(failed)
        self.assertTrue(self.reader.history(None, self.workspace, self.conversation()['id'])['conversation']['unread'])

    def test_queue_handle_only_covers_the_seen_snapshot_and_invalid_boundary_is_rejected(self):
        self.project(self.row('1'))
        conversation = self.conversation()['id']
        seen = self.checkpoint(conversation)
        self.project(self.row('2', at=self.at+timedelta(seconds=1)))
        meta = self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, seen)['conversation']
        self.assertEqual(meta['unanswered_count'], 1)
        self.assertEqual(meta['seen_checkpoint'], seen.seen_checkpoint.model_dump(mode='json'))
        self.assertEqual(meta['priority_at'], self.at+timedelta(seconds=1))
        self.assertEqual(meta['preview'], 'Hello')
        invalid = self.checkpoint(conversation).model_copy(deep=True)
        invalid.seen_checkpoint.latest_inbound_id = uuid4()
        with self.assertRaises(AuthError) as error:
            self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, invalid)
        self.assertEqual(error.exception.status, 409)
        # An older retry is idempotent and cannot erase a later incoming.
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, seen)
        self.assertEqual(self.reader.history(None, self.workspace, conversation)['conversation']['unanswered_count'], 1)

    def test_queue_local_send_ack_does_not_expand_its_original_coverage(self):
        self.project(self.row('1'))
        conversation = self.conversation()['id']
        before = self.reader.history(None, self.workspace, conversation)['conversation']
        model = SendInput(request_id=uuid4(), text='Reply', seen_inbound_seq=before['inbound_seq'], seen_checkpoint=before['seen_checkpoint'])
        command = self.reader.send(SimpleNamespace(user_id=self.user), self.workspace, conversation, model)
        # Every non-confirmed state retains the pending request and old priority.
        for state in ('queued', 'sending', 'unknown', 'failed'):
            self.db.execute(text('UPDATE send_commands SET state=:state WHERE workspace_id=:w'), {'w':self.workspace,'state':state})
            refresh_unread(self.db, self.workspace, conversation)
            meta = self.reader.history(None, self.workspace, conversation)['conversation']
            self.assertEqual(meta['unanswered_count'], 1)
            self.assertEqual(meta['priority_at'], self.at)
        self.project(self.row('2', body='During send', at=self.at+timedelta(seconds=2)))
        for state in ('accepted', 'delivered', 'read'):
            self.db.execute(text('UPDATE send_commands SET state=:state WHERE workspace_id=:w'), {'w':self.workspace,'state':state})
            refresh_unread(self.db, self.workspace, conversation)
            meta = self.reader.history(None, self.workspace, conversation)['conversation']
            self.assertEqual(meta['unanswered_count'], 1)
            self.assertEqual(meta['preview'], 'During send')
        # Idempotent retry uses the same command even when new incoming exists.
        self.assertEqual(self.reader.send(SimpleNamespace(user_id=self.user), self.workspace, conversation, model)['command_id'], command['command_id'])
        self.db.execute(text("UPDATE send_commands SET state='failed' WHERE workspace_id=:w"), {'w':self.workspace})
        refresh_unread(self.db, self.workspace, conversation)
        self.assertEqual(self.reader.history(None, self.workspace, conversation)['conversation']['unanswered_count'], 2)

    def test_queue_native_echo_during_send_cannot_close_newer_incoming(self):
        self.project(self.row('1'))
        conversation = self.conversation()['id']
        local = self.local_reply(conversation)
        self.db.execute(text("UPDATE send_commands SET state='sending' WHERE workspace_id=:w"), {'w':self.workspace})
        self.project(self.row('2', body='During send', at=self.at+timedelta(seconds=2)))
        echo = self.row('30',direction='out',body='Reply',at=self.at+timedelta(seconds=5))
        self.project(echo)
        meta = self.reader.history(None,self.workspace,conversation)['conversation']
        self.assertEqual(meta['unanswered_count'], 2)
        self.db.execute(text("UPDATE send_commands SET state='accepted' WHERE workspace_id=:w"), {'w':self.workspace})
        refresh_unread(self.db,self.workspace,conversation)
        self.assertEqual(self.reader.history(None,self.workspace,conversation)['conversation']['unanswered_count'],1)
        self.project(echo)
        self.assertEqual(len(self.messages()),3)
        self.assertEqual(self.reader.history(None,self.workspace,conversation)['conversation']['unanswered_count'],1)
        self.assertTrue(any(m['id']==local and m['send_command_id'] for m in self.messages()))

    def test_queue_reply_coverage_follows_a_verified_incoming_mirror(self):
        self.project(self.row('102',branch=2),branch=2)
        conversation = self.conversation()['id']
        self.local_reply(conversation)
        refresh_unread(self.db,self.workspace,conversation)
        self.assertFalse(self.reader.history(None,self.workspace,conversation)['conversation']['needs_reply'])
        self.project(self.row('101',branch=1))
        meta = self.reader.history(None,self.workspace,conversation)['conversation']
        self.assertEqual(meta['unanswered_count'],0)
        self.assertEqual(len([m for m in self.messages() if m['direction']=='in']),1)

    def test_queue_manual_receipt_survives_reply_failure_without_new_freshness(self):
        self.project(self.row('1'))
        conversation = self.conversation()['id']
        self.reader.handle(SimpleNamespace(user_id=self.user), self.workspace, conversation, self.checkpoint(conversation))
        meta = self.reader.history(None, self.workspace, conversation)['conversation']
        self.assertFalse(meta['needs_reply'])
        self.assertIsNone(meta['waiting_since'])
        self.assertEqual(meta['priority_at'], self.at)
        self.local_reply(conversation)
        self.db.execute(text("UPDATE send_commands SET state='failed' WHERE workspace_id=:w"), {'w':self.workspace})
        refresh_unread(self.db, self.workspace, conversation)
        self.assertFalse(self.reader.history(None, self.workspace, conversation)['conversation']['needs_reply'])

    def test_queue_equal_times_in_separate_journals_do_not_prove_an_answer(self):
        self.project(self.row('1'), self.row('999', direction='out'))
        conversation = self.conversation()['id']
        self.assertTrue(self.reader.history(None, self.workspace, conversation)['conversation']['needs_reply'])
        self.db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('order_scope','verified-shared-sequence') WHERE workspace_id=:w"), {'w':self.workspace})
        refresh_unread(self.db, self.workspace, conversation)
        self.assertFalse(self.reader.history(None, self.workspace, conversation)['conversation']['needs_reply'])

    def test_imported_message_actor_and_body_are_immutable(self):
        self.project(self.row('1'), self.row('2', direction='out', actor='automation', at=self.at+timedelta(minutes=2)))
        conversation = self.conversation()['id']
        self.assertTrue(self.reader.history(None, self.workspace, conversation)['conversation']['needs_reply'])
        original = self.messages()[-1]
        self.project(self.row('2', direction='out', actor='human', at=original['occurred_at']))
        meta = self.reader.history(None, self.workspace, conversation)['conversation']
        self.assertTrue(meta['needs_reply'])
        self.assertEqual(meta['priority_at'], self.at)
        self.assertEqual(self.messages()[-1]['actor_type'], 'automation')
        self.assertEqual(self.messages()[-1]['id'], original['id'])
        self.assertEqual(len(self.messages()), 2)

    def test_queue_repair_is_resumable_and_keeps_history_worker_lease(self):
        from center_messenger.chat_queue import repair_batch as queue_repair
        self.project(self.row('1'))
        for n in range(5):
            self.add_customer(uuid4(), 1, 100+n)
            self.project(self.row(str(100+n), customer=100+n))
        lease = uuid4()
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor='{}' WHERE workspace_id=:w"), {'w':self.workspace})
        self.db.execute(text("INSERT INTO alfa_max_sync_queue(workspace_id,connection_id,lease_owner,lease_until) VALUES (:w,:c,:lease,clock_timestamp()+interval '1 minute')"), {'w':self.workspace,'c':self.channel,'lease':lease})
        self.db.execute(text("UPDATE conversations SET needs_reply=false,last_message_at=now() WHERE workspace_id=:w"), {'w':self.workspace})
        # Live fallback is correct before the first batch has run.
        self.assertEqual(self.reader.listing(None, self.workspace)['unread'], 6)
        self.assertTrue(queue_repair(self.db, self.workspace, self.channel, limit=2))
        progress = self.db.execute(text("SELECT capabilities->'chat_queue_projection' FROM connections WHERE workspace_id=:w AND id=:c"), {'w':self.workspace,'c':self.channel}).scalar_one()
        self.assertEqual(progress['processed'], 2)
        for _ in range(4):
            if not queue_repair(self.db, self.workspace, self.channel, limit=2): break
        self.assertEqual(self.reader.listing(None, self.workspace)['unread'], 6)
        self.assertEqual(self.db.execute(text('SELECT lease_owner FROM alfa_max_sync_queue WHERE workspace_id=:w'), {'w':self.workspace}).scalar_one(), lease)
        self.assertFalse(queue_repair(self.db, self.workspace, self.channel))
        self.assertEqual(self.db.execute(text('SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w'), {'w':self.workspace}).scalar_one(), {})

    def test_queue_repair_runs_under_runtime_rls_with_only_its_workspace_scope(self):
        from center_messenger.chat_queue import repair_batch as queue_repair
        self.project(self.row('1'))
        self.db.execute(text(f'GRANT USAGE ON SCHEMA "{self.schema}" TO cm_app'))
        self.db.execute(text('SET LOCAL ROLE cm_app'))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM conversations')).scalar_one(),0)
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(self.workspace)})
        self.assertFalse(queue_repair(self.db,self.workspace,self.channel))
        progress = self.db.execute(text("SELECT capabilities->'chat_queue_projection' FROM connections WHERE id=:c"),{'c':self.channel}).scalar_one()
        self.assertTrue(progress['complete'])
        self.assertEqual(progress['processed'],1)
        from center_messenger.chat_queue import invalidate_projection
        self.assertEqual(invalidate_projection(self.db,self.workspace),1)
        self.assertFalse(queue_repair(self.db,self.workspace,self.channel))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM messages')).scalar_one(),1)

    def test_queue_max_edit_preserves_a_manual_handling_receipt(self):
        bot = uuid4()
        self.db.execute(text("INSERT INTO connections(workspace_id,id,kind,name) VALUES (:w,:c,'max_bot','Bot')"),{'w':self.workspace,'c':bot})
        event = {'update_type':'message_created','timestamp':1792800000000,'message':{'timestamp':1792800000000,'sender':{'user_id':10,'is_bot':False,'first_name':'Parent'},'recipient':{'chat_type':'dialog','user_id':99},'body':{'mid':'max-1','text':'Question'}}}
        project_event(self.db,self.workspace,bot,99,event)
        conversation = self.db.execute(text('SELECT conversation_id FROM messages WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one()
        self.db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('handled_receipt',jsonb_build_object('version',1)) WHERE workspace_id=:w"),{'w':self.workspace})
        refresh_unread(self.db,self.workspace,conversation)
        event['update_type']='message_edited';event['timestamp']+=1000;event['message']['body']['text']='Edited question'
        project_event(self.db,self.workspace,bot,99,event)
        self.assertFalse(self.db.execute(text('SELECT needs_reply FROM conversations WHERE workspace_id=:w AND id=:c'),{'w':self.workspace,'c':conversation}).scalar_one())
        self.assertEqual(self.db.execute(text('SELECT text FROM messages WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one(),'Edited question')

    def test_queue_105_dialog_prefix_has_no_gaps_after_priority_moves(self):
        from center_messenger.chat_queue import repair_batch as queue_repair
        expected = []
        for n in range(105):
            self.add_customer(uuid4(),1,1000+n)
            incoming = self.row(str(1000+n),customer=1000+n,at=self.at+timedelta(microseconds=n))
            messages = [incoming]
            unread = n%3 != 0
            priority = n if unread else 1_000_000+n
            if not unread:
                messages.append(self.row(str(2000+n),customer=1000+n,direction='out',at=self.at+timedelta(microseconds=priority)))
            self.project(*messages)
            identifier = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND external_chat_id='alfa-contact:'||contact_id::text AND contact_id=(SELECT contact_id FROM alfa_customers WHERE workspace_id=:w AND run_id=:r AND branch_id=1 AND external_id=:customer)"),{'w':self.workspace,'r':self.run,'customer':1000+n}).scalar_one()
            expected.append([unread,priority,str(identifier)])
        ordered = lambda: [entry[2] for entry in sorted(expected,key=lambda entry:(not entry[0],-entry[1],entry[2]))]
        before = self.reader.listing(None,self.workspace,through_page=2)
        self.assertEqual([row['id'] for row in before['conversations']],ordered()[:90])
        self.assertFalse(queue_repair(self.db,self.workspace,self.channel,limit=200))
        self.assertEqual([row['id'] for row in self.reader.listing(None,self.workspace,through_page=2)['conversations']],ordered()[:90])
        handled = UUID(ordered()[0])
        self.reader.handle(SimpleNamespace(user_id=self.user),self.workspace,handled,self.checkpoint(handled))
        next(entry for entry in expected if entry[2]==str(handled))[0]=False
        rows = self.reader.listing(None,self.workspace,through_page=2)['conversations']
        self.assertEqual([row['id'] for row in rows],ordered()[:90])
        self.assertEqual(len({row['id'] for row in rows}),90)

    def test_queue_seen_handle_serializes_with_native_postgres_incoming_commit(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        if 'wasm' in self.db.execute(text('SELECT version()')).scalar_one().lower():
            self.skipTest('native PostgreSQL required for the actual concurrent writer/handle race')
        self.project(self.row('1'))
        conversation = self.conversation()['id']
        seen = self.checkpoint(conversation)
        self.transaction.commit()
        self.transaction = self.db.begin()
        entered = threading.Event()
        @contextmanager
        def native_transaction(*args, **kwargs):
            with self.engine.begin() as db:
                db.execute(text(f'SET search_path TO "{self.schema}",public'))
                if kwargs.get('lock'):
                    entered.set()
                    db.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w':self.workspace})
                yield db
        maximum = SimpleNamespace(chat_transaction=native_transaction, accounts=self.accounts)
        reader = ChatReader(SimpleNamespace(max=maximum, alfa_max=self.service, enqueue=Messaging.enqueue))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.engine.begin() as writer:
                writer.execute(text(f'SET search_path TO "{self.schema}",public'))
                writer.execute(text('SELECT id FROM workspaces WHERE id=:w FOR UPDATE'), {'w':self.workspace})
                self.service._project_page(writer,self.workspace,self.channel,self.crm,1,[self.row('2',body='Concurrent incoming',at=self.at+timedelta(seconds=1))])
                pending = pool.submit(reader.handle, SimpleNamespace(user_id=self.user), self.workspace, conversation, seen)
                self.assertTrue(entered.wait(2))
                self.assertFalse(pending.done())
            meta = pending.result(timeout=5)['conversation']
        self.assertEqual(meta['unanswered_count'], 1)
        self.assertEqual(meta['preview'], 'Concurrent incoming')
        self.assertEqual(meta['waiting_since'], self.at+timedelta(seconds=1))

    def test_personal_name_preferences_are_isolated_and_default_to_parent(self):
        from center_messenger import chat_preferences
        from center_messenger.chat_api import ChatPreferencesInput
        from pydantic import ValidationError
        other = uuid4()
        self.assertEqual(chat_preferences.read(self.db,self.workspace,self.user)['name_order'],'parent_first')
        chat_preferences.save(self.db,self.workspace,self.user,'child_first')
        self.assertEqual(chat_preferences.read(self.db,self.workspace,self.user)['name_order'],'child_first')
        self.assertEqual(chat_preferences.read(self.db,self.workspace,other)['name_order'],'parent_first')
        with self.assertRaises(ValidationError):
            ChatPreferencesInput(name_order='execute-code')
        self.assertEqual(self.reader.context(SimpleNamespace(user_id=self.user),self.workspace)['preferences']['name_order'],'child_first')

    def test_existing_source_text_time_attachments_and_version_stay_immutable(self):
        self.project(self.row('1',body='Original'))
        before = dict(self.messages()[0])
        changed_row = self.row('1',body='Edited in CRM',at=self.at+timedelta(days=1))
        changed_row['attachments'] = [{'name':'new file'}]
        self.project(changed_row)
        after = dict(self.messages()[0])
        self.assertEqual(after['text'],before['text'])
        self.assertEqual(after['occurred_at'],before['occurred_at'])
        self.assertEqual(after['payload'],before['payload'])
        self.assertEqual(after['version'],before['version'])

    def test_receipts_verify_static_initial_import_and_keep_duplicate_source_ambiguous(self):
        def page(request):
            return {'messages':[self.row('1')] if request==(1,'receive',0) else [],'has_more':False,'total':1 if request==(1,'receive',0) else 0}
        cursor=self.run_worker({},page_result=page)
        report=history_coverage(self.db,self.workspace,self.channel,cursor,[1,2])
        self.assertTrue(report['history_complete'])
        self.assertTrue(report['totals_verified'])
        self.assertEqual(cursor['proof:1:receive']['distinct_ids'],1)
        from center_messenger.history_receipts import new_records
        self.assertEqual(new_records(self.db,self.workspace,self.channel,1,[self.row('1')]),[])
        self.assertEqual(len(new_records(self.db,self.workspace,self.channel,1,[self.row('2')])),1)

    def test_repeated_source_rows_are_retained_without_false_missing_messages(self):
        def page(request):
            if request[:2] == (1,'receive'):
                return {'messages':[self.row('1')],'has_more':request[2]==0,'total':2,'row_count':1}
            return {'messages':[],'has_more':False,'total':0,'row_count':0}
        cursor=self.run_worker({},page_result=page)
        cursor=self.run_worker(cursor,page_result=page,after_seconds=3)
        report=history_coverage(self.db,self.workspace,self.channel,cursor,[1,2])
        self.assertTrue(report['history_loaded'])
        self.assertFalse(report['history_complete'])
        self.assertEqual(cursor['proof:1:receive']['status'],'source_repeats')
        self.assertEqual(cursor['proof:1:receive']['duplicate_rows'],1)
        self.assertEqual(report['streams'][0]['missing_rows'],0)
        self.assertEqual(len(self.messages()),1)

    def test_new_message_feed_includes_archived_clients_and_native_crm_replies(self):
        self.db.execute(text("UPDATE alfa_customers SET status='archive' WHERE workspace_id=:w"),{'w':self.workspace})
        self.project(self.row('1'))
        conversation=self.conversation()['id']
        self.assertTrue(self.reader.history(None,self.workspace,conversation)['conversation']['needs_reply'])
        self.project(self.row('2',direction='out',at=self.at+timedelta(seconds=10)))
        self.assertFalse(self.reader.history(None,self.workspace,conversation)['conversation']['needs_reply'])
        self.assertEqual(len(self.messages()),2)

    def test_counter_cache_avoids_recount_until_new_committed_data(self):
        self.project(self.row('1'))
        self.service.progress(None,self.workspace)
        with patch.object(self.db,'execute',wraps=self.db.execute) as execute:
            self.service.progress(None,self.workspace)
        expensive=[str(c.args[0]) for c in execute.call_args_list if 'count(DISTINCT' in str(c.args[0])]
        self.assertEqual(expensive,[])

    def lookup_worker(self, handler):
        from center_messenger.customer_lookup import CustomerLookupWorker
        old, client = self.active_review_worker(handler)
        return CustomerLookupWorker(old.connections, self.service), client

    def lookup_job(self, customer=99):
        from center_messenger.customer_lookup import TOPIC
        return self.db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w "
            "AND topic=:t AND event_key=:key"),
            {"w": self.workspace, "t": TOPIC, "key": f"{self.crm}:{customer}"}).scalar_one()

    def test_unknown_customer_is_immediately_visible_in_updates_and_enqueues_one_point_lookup(self):
        self.at = datetime.now(UTC)
        self.project(self.row("99", customer=99))
        first = self.reader.updates(None, self.workspace, after=0)
        self.assertEqual(len(first["updates"]), 1)
        self.assertTrue(first["updates"][0]["new_inbound"])
        self.assertEqual(first["updates"][0]["status"], "unlinked")
        self.project(self.row("99", customer=99), self.row("100", customer=99, body="Another question"))
        self.assertEqual(self.lookup_job()["branches"], [1])
        identity = self.reader.listing(None, self.workspace)["conversations"][0]["identity_resolution"]
        self.assertEqual(identity["customer_id"], "99")
        self.assertEqual(identity["state"], "queued")
        self.assertEqual(self.reader.listing(None, self.workspace, branch=1)["total"], 1)
        self.assertEqual(self.reader.listing(None, self.workspace, branch=2)["total"], 0)

    def test_point_lookup_links_new_lead_and_emits_change_after_consumed_cursor_without_realert(self):
        self.at = datetime.now(UTC)
        self.project(self.row("99", customer=99))
        before = self.messages()[0]
        cursor = self.reader.updates(None, self.workspace, after=0)["next_seq"]
        worker, client = self.lookup_worker(lambda path, body: {"items": [
            {"id": 99, "name": "New Child", "legal_name": "New Parent", "is_study": 0}], "total": 1})
        self.assertTrue(worker.step(self.workspace))
        self.assertEqual(self.lookup_job()["state"], "resolved")
        self.assertEqual(client.call.call_count, 1)
        self.assertEqual(client.call.call_args.args[1], {"id": 99, "is_study": 2, "removed": 0, "page": 0})
        after = self.messages()[0]
        self.assertEqual((after["id"], after["text"], after["occurred_at"]), (before["id"], before["text"], before["occurred_at"]))
        changes = self.reader.updates(None, self.workspace, after=cursor)["updates"]
        self.assertTrue(changes)
        self.assertFalse(any(c["new_inbound"] for c in changes))
        listing = self.reader.listing(None, self.workspace)["conversations"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["status"], "lead")
        self.assertIsNotNone(listing[0]["contact_id"])
        self.assertIsNone(listing[0]["identity_resolution"])
        self.assertFalse(worker.step(self.workspace))
        self.service.browser.assert_not_called()

    def test_lookup_merges_verified_branch_mirrors_but_preserves_source_rows(self):
        self.project(self.row("99", customer=99))
        self.project(self.row("100", customer=99, branch=2), branch=2)
        self.assertEqual(self.lookup_job()["branches"], [1, 2])
        worker, client = self.lookup_worker(lambda p, b: {"items": [{"id": 99, "name": "Child", "is_study": 1}], "total": 1})
        cursor = self.reader.updates(None, self.workspace, after=0)["next_seq"]
        worker.step(self.workspace)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(client.call.call_count, 1)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM source_records WHERE workspace_id=:w"), {"w": self.workspace}).scalar_one(), 2)
        self.assertFalse(any(u["new_inbound"] for u in self.reader.updates(None, self.workspace, cursor)["updates"]))

    def test_point_lookup_archive_fallback_never_requests_full_archive(self):
        self.project(self.row("99", customer=99))
        def handler(path, body):
            return {"items": [] if body["removed"] == 0 else [{"id": 99, "name": "Archived", "is_study": 0}],
                    "total": 0 if body["removed"] == 0 else 1}
        worker, client = self.lookup_worker(handler)
        worker.step(self.workspace)
        self.assertEqual([c.args[1]["removed"] for c in client.call.call_args_list], [0, 2])
        self.assertTrue(all(c.args[1]["id"] == 99 for c in client.call.call_args_list))
        self.assertEqual(self.reader.listing(None, self.workspace)["conversations"][0]["status"], "archive")

    def test_point_lookup_missing_and_unsupported_ids_are_distinct_and_keep_messages(self):
        for customer, response, state in ((99, {"items": [], "total": 0}, "not_found"),
                (100, {"items": [{"id": 7, "name": "Wrong card"}], "total": 1}, "unsupported_id")):
            self.project(self.row(str(customer), customer=customer))
            worker, client = self.lookup_worker(lambda p, b: response)
            worker.step(self.workspace)
            self.assertEqual(self.lookup_job(customer)["state"], state)
            self.assertFalse(worker.step(self.workspace))
        self.assertEqual(len(self.messages()), 2)
        self.assertTrue(all(c["contact_id"] is None for c in self.reader.listing(None, self.workspace)["conversations"]))

    def test_point_lookup_retries_network_and_respects_rate_limit(self):
        from center_messenger.alfa_client import AlfaError
        self.project(self.row("99", customer=99))
        def failing(p, b):
            raise AlfaError("rate_limited", 429, "900")
        worker, client = self.lookup_worker(failing)
        worker.step(self.workspace)
        value = self.lookup_job()
        self.assertEqual(value["state"], "retry")
        self.assertGreater((datetime.fromisoformat(value["next_at"]) - datetime.now(UTC)).total_seconds(), 890)
        self.assertFalse(worker.step(self.workspace))
        self.assertEqual(client.call.call_count, 1)
        self.assertEqual(len(self.messages()), 1)

    def test_point_lookup_rejects_settings_and_branch_changes_during_request(self):
        for change in ("UPDATE alfa_settings SET version=version+1 WHERE workspace_id=:w",
                       "UPDATE alfa_branches SET available=false WHERE workspace_id=:w AND external_id=1"):
            with self.subTest(change=change), self.db.begin_nested() as nested:
                self.project(self.row("99", customer=99))
                def handler(p, b):
                    self.db.execute(text(change), {"w": self.workspace})
                    return {"items": [{"id": 99, "name": "Stale", "is_study": 1}], "total": 1}
                worker, _ = self.lookup_worker(handler)
                self.assertFalse(worker.step(self.workspace))
                self.assertIsNone(self.conversation()["contact_id"])
                self.assertEqual(self.lookup_job()["state"], "queued")
                nested.rollback()

    def test_point_lookup_preserves_manual_binding_made_during_network_call(self):
        self.project(self.row("99", customer=99))
        original = self.conversation()["id"]
        def handler(p, b):
            self.db.execute(text("UPDATE conversations SET crm_link_manual=true,contact_id=:c WHERE workspace_id=:w AND id=:id"),
                {"w": self.workspace, "id": original, "c": self.contact})
            return {"items": [{"id": 99, "name": "Other card", "is_study": 1}], "total": 1}
        worker, _ = self.lookup_worker(handler)
        worker.step(self.workspace)
        self.assertEqual(self.messages()[0]["conversation_id"], original)
        self.assertEqual(self.conversation()["contact_id"], self.contact)
        self.assertTrue(self.conversation()["crm_link_manual"])

    def test_point_lookup_resumes_unknown_ids_saved_before_feature_and_enqueue_is_atomic(self):
        from center_messenger.customer_lookup import TOPIC
        with self.db.begin_nested() as tx:
            self.project(self.row("99", customer=99))
            tx.rollback()
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM outbox_events WHERE workspace_id=:w AND topic=:t"),
            {"w": self.workspace, "t": TOPIC}).scalar_one(), 0)
        self.project(self.row("99", customer=99))
        self.db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w AND topic=:t"), {"w": self.workspace, "t": TOPIC})
        worker, _ = self.lookup_worker(lambda p, b: {"items": [{"id": 99, "name": "Recovered", "is_study": 1}], "total": 1})
        worker.step(self.workspace)
        self.assertEqual(self.lookup_job()["state"], "resolved")

    def test_point_lookup_rejects_expired_lease_and_replaced_directory_snapshot(self):
        from center_messenger.customer_lookup import TOPIC
        for variant in ("expired", "snapshot"):
            with self.subTest(variant=variant), self.db.begin_nested() as tx:
                self.project(self.row("99", customer=99))
                def handler(p, b):
                    if variant == "expired":
                        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{lease_until}',to_jsonb(CAST(:until AS text))) "
                            "WHERE workspace_id=:w AND topic=:t"),
                            {"w": self.workspace, "t": TOPIC, "until": (datetime.now(UTC)-timedelta(seconds=1)).isoformat()})
                    else:
                        self.published.return_value = dict(self.snapshot, run=uuid4())
                    return {"items": [{"id":99,"name":"Stale","is_study":1}], "total":1}
                worker, _ = self.lookup_worker(handler)
                self.assertFalse(worker.step(self.workspace))
                self.assertIsNone(self.conversation()["contact_id"])
                self.published.return_value = self.snapshot
                tx.rollback()

    def test_point_lookup_lease_prevents_a_second_worker_fetch_and_scopes_other_workspaces(self):
        self.project(self.row("99", customer=99))
        def handler(p, b):
            second, client = self.lookup_worker(lambda p,b: self.fail("Concurrent duplicate request"))
            self.assertFalse(second.step(self.workspace))
            client.call.assert_not_called()
            self.assertFalse(second.step(uuid4()))
            return {"items":[{"id":99,"name":"Child","is_study":1}], "total":1}
        worker, _ = self.lookup_worker(handler)
        worker.step(self.workspace)
        self.assertEqual(self.lookup_job()["state"], "resolved")

    def test_point_lookup_credential_error_resumes_immediately_after_settings_change(self):
        from center_messenger.alfa_client import AlfaError
        self.project(self.row("99", customer=99))
        worker, client = self.lookup_worker(lambda p,b: None)
        client.call.side_effect = AlfaError("unauthorized", 401)
        worker.step(self.workspace)
        self.assertEqual(self.lookup_job()["state"], "credentials_required")
        self.assertFalse(worker.step(self.workspace))
        self.db.execute(text("UPDATE alfa_settings SET version=version+1 WHERE workspace_id=:w"), {"w": self.workspace})
        client.call.side_effect = lambda p,b:{"items":[{"id":99,"name":"Child","is_study":1}], "total":1}
        self.assertTrue(worker.step(self.workspace))
        self.assertEqual(self.lookup_job()["state"], "resolved")

    def test_unknown_message_without_id_stays_visible_with_explicit_reason(self):
        self.at = datetime.now(UTC)
        self.project(dict(self.row(), customers=[]))
        item = self.reader.listing(None, self.workspace)["conversations"][0]
        self.assertEqual(item["identity_resolution"]["state"], "missing_id")
        self.assertEqual(len(self.reader.updates(None, self.workspace, 0)["updates"]), 1)

    def active_review_worker(self, handler):
        from contextlib import nullcontext
        from center_messenger.active_review import ActiveReviewWorker
        client=SimpleNamespace(call=Mock(side_effect=handler))
        vault=Mock();vault.open.return_value={};vault.seal.return_value='test'
        connections=SimpleNamespace(accounts=self.accounts,vault=vault,
            _row=lambda db,w:db.execute(text('SELECT * FROM alfa_settings WHERE workspace_id=:w'),{'w':w}).mappings().one_or_none(),
            _host_gate=lambda origin:nullcontext(None),client_factory=lambda *args:client)
        return ActiveReviewWorker(connections),client

    def test_active_review_does_not_visit_archived_cards_and_waits_six_hours(self):
        from center_messenger import active_review
        self.add_customer(uuid4(),1,8)
        self.db.execute(text("UPDATE alfa_customers SET status='archive' WHERE workspace_id=:w AND external_id=8"),{'w':self.workspace})
        def handler(path,body):
            self.assertEqual(body['is_study'],1);self.assertEqual(body['removed'],0)
            rows=[{'id':7,'name':'Child updated','legal_name':'Parent','is_study':1,'phone':['79000000000']}] if '/1/' in path else []
            return {'items':rows,'total':len(rows),'page':body['page']}
        worker,client=self.active_review_worker(handler)
        for _ in range(4):self.assertTrue(worker.step(self.workspace))
        result=active_review.status(self.db,self.workspace,self.crm)
        self.assertEqual(result['state'],'complete')
        self.assertEqual(result['processed'],1)
        self.assertEqual(client.call.call_count,2)
        self.assertFalse(worker.step(self.workspace))
        self.assertEqual(client.call.call_count,2)
        self.assertEqual(self.db.execute(text('SELECT name FROM alfa_customers WHERE workspace_id=:w AND external_id=7'),{'w':self.workspace}).scalar(),'Child updated')
        self.assertEqual(len(self.messages()),0)

    def test_active_review_handles_small_source_pages_without_skipping_clients(self):
        from center_messenger import active_review
        def handler(path,body):
            rows=[{'id':i,'name':f'Child {i}','legal_name':'Parent','is_study':1} for i in ([7,9] if body['page']==0 else [10])] if '/1/' in path else []
            return {'items':rows,'total':3 if '/1/' in path else 0,'page':body['page']}
        worker,client=self.active_review_worker(handler)
        for _ in range(5):self.assertTrue(worker.step(self.workspace))
        result=active_review.status(self.db,self.workspace,self.crm)
        self.assertEqual(result['state'],'complete');self.assertEqual(result['processed'],3)
        self.assertEqual(len([c for c in client.call.call_args_list if '/1/' in c.args[0]]),2)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_customers WHERE workspace_id=:w AND branch_id=1'),{'w':self.workspace}).scalar(),3)

    def test_only_newly_archived_transition_is_checked_once(self):
        from center_messenger import active_review
        def handler(path,body):
            rows=[{'id':7,'name':'Child','is_study':1}] if body.get('id')==7 and body['removed']==2 else []
            return {'items':rows,'total':len(rows),'page':body['page']}
        worker,client=self.active_review_worker(handler)
        for _ in range(5):worker.step(self.workspace)
        result=active_review.status(self.db,self.workspace,self.crm)
        self.assertEqual(result['state'],'complete')
        self.assertEqual(self.db.execute(text('SELECT status FROM alfa_customers WHERE workspace_id=:w AND external_id=7'),{'w':self.workspace}).scalar(),'archive')
        active_review.request(self.db,self.workspace,self.crm)
        client.call.reset_mock()
        for _ in range(4):worker.step(self.workspace)
        self.assertTrue(all('id' not in call.args[1] for call in client.call.call_args_list))

    def test_active_review_rejects_wrong_customer_type_without_publishing(self):
        from center_messenger import active_review
        worker,client=self.active_review_worker(lambda p,b:{'items':[{'id':99,'name':'Wrong','is_study':0}],'total':1})
        self.assertFalse(worker.step(self.workspace))
        self.assertEqual(active_review.status(self.db,self.workspace,self.crm)['state'],'error')
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM alfa_customers WHERE workspace_id=:w AND external_id=99'),{'w':self.workspace}).scalar(),0)

    def test_missing_active_id_stays_visible_without_repeated_archive_probes(self):
        from center_messenger import active_review
        worker,client=self.active_review_worker(lambda p,b:{'items':[],'total':0})
        for _ in range(5):worker.step(self.workspace)
        self.assertEqual(active_review.status(self.db,self.workspace,self.crm)['unresolved'],1)
        active_review.request(self.db,self.workspace,self.crm);client.call.reset_mock()
        for _ in range(4):worker.step(self.workspace)
        self.assertEqual(active_review.status(self.db,self.workspace,self.crm)['unresolved'],1)
        self.assertTrue(all('id' not in call.args[1] for call in client.call.call_args_list))

    def test_completed_identity_repair_is_not_restarted_by_a_new_active_id(self):
        self.project(self.row('1'))
        cursor={}
        repair_batch(self.service,self.db,self.workspace,self.channel,cursor)
        cursor['sync_policy']=1
        previous=dict(cursor['client_repair'])
        self.add_customer(uuid4(),1,99)
        self.assertFalse(repair_batch(self.service,self.db,self.workspace,self.channel,cursor))
        self.assertEqual(cursor['client_repair'],previous)

    def test_incremental_policy_stops_automatic_archive_directory_import_but_allows_explicit_run(self):
        from contextlib import nullcontext
        from center_messenger.alfa_sync import AlfaSync, WorkCancelled
        connections=SimpleNamespace(accounts=self.accounts,_row=Mock(return_value={}))
        sync=AlfaSync(connections)
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:p AS jsonb) WHERE workspace_id=:w"),{'w':self.workspace,'p':json.dumps({'sync_policy':1})})
        control={'running_id':None,'published_id':self.snapshot['run'],'connection_id':self.crm}
        with patch.object(sync,'_work_transaction',side_effect=lambda *a:nullcontext((self.db,control))), patch.object(sync,'_job',side_effect=WorkCancelled) as full_job:
            sync._step(self.workspace,uuid4())
            full_job.assert_not_called()
            control['running_id']=uuid4()
            with self.assertRaises(WorkCancelled):sync._step(self.workspace,uuid4())
            full_job.assert_called_once()


    def test_same_account_reauthentication_retains_history_and_pause_but_not_live_activity(self):
        saved = {"sync_policy": 1, "1:receive": 19, "complete:1:sent": True, "archive_paused": True,
                 "activity": {"client": {"name": "Old operation"}}, "live_cycle": {"added_records": 1}}
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:p AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "p": json.dumps(saved)})
        old_cipher = self.db.execute(text("SELECT session_ciphertext FROM alfa_max_settings WHERE workspace_id=:w"), {'w': self.workspace}).scalar_one()
        connection, version = self.service._ensure_channel(None, self.workspace, "test@example.test", self.crm)
        self.assertEqual(version, 1)
        self.assertEqual(self.db.execute(text("SELECT session_ciphertext FROM alfa_max_settings WHERE workspace_id=:w"), {'w': self.workspace}).scalar_one(), old_cipher)
        crm = {'origin': 'https://test.s20.online', 'connection_id': self.crm, 'version': 1}
        self.service._crm = Mock(return_value=(crm, [1]))
        self.service.alfa._row = Mock(return_value=crm)
        self.service.status = Mock(return_value={})
        self.service.browser.login.return_value = ('active', {'cookies': []})
        self.service.browser.check_session.return_value = {'cookies': []}
        self.service.vault.seal_browser_state.return_value = 'new-session'
        self.service.connect(SimpleNamespace(user_id=self.user), self.workspace, 'test@example.test', SimpleNamespace(get_secret_value=lambda: 'synthetic-only'))
        version = self.db.execute(text('SELECT version FROM alfa_max_settings WHERE workspace_id=:w'), {'w': self.workspace}).scalar_one()
        cursor = self.db.execute(text("SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w"), {"w": self.workspace}).scalar_one()
        self.assertEqual(connection, self.channel)
        self.assertEqual(version, 2)
        self.assertEqual(cursor["1:receive"], 19)
        self.assertTrue(cursor["complete:1:sent"])
        self.assertTrue(cursor["archive_paused"])
        self.assertNotIn("activity", cursor)
        self.assertNotIn("live_cycle", cursor)

    def test_different_account_does_not_inherit_history_checkpoints(self):
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=CAST(:p AS jsonb) WHERE workspace_id=:w"),
                        {"w": self.workspace, "p": json.dumps({"complete:1:sent": True, "archive_paused": True})})
        self.service._ensure_channel(None, self.workspace, "other@example.test", self.crm)
        self.assertEqual(self.db.execute(text("SELECT history_cursor->>'complete:1:sent' FROM alfa_max_settings WHERE workspace_id=:w"), {'w': self.workspace}).scalar_one(), 'true')
        crm = {'origin': 'https://test.s20.online', 'connection_id': self.crm, 'version': 1}
        self.service._crm = Mock(return_value=(crm, [1]))
        self.service.alfa._row = Mock(return_value=crm)
        self.service.status = Mock(return_value={})
        self.service.browser.login.return_value = ('active', {'cookies': []})
        self.service.browser.check_session.return_value = {'cookies': []}
        self.service.vault.seal_browser_state.return_value = 'new-session'
        self.service.connect(SimpleNamespace(user_id=self.user), self.workspace, 'other@example.test', SimpleNamespace(get_secret_value=lambda: 'synthetic-only'))
        cursor = self.db.execute(text("SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w"), {"w": self.workspace}).scalar_one()
        self.assertNotIn('complete:1:sent', cursor)
        self.assertNotIn('archive_paused', cursor)

    def streaming_browser(self, page, before=None, after=None):
        def read(origin, session, requests, **kwargs):
            results = []
            for request in requests:
                if before:
                    before(request)
                if not kwargs["before_read"](request):
                    result = {"skipped": True}
                else:
                    kwargs["on_progress"]()
                    result = page(request)
                results.append(result)
                kwargs["on_result"](request, result, {})
                if after:
                    after(request, result)
            return results, {}
        return read

    def test_streamed_heads_are_visible_before_a_later_browser_failure(self):
        def page(request):
            if request[2] == 4:
                self.assertEqual(len(self.messages()), 1, "head was not committed before archive IO")
                raise AlfaBrowserError("crm_unavailable")
            return {"messages": [self.row("701")] if request == (1, "receive", 0) else [], "has_more": True}
        cursor = self.run_worker({"1:receive": 4}, browser_effect=self.streaming_browser(page))
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(cursor["1:receive"], 4)
        self.assertIn("live_checked:2:sent", cursor)

    def test_page_failure_retries_only_that_journal_while_healthy_feed_runs_at_normal_interval(self):
        def page(request):
            return {"error": "crm_unavailable"} if request == (1, "receive", 4) else {"messages": [], "has_more": False}
        cursor = self.run_worker({"1:receive": 4, "complete:1:sent": True,
            "complete:2:receive": True, "complete:2:sent": True}, browser_effect=self.streaming_browser(page))
        seconds = self.db.execute(text("SELECT EXTRACT(EPOCH FROM due_at-clock_timestamp()) FROM alfa_max_sync_queue WHERE workspace_id=:w"),
                                  {"w": self.workspace}).scalar_one()
        self.assertLessEqual(float(seconds), 15.1)
        self.assertGreater(float(seconds), 10)
        self.assertIn("retry_archive:1:receive", cursor)
        cursor = self.run_worker(cursor, browser_effect=self.streaming_browser(page), after_seconds=16)
        requests = self.service.browser.read_histories.call_args.args[2]
        self.assertNotIn((1, "receive", 4), requests)
        self.assertIn((2, "sent", 0), requests)
        self.assertEqual(cursor["1:receive"], 4)

    def test_pause_requested_during_io_preserves_checkpoint_but_keeps_new_heads(self):
        actor = SimpleNamespace(user_id=self.user)
        calls = []
        def before(request):
            if request[2] > 0:
                result = self.service.sync(actor, self.workspace, archive_paused=True)
                self.assertTrue(result["archive_paused"])
        def page(request):
            calls.append(request)
            return {"messages": [], "has_more": True}
        cursor = self.run_worker({"1:receive": 4}, browser_effect=self.streaming_browser(page, before=before))
        self.assertTrue(cursor["archive_paused"])
        self.assertEqual(cursor["1:receive"], 4)
        self.assertNotIn((1, "receive", 4), calls)
        self.assertTrue(all(f"live_checked:{b}:{direction}" in cursor for b in (1, 2) for direction in ("receive", "sent")))
        self.service.sync(actor, self.workspace, archive_paused=False)
        saved = self.db.execute(text("SELECT history_cursor FROM alfa_max_settings WHERE workspace_id=:w"), {"w": self.workspace}).scalar_one()
        self.assertFalse(saved["archive_paused"])
        self.assertEqual(saved["1:receive"], 4)

    def test_retry_requested_during_active_read_is_durable_and_does_not_rewind_completed_journals(self):
        requested = False
        def after(request, result):
            nonlocal requested
            if not requested:
                requested = True
                reply = self.service.sync(SimpleNamespace(user_id=self.user), self.workspace, branch=1, direction="receive")
                self.assertTrue(reply["already_running"])
        initial = {"1:receive": 4, "paused:1:receive": (datetime.now(UTC) + RECHECK_INTERVAL).isoformat(),
                   "error:1:receive": "invalid_response", "complete:2:sent": True}
        cursor = self.run_worker(initial, browser_effect=self.streaming_browser(
            lambda request: {"messages": [], "has_more": True}, after=after))
        self.assertIn("1:receive", cursor["retry_requests"])
        cursor = self.run_worker(cursor, browser_effect=self.streaming_browser(
            lambda request: {"messages": [], "has_more": False}))
        self.assertNotIn("retry_requests", cursor)
        self.assertIn((1, "receive", 4), self.service.browser.read_histories.call_args.args[2])
        self.assertTrue(cursor["complete:2:sent"])
        self.assertNotIn((2, "sent", 1), self.service.browser.read_histories.call_args.args[2])

    def prepare_card_monitor(self):
        student = uuid4()
        self.db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES(:w,:s,'Child')"),
                        {"w": self.workspace, "s": student})
        self.db.execute(text("UPDATE alfa_customers SET student_id=:s WHERE workspace_id=:w"),
                        {"w": self.workspace, "s": student})
        self.db.execute(text("INSERT INTO alfa_sync_control(workspace_id,connection_id,requested_by,published_id) VALUES(:w,:c,:u,:r)"),
                        {"w": self.workspace, "c": self.crm, "u": self.user, "r": self.run})
        return student

    def test_card_monitor_counts_one_family_and_requires_all_branch_sources_fresh(self):
        from center_messenger import family_cache
        self.add_customer(self.contact, 2, 7)
        self.prepare_card_monitor()
        self.project(self.row())
        for branch in (1, 2):
            self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,"
                "settings_version,balance,paid_lesson_count,subscriptions,lessons,lessons_refreshed_at,lessons_full_refreshed_at) "
                "VALUES(:w,:c,:b,7,1,0,0,'[]','[]',now(),now())"),
                {"w": self.workspace, "c": self.crm, "b": branch})
            report = family_cache.progress(self.db, self.workspace, self.crm, self.snapshot)
            self.assertEqual(report["total"], 1)
            self.assertEqual(report["ready"], int(branch == 2))
        self.db.execute(text("UPDATE alfa_family_details SET refreshed_at=now()-interval '31 minutes' WHERE workspace_id=:w AND branch_id=2"),
                        {"w": self.workspace})
        report = family_cache.progress(self.db, self.workspace, self.crm, self.snapshot)
        self.assertEqual(report["cached"], 1)
        self.assertEqual(report["ready"], 0)

    def test_card_monitor_only_exposes_a_current_leased_source_and_ignores_old_settings(self):
        from center_messenger import family_cache
        self.prepare_card_monitor()
        self.project(self.row())
        family_cache.enqueue(self.db, self.workspace, self.snapshot | {"version": 1})
        worker = family_cache.FamilyCacheWorker(SimpleNamespace(accounts=self.accounts))
        job = worker.claim(self.workspace)
        current = family_cache.current(self.db, self.workspace, self.crm)
        self.assertEqual(current["customer_id"], "7")
        self.assertEqual(current["name"], "Parent")
        report = family_cache.progress(self.db, self.workspace, self.crm, self.snapshot)
        self.assertEqual(report["queued"], 1)
        self.db.execute(text("UPDATE outbox_events SET payload=jsonb_set(payload,'{lease_until}',to_jsonb(CAST(:at AS text))) WHERE id=:id"),
                        {"at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "id": job["id"]})
        self.assertIsNone(family_cache.current(self.db, self.workspace, self.crm))
        self.db.execute(text("UPDATE alfa_settings SET version=version+1 WHERE workspace_id=:w"), {"w": self.workspace})
        self.assertEqual(family_cache.progress(self.db, self.workspace, self.crm, self.snapshot)["queued"], 0)

    def preferred_card_fixture(self):
        from center_messenger import card_preference
        reader, worker, _ = self.family_worker_fixture(include_groups=True)
        now = datetime.now(UTC)
        self.snapshot.update(finished_at=now-timedelta(hours=2), today=now.date(), timezone='UTC')
        active, student = uuid4(), uuid4()
        self.add_customer(active, 2, 9160)
        self.db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES(:w,:id,'Мария Забунова')"),
                        {'w':self.workspace,'id':student})
        self.db.execute(text("UPDATE alfa_customers SET external_id=9161,name='+79990000001',legal_name='+7 (999) 000-00-01',"
            "status='archive',phones='[\"+79990000001\"]'::jsonb WHERE workspace_id=:w AND contact_id=:source"),
            {'w':self.workspace,'source':self.contact})
        self.db.execute(text("UPDATE alfa_customers SET name='Мария Забунова',legal_name='Наталья Забунова',student_id=:student,"
            "phones='[\"+79990000001\"]'::jsonb WHERE workspace_id=:w AND external_id=9160"),
            {'w':self.workspace,'student':student})
        for contact,name in ((self.contact,'+79990000001'),(active,'Наталья Забунова')):
            self.db.execute(text("UPDATE contacts SET display_name=:name WHERE workspace_id=:w AND id=:c"),
                            {'w':self.workspace,'c':contact,'name':name})
            self.db.execute(text("UPDATE alfa_directory SET display_name=:name,phones='[\"+79990000001\"]'::jsonb,search_text=:name "
                "WHERE workspace_id=:w AND run_id=:r AND contact_id=:c"),
                {'w':self.workspace,'r':self.run,'c':contact,'name':name})
        self.db.execute(text("INSERT INTO alfa_groups(workspace_id,run_id,branch_id,external_id,name) VALUES(:w,:r,2,30,'Рисование')"),
                        {'w':self.workspace,'r':self.run})
        self.db.execute(text("INSERT INTO alfa_memberships(workspace_id,run_id,branch_id,group_id,external_id,customer_id,b_date,e_date) "
            "VALUES(:w,:r,2,30,40,9160,:begin,:end)"),
            {'w':self.workspace,'r':self.run,'begin':now.date()-timedelta(days=30),'end':now.date()+timedelta(days=90)})
        lesson={'id':50,'customer_id':'9160','branch_id':'2','date':(now+timedelta(days=1)).date().isoformat(),
                'time':'17:00','status':1,'group_ids':[30],'customer_verified':True,'observed_at':now.isoformat()}
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,"
            "balance,paid_lesson_count,subscriptions,lessons,lessons_refreshed_at,lessons_full_refreshed_at) "
            "VALUES(:w,:c,2,9160,1,487.50,1,'[]',CAST(:lessons AS jsonb),:now,:now)"),
            {'w':self.workspace,'c':self.crm,'lessons':json.dumps([lesson]),'now':now})
        self.project(self.row(record='700',customer=9161,branch=1,body='Мария сегодня была на занятии'))
        conversation=self.messages()[0]['conversation_id']
        return card_preference,reader,worker,active,student,conversation

    def test_card_preference_selects_live_customer_without_rewriting_source_or_reply_target(self):
        policy,reader,_,active,student,conversation=self.preferred_card_fixture()
        before=[(m['id'],m['conversation_id'],m['payload']) for m in self.messages()]
        self.assertEqual(policy.refresh(self.db,self.workspace,self.snapshot,[self.contact]),1)
        choice=policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)
        self.assertEqual((choice['customer'],choice['branch'],choice['reason']),('9160','2','scheduled_lesson'))
        listed=self.reader.listing(None,self.workspace)['conversations'][0]
        self.assertEqual(listed['name'],'Наталья Забунова')
        self.assertEqual(listed['status'],'client')
        self.assertEqual(listed['students'],['Мария Забунова'])
        self.assertEqual(listed['contact_id'],str(self.contact))
        self.assertEqual(listed['family_contact_id'],str(active))
        meta=self.reader.history(None,self.workspace,conversation)['conversation']
        self.assertEqual(meta['name'],listed['name'])
        self.assertEqual(meta['card_selection']['customer'],'9160')
        family=reader.detail(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(family['id'],str(active))
        self.assertEqual(family['source_contact_id'],str(self.contact))
        self.assertEqual(family['selection']['student'],str(student))
        self.assertEqual(len(family['students']),1)
        self.assertEqual(family['students'][0]['balance'],'487.50')
        self.assertTrue(family['students'][0]['cards'][0]['url'].endswith('id=9160'))
        self.assertTrue(family['cache']['auto_refresh'])
        target=self.service.send_target(self.workspace,self.channel,conversation)
        self.assertEqual((target['branch'],target['customer']),(1,9161))
        self.assertEqual(before,[(m['id'],m['conversation_id'],m['payload']) for m in self.messages()])

    def test_card_preference_open_prepares_only_the_displayed_active_family(self):
        _,reader,_,active,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("DELETE FROM alfa_family_details WHERE workspace_id=:w"),{'w':self.workspace})
        result=reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(result['id'],str(active))
        jobs=self.family_jobs()
        self.assertEqual({r['payload']['customer'] for r in jobs},{'9160'})
        self.assertEqual({r['payload']['contact'] for r in jobs},{str(active)})
        self.assertTrue(all(r['payload']['manual'] for r in jobs))

    def test_card_preference_background_cache_follows_pending_source_chat(self):
        from center_messenger import family_cache
        policy,_,worker,active,_,_=self.preferred_card_fixture()
        self.db.execute(text("DELETE FROM alfa_family_details WHERE workspace_id=:w"),{'w':self.workspace})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        family_cache.enqueue(self.db,self.workspace,self.snapshot)
        job=worker.claim(self.workspace)
        self.assertEqual(job['customer'],'9160')
        self.assertFalse(job['manual'])
        with patch('center_messenger.family_cache.fetch_family_finance',return_value=self.live_family_facts()) as remote:
            worker.process(self.workspace,job)
        remote.assert_called_once()
        self.assertEqual(remote.call_args.args[1:3],(2,9160))
        self.assertTrue(policy.is_pending(self.db,self.workspace,self.snapshot,[active]))
        self.assertFalse(policy.is_pending(self.db,self.workspace,self.snapshot,[self.contact]))
        report=family_cache.progress(self.db,self.workspace,self.crm,self.snapshot)
        self.assertEqual(report['total'],1)

    def test_pending_card_lookup_validates_aliases_with_repeated_branch_observations(self):
        policy,_,_,active,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,legal_name,"
            "legal_type,dob,phones,status,contact_id,student_id) SELECT workspace_id,run_id,1,external_id,name,legal_name,"
            "legal_type,dob,phones,status,contact_id,student_id FROM alfa_customers "
            "WHERE workspace_id=:w AND run_id=:r AND branch_id=2 AND external_id=9160"),
            {'w':self.workspace,'r':self.run})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.assertIsNotNone(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact))
        self.assertTrue(policy.is_pending(self.db,self.workspace,self.snapshot,iter([active])))
        self.assertFalse(policy.is_pending(self.db,self.workspace,self.snapshot,[self.contact]))
        self.assertFalse(policy.is_pending(self.db,self.workspace,self.snapshot,[uuid4()]))
        self.db.execute(text("UPDATE alfa_customers SET phones='[\"+79990000099\"]'::jsonb "
            "WHERE workspace_id=:w AND contact_id=:c"),{'w':self.workspace,'c':active})
        self.assertFalse(policy.is_pending(self.db,self.workspace,self.snapshot,[active]))
        self.assertTrue(policy.is_pending(self.db,self.workspace,self.snapshot,[self.contact]))

    def test_pending_card_candidate_hints_never_replace_the_validated_manual_link(self):
        policy,_,_,active,_,conversation=self.preferred_card_fixture()
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        hint={'source':'broken-legacy-id','contacts':[str(active)],'policy':policy.VERSION,
              'run':str(self.run),'version':self.snapshot['version']}
        self.db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,:t,'malformed-hint',CAST(:p AS jsonb),'published')"),
            {'w':self.workspace,'id':uuid4(),'t':policy.TOPIC,'p':json.dumps(hint)})
        self.assertTrue(policy.is_pending(self.db,self.workspace,self.snapshot,[active]))
        self.db.execute(text("UPDATE conversations SET crm_link_manual=true WHERE workspace_id=:w AND id=:c"),
                        {'w':self.workspace,'c':conversation})
        self.assertFalse(policy.is_pending(self.db,self.workspace,self.snapshot,[active]))
        self.assertTrue(policy.is_pending(self.db,self.workspace,self.snapshot,[self.contact]))

    def test_card_preference_fences_phone_changes_before_background_recalculation(self):
        policy,_,_,active,_,conversation=self.preferred_card_fixture()
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.db.execute(text("UPDATE alfa_customers SET phones='[\"+79990000002\"]'::jsonb WHERE workspace_id=:w AND contact_id=:c"),
                        {'w':self.workspace,'c':active})
        self.assertIsNone(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact))
        meta=self.reader.history(None,self.workspace,conversation)['conversation']
        self.assertEqual(meta['family_contact_id'],str(self.contact))

    def test_new_phone_owner_invalidates_guard_but_does_not_displace_a_higher_rank_teaching_client(self):
        policy,_,_,_,_,conversation=self.preferred_card_fixture()
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        other=uuid4();self.add_customer(other,2,9200)
        self.db.execute(text("UPDATE alfa_customers SET legal_name='Другой родитель',phones='[\"+79990000001\"]'::jsonb "
            "WHERE workspace_id=:w AND external_id=9200"),{'w':self.workspace})
        self.assertIsNone(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact))
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        choice=policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)
        self.assertEqual(choice['customer'],'9160')
        self.assertEqual(choice['reason'],'scheduled_lesson')
        self.assertIsNone(choice['ambiguous'])

    def test_equal_rank_current_clients_with_different_parents_are_not_selected_arbitrarily(self):
        policy, _, _, _, _, conversation = self.preferred_card_fixture()
        other = uuid4()
        self.add_customer(other, 2, 9200)
        self.db.execute(text("UPDATE alfa_customers SET legal_name='Different current parent',"
            "phones='[\"+79990000001\"]' WHERE workspace_id=:w AND external_id=9200"), {'w': self.workspace})
        self.db.execute(text("UPDATE alfa_family_details SET lessons='[]' WHERE workspace_id=:w"), {'w': self.workspace})
        self.db.execute(text('UPDATE alfa_memberships SET e_date=:yesterday WHERE workspace_id=:w'),
                        {'w': self.workspace, 'yesterday': self.snapshot['today'] - timedelta(days=1)})
        policy.refresh(self.db, self.workspace, self.snapshot, [self.contact])
        choice = policy.selected(self.db, self.workspace, self.snapshot, conversation, self.contact)
        self.assertEqual(choice['contact'], str(self.contact))
        self.assertEqual(choice['ambiguous'], 'conflicting_parents')

    def test_card_preference_preserves_manual_links_and_requires_the_source_conversation(self):
        policy,reader,_,_,_,conversation=self.preferred_card_fixture()
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.reader.link(SimpleNamespace(user_id=self.user),self.workspace,conversation,SimpleNamespace(contact_id=self.contact))
        self.assertIsNone(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact))
        family=reader.prepare(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(family['id'],str(self.contact))
        with self.assertRaises(AuthError):
            reader.detail(None,self.workspace,uuid4(),conversation=conversation)
        self.assertEqual(self.reader.history(None,self.workspace,conversation)['conversation']['card_selection'],{'mode':'manual'})

    def test_card_preference_keeps_siblings_and_their_balances_separate(self):
        policy,reader,_,_,_,conversation=self.preferred_card_fixture()
        other,student=uuid4(),uuid4();self.add_customer(other,2,9162)
        self.db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES(:w,:id,'Сын')"),{'w':self.workspace,'id':student})
        self.db.execute(text("UPDATE alfa_customers SET legal_name='Наталья Забунова',name='Сын',student_id=:s,balance=900,"
            "phones='[\"+79990000001\"]'::jsonb WHERE workspace_id=:w AND external_id=9162"),{'w':self.workspace,'s':student})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        family=reader.detail(None,self.workspace,self.contact,conversation=conversation)
        self.assertEqual(len(family['students']),2)
        self.assertEqual({s['name']:s['balance'] for s in family['students']},{'Мария Забунова':'487.50','Сын':'900.00'})
        self.assertEqual(family['selection']['customer'],'9160')
        self.assertEqual(set(self.reader.listing(None,self.workspace)['conversations'][0]['students']),{'Мария Забунова','Сын'})

    def test_card_preference_manual_link_survives_new_poll_and_canonical_repair(self):
        _,_,_,active,_,conversation=self.preferred_card_fixture()
        self.project(self.row(record='701',customer=9160,branch=2,body='Другой источник'),branch=2)
        self.reader.link(SimpleNamespace(user_id=self.user),self.workspace,conversation,SimpleNamespace(contact_id=active))
        self.project(self.row(record='702',customer=9161,branch=1,body='Новое сообщение'))
        self.project(self.row(record='703',customer=9160,branch=2,body='Новая запись клиента'),branch=2)
        linked=self.db.execute(text("SELECT contact_id,crm_link_manual FROM conversations WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':conversation}).mappings().one()
        self.assertEqual(linked['contact_id'],active)
        self.assertTrue(linked['crm_link_manual'])
        self.assertEqual(self.reader.history(None,self.workspace,conversation)['conversation']['id'],str(conversation))
        self.assertEqual({m['text'] for m in self.messages() if m['conversation_id']==conversation},
                         {'Мария сегодня была на занятии','Новое сообщение'})

    def test_card_preference_uses_real_membership_and_rejects_cancelled_or_other_customer_lessons(self):
        policy,_,_,_,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("UPDATE alfa_family_details SET lessons=jsonb_set(lessons,'{0,status}','2') WHERE workspace_id=:w"),
                        {'w':self.workspace})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.assertEqual(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)['reason'],'active_group')
        self.db.execute(text("UPDATE alfa_memberships SET e_date=:yesterday WHERE workspace_id=:w"),
                        {'w':self.workspace,'yesterday':self.snapshot['today']-timedelta(days=1)})
        self.db.execute(text("UPDATE alfa_family_details SET lessons=jsonb_set(jsonb_set(lessons,'{0,status}','1'),'{0,customer_id}','\"9161\"') "
            "WHERE workspace_id=:w"),{'w':self.workspace})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.assertEqual(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)['reason'],'active_client')

    def test_active_group_outranks_an_archived_real_named_lead_with_an_old_parent_label(self):
        policy, reader, _, active, _, conversation = self.preferred_card_fixture()
        self.db.execute(text("UPDATE alfa_customers SET name='Former lead',legal_name='Old short label' "
            "WHERE workspace_id=:w AND external_id=9161"), {'w': self.workspace})
        self.db.execute(text("UPDATE alfa_family_details SET lessons='[]' WHERE workspace_id=:w"), {'w': self.workspace})
        policy.refresh(self.db, self.workspace, self.snapshot, [self.contact])
        choice = policy.selected(self.db, self.workspace, self.snapshot, conversation, self.contact)
        self.assertEqual((choice['customer'], choice['reason']), ('9160', 'active_group'))
        self.assertEqual(choice['contact'], str(active))
        self.assertIsNone(choice['ambiguous'])
        family = reader.detail(None, self.workspace, self.contact, conversation=conversation)
        self.assertFalse(family['needs_review'])
        self.assertEqual({card['id'] for child in family['students'] for card in child['cards']}, {'9160'})

    def test_active_client_outranks_a_real_named_lead_after_names_were_corrected(self):
        policy, reader, _, _, _, conversation = self.preferred_card_fixture()
        self.db.execute(text("UPDATE alfa_customers SET name='Former lead',legal_name='Old parent',status='lead' "
            "WHERE workspace_id=:w AND external_id=9161"), {'w': self.workspace})
        policy.refresh(self.db, self.workspace, self.snapshot, [self.contact])
        choice = policy.selected(self.db, self.workspace, self.snapshot, conversation, self.contact)
        self.assertEqual(choice['customer'], '9160')
        self.assertEqual(len(reader.detail(None, self.workspace, self.contact, conversation=conversation)['students']), 1)

    def test_scanned_new_active_id_switches_existing_archive_chat_in_the_same_transaction(self):
        from center_messenger.active_review import apply_customer
        policy, _, _, _, _, conversation = self.preferred_card_fixture()
        self.db.execute(text('DELETE FROM alfa_customers WHERE workspace_id=:w AND external_id=9160'), {'w': self.workspace})
        policy.refresh(self.db, self.workspace, self.snapshot, [self.contact])
        self.assertEqual(policy.selected(self.db, self.workspace, self.snapshot, conversation, self.contact)['customer'], '9161')
        before = [(m['id'], m['text'], m['payload']['customer_id']) for m in self.messages()]
        apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 9160, '_branch': 2, 'name': 'Current pupil', 'legal_name': 'Current parent',
             'is_study': 1, 'phone': ['+79990000001']})
        choice = policy.selected(self.db, self.workspace, self.snapshot, conversation, self.contact)
        self.assertEqual((choice['customer'], choice['status']), ('9160', 'client'))
        listed = self.reader.listing(None, self.workspace)['conversations'][0]
        self.assertEqual((listed['name'], listed['status']), ('Current parent', 'client'))
        self.assertEqual([(m['id'], m['text'], m['payload']['customer_id']) for m in self.messages()], before)

    def test_direct_phone_matching_does_not_follow_the_candidates_other_phone_to_a_third_family(self):
        policy, _, _, active, _, conversation = self.preferred_card_fixture()
        self.db.execute(text("UPDATE alfa_customers SET phones='[\"+79990000002\",\"+79990000001\"]' "
            "WHERE workspace_id=:w AND external_id=9160"), {'w': self.workspace})
        third = uuid4()
        self.add_customer(third, 2, 9300)
        self.db.execute(text("UPDATE alfa_customers SET phones='[\"+79990000002\"]',legal_name='Unrelated parent' "
            "WHERE workspace_id=:w AND external_id=9300"), {'w': self.workspace})
        bounded = policy._catalog(self.db, self.workspace, self.snapshot, [self.contact])
        self.assertNotIn(str(third), bounded[0])
        policy.refresh(self.db, self.workspace, self.snapshot, [self.contact])
        choice = policy.selected(self.db, self.workspace, self.snapshot, conversation, self.contact)
        self.assertEqual(choice['contact'], str(active))
        self.assertNotIn('9300', json.dumps(choice))

    def test_all_phone_index_is_reversible_and_used_for_direct_secondary_phone_lookup(self):
        path = ROOT / 'app/backend/migrations/versions/0025_customer_phone_search.py'
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        with Operations.context(MigrationContext.configure(self.db)):
            module.downgrade()
            module.upgrade()
        self.db.execute(text('SET LOCAL enable_seqscan=off'))
        plan = '\n'.join(self.db.execute(text("EXPLAIN (COSTS OFF) SELECT external_id FROM alfa_customers "
            "WHERE phones ?| ARRAY['+79990000001']")).scalars())
        self.assertIn('alfa_customers_phones_gin_idx', plan)

    def test_card_preference_is_durable_and_fenced_by_settings_and_workspace(self):
        policy,_,_,_,_,conversation=self.preferred_card_fixture()
        policy.repair(self.db,self.workspace,self.snapshot,limit=1)
        first=policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)
        self.assertEqual(first['customer'],'9160')
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.assertEqual(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)['selected_at'],first['selected_at'])
        self.assertIsNone(policy.selected(self.db,uuid4(),self.snapshot,conversation,self.contact))
        self.db.execute(text("UPDATE alfa_settings SET version=2 WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertEqual(policy.refresh(self.db,self.workspace,self.snapshot,[self.contact]),0)
        self.snapshot['version']=2
        self.assertIsNone(policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact))

    def test_card_preference_filters_search_and_updates_use_the_same_active_card(self):
        policy,_,_,_,_,conversation=self.preferred_card_fixture()
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        self.assertEqual(self.reader.listing(None,self.workspace,status='client')['total'],1)
        self.assertEqual(self.reader.listing(None,self.workspace,status='archive')['total'],0)
        self.assertEqual(self.reader.listing(None,self.workspace,branch=2)['total'],1)
        self.assertEqual(self.reader.listing(None,self.workspace,query='Наталья')['total'],1)
        updates=self.reader.updates(None,self.workspace,after=0)['updates']
        self.assertEqual(updates[0]['name'],'Наталья Забунова')
        self.assertEqual(updates[0]['status'],'client')
        self.assertEqual(updates[0]['card_selection']['customer'],'9160')

    def test_card_preference_migration_recovers_existing_manual_links_and_is_reversible(self):
        _,_,_,_,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("INSERT INTO auth_events(id,workspace_id,user_id,action,subject_id) "
            "VALUES(:id,:w,:u,'chat_contact_linked',:c)"),
            {'id':uuid4(),'w':self.workspace,'u':self.user,'c':conversation})
        path=ROOT/'app/backend/migrations/versions/0016_crm_card_preference.py'
        spec=importlib.util.spec_from_file_location(path.stem,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        context=MigrationContext.configure(self.db)
        before=[m['id'] for m in self.messages()]
        with Operations.context(context):
            module.downgrade();module.upgrade()
        self.assertTrue(self.db.execute(text("SELECT crm_link_manual FROM conversations WHERE workspace_id=:w AND id=:c"),
                                       {'w':self.workspace,'c':conversation}).scalar_one())
        self.assertEqual(before,[m['id'] for m in self.messages()])
        self.assertTrue(self.db.execute(text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname='alfa_customers_primary_phone_idx')")).scalar_one())

    def test_card_preference_refresh_invalidates_a_cached_chat_list_window(self):
        policy,_,_,_,_,_=self.preferred_card_fixture()
        initial=self.reader.listing(None,self.workspace,window=90)
        self.assertEqual(initial['conversations'][0]['status'],'archive')
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        updated=self.reader.listing(None,self.workspace,window=90,window_token=initial['window_token'])
        self.assertFalse(updated.get('not_modified',False))
        self.assertEqual(updated['conversations'][0]['name'],'Наталья Забунова')
        unchanged=self.reader.listing(None,self.workspace,window=90,window_token=updated['window_token'])
        self.assertTrue(unchanged['not_modified'])

    def test_card_preference_global_id_observation_without_phone_keeps_parent_and_schedule(self):
        policy,_,_,active,student,conversation=self.preferred_card_fixture()
        self.add_customer(active,1,9160)
        self.db.execute(text("UPDATE alfa_customers SET name='Мария Забунова',legal_name='',student_id=:s "
            "WHERE workspace_id=:w AND branch_id=1 AND external_id=9160"),{'w':self.workspace,'s':student})
        lesson=self.family_facts()['lessons'][0]
        lesson=dict(lesson,branch_id='1',time='15:00')
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,settings_version,"
            "balance,lessons,lessons_refreshed_at) VALUES(:w,:c,1,9160,1,487.50,CAST(:lessons AS jsonb),now())"),
            {'w':self.workspace,'c':self.crm,'lessons':json.dumps([lesson])})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        choice=policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)
        self.assertEqual(choice['name'],'Наталья Забунова')
        self.assertEqual((choice['customer'],choice['branch'],choice['reason']),('9160','1','scheduled_lesson'))

    def test_card_preference_does_not_treat_shared_business_phone_as_one_family(self):
        policy,reader,_,_,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("UPDATE alfa_customers SET legal_type=2 WHERE workspace_id=:w AND external_id=9160"),{'w':self.workspace})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        choice=policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)
        self.assertEqual(choice['contact'],str(self.contact))
        self.assertEqual(choice['ambiguous'],'shared_business_phone')
        self.assertTrue(reader.detail(None,self.workspace,self.contact,conversation=conversation)['needs_review'])

    def test_card_preference_accepts_a_direct_secondary_phone_without_changing_the_reply_target(self):
        policy,_,_,active,_,conversation=self.preferred_card_fixture()
        self.db.execute(text("UPDATE alfa_customers SET phones='[\"+79990000002\",\"+79990000001\"]'::jsonb "
            "WHERE workspace_id=:w AND external_id=9160"),{'w':self.workspace})
        policy.refresh(self.db,self.workspace,self.snapshot,[self.contact])
        choice=policy.selected(self.db,self.workspace,self.snapshot,conversation,self.contact)
        self.assertEqual(choice['contact'],str(active))
        self.assertEqual(choice['customer'],'9160')
        target=self.service.send_target(self.workspace,self.channel,conversation)
        self.assertEqual(target['customer'],9161)

    def test_stored_history_count_tracks_superseding_moves_and_rollback(self):
        self.project(self.row('1'), self.row('2'), self.row('3'))
        first = self.conversation()['id']
        self.assertEqual(self.reader.history(None, self.workspace, first)['total'], 3)
        message = self.messages()[0]['id']
        self.db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('superseded_by','synthetic') WHERE id=:m"), {'m':message})
        self.assertEqual(self.reader.history(None, self.workspace, first)['total'], 2)
        self.db.execute(text("UPDATE messages SET payload=payload-'superseded_by' WHERE id=:m"), {'m':message})
        self.add_customer(uuid4(), 1, 8)
        self.project(self.row('4', customer=8))
        second = self.db.execute(text('SELECT id FROM conversations WHERE workspace_id=:w AND id<>:c'), {'w':self.workspace,'c':first}).scalar_one()
        self.db.execute(text('UPDATE messages SET conversation_id=:c WHERE id=:m'), {'c':second,'m':message})
        self.assertEqual(self.reader.history(None,self.workspace,first)['total'],2)
        self.assertEqual(self.reader.history(None,self.workspace,second)['total'],2)
        with self.assertRaises(RuntimeError), self.db.begin_nested():
            self.project(self.row('5',customer=8))
            raise RuntimeError('rollback')
        self.assertEqual(self.reader.history(None,self.workspace,second)['total'],2)

    def test_bootstrap_returns_the_filtered_first_chat_and_forty_chronological_messages(self):
        self.project(*[self.row(str(n), body=f'Message {n}', at=self.at+timedelta(minutes=n)) for n in range(1,61)])
        actor=SimpleNamespace(user_id=self.user)
        result=self.reader.bootstrap(actor,self.workspace,branch=1)
        self.assertIn('preferences',result['context'])
        self.assertEqual(result['listing']['total'],1)
        history=result['history']
        self.assertEqual(len(history['messages']),40)
        self.assertEqual(history['total'],60)
        boundary=history['messages'][0]
        earlier=self.reader.history(None,self.workspace,UUID(history['conversation']['id']),
            before_id=UUID(boundary['id']),before_time=boundary['occurred_at'],limit=40)
        self.assertEqual(len(earlier['messages']),20)
        self.assertFalse(earlier['has_more'])
        ids=[m['id'] for m in earlier['messages']+history['messages']]
        self.assertEqual(len(set(ids)),60)
        empty=self.reader.bootstrap(actor,self.workspace,branch=2)
        self.assertEqual(empty['listing']['conversations'],[])
        self.assertIsNone(empty['history'])

    def test_unchanged_list_uses_a_small_version_read_without_aggregating_all_chat_rows(self):
        self.project(self.row())
        first=self.reader.listing(None,self.workspace,window=90)
        statements=[]
        original=self.db.execute
        def record(statement,*args,**kwargs):
            statements.append(str(statement));return original(statement,*args,**kwargs)
        with patch.object(self.db,'execute',side_effect=record):
            same=self.reader.listing(None,self.workspace,window=90,window_token=first['window_token'])
        self.assertTrue(same['not_modified'])
        self.assertTrue(any('chat_read_versions' in sql for sql in statements))
        self.assertFalse(any('jsonb_agg' in sql or 'count(*) FROM messages' in sql for sql in statements))
        before=self.reader.event_revision(None,self.workspace)
        self.db.execute(text("UPDATE contacts SET display_name='Renamed' WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertNotEqual(self.reader.event_revision(None,self.workspace)['version'],before['version'])

    def test_history_projection_falls_back_for_missing_rows_and_cannot_authorize_handling(self):
        self.project(self.row('1'), self.row('2'))
        conversation = self.conversation()['id']
        before = self.reader.history(None,self.workspace,conversation)
        self.assertEqual(before['conversation']['unread_count'],2)
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object("
            "'needs_reply',false,'unanswered_count',0,'latest_inbound_id',NULL) "
            "WHERE workspace_id=:w AND topic='chat.list.state' AND event_key=:c"),
            {'w':self.workspace,'c':str(conversation)})
        projected = self.reader.history(None,self.workspace,conversation)
        self.assertFalse(projected['conversation']['unread'])
        with self.assertRaises(AuthError) as denied:
            self.reader.handle(SimpleNamespace(user_id=self.user),self.workspace,conversation,
                HandleInput(seen_checkpoint=projected['conversation']['seen_checkpoint']))
        self.assertEqual(denied.exception.status,409)
        self.db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w "
            "AND topic='chat.list.state' AND event_key=:c"),{'w':self.workspace,'c':str(conversation)})
        fallback = self.reader.history(None,self.workspace,conversation)
        self.assertEqual(fallback['conversation']['unread_count'],2)
        self.assertEqual([m['id'] for m in fallback['messages']],[m['id'] for m in before['messages']])

    def test_compact_card_keeps_cached_reads_small_and_checks_contact_access_before_cache_hits(self):
        reader, worker, _=self.family_worker_fixture()
        self.project(self.row())
        conversation=self.conversation()['id']
        result=reader.prepare(None,self.workspace,self.contact,conversation=conversation,compact=True)
        self.assertIn('version',result)
        self.assertNotIn('lessons',result['students'][0]['cards'][0])
        with patch.object(reader,'detail',wraps=reader.detail) as full:
            same=reader.prepare(None,self.workspace,self.contact,conversation=conversation,cached_only=True,
                compact=True,known_version=result['version'])
            self.assertTrue(same['not_modified'])
            full.assert_not_called()
        with self.assertRaises(AuthError):
            reader.prepare(None,self.workspace,uuid4(),conversation=conversation,cached_only=True,
                compact=True,known_version=result['version'])
        self.db.execute(text("UPDATE contacts SET display_name='Changed family' WHERE workspace_id=:w AND id=:c"),
            {'w':self.workspace,'c':self.contact})
        with patch.object(reader,'detail',wraps=reader.detail) as full:
            reader.prepare(None,self.workspace,self.contact,conversation=conversation,cached_only=True,
                compact=True,known_version=result['version'])
            full.assert_called_once()

    def test_read_version_migration_is_reversible_and_backfills_existing_message_counts(self):
        self.project(self.row())
        before=[m['id'] for m in self.messages()]
        self.db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        path=ROOT/'app/backend/migrations/versions/0017_chat_read_versions.py'
        spec=importlib.util.spec_from_file_location(path.stem,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        later_path=ROOT/'app/backend/migrations/versions/0020_lesson_history_coverage.py'
        later_spec=importlib.util.spec_from_file_location(later_path.stem,later_path)
        later=importlib.util.module_from_spec(later_spec);later_spec.loader.exec_module(later)
        with Operations.context(MigrationContext.configure(self.db)):
            # The later status trigger depends on the version function. Roll it
            # back before its parent, as Alembic does in the actual graph.
            later.downgrade();module.downgrade();module.upgrade();later.upgrade()
        self.assertEqual([m['id'] for m in self.messages()],before)
        self.assertEqual(self.reader.history(None,self.workspace,self.conversation()['id'])['total'],1)
        self.assertTrue(self.db.execute(text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname='messages_chat_order_idx')")).scalar_one())

    def test_voice_duration_migration_preserves_keys_and_enforces_the_setting(self):
        from center_messenger.ai_voice import AiVoice
        self.db.execute(text("INSERT INTO ai_settings(workspace_id,folder_id,key_ciphertext,voice_enabled) "
                             "VALUES(:w,'b1gtestfolder','sealed-yandex',true)"),{'w':self.workspace})
        self.assertEqual(AiVoice.voice_duration(self.db,self.workspace),180)
        self.db.execute(text("UPDATE ai_settings SET voice_max_seconds=75 WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertEqual(AiVoice.voice_duration(self.db,self.workspace),75)
        for value in (14,181):
            savepoint=self.db.begin_nested()
            try:
                with self.assertRaises(Exception):
                    self.db.execute(text("UPDATE ai_settings SET voice_max_seconds=:v WHERE workspace_id=:w"),{'w':self.workspace,'v':value})
            finally:
                savepoint.rollback()
        path=ROOT/'app/backend/migrations/versions/0018_voice_duration.py'
        spec=importlib.util.spec_from_file_location('voice_migration',path)
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(self.db)):
            migration.downgrade();migration.upgrade()
        row=self.db.execute(text('SELECT voice_max_seconds,key_ciphertext,voice_enabled FROM ai_settings WHERE workspace_id=:w'),{'w':self.workspace}).one()
        self.assertEqual(tuple(row),(180,'sealed-yandex',True))

    def test_gateway_sql_routes_and_migration_preserve_existing_voice_settings(self):
        from center_messenger.ai_voice import AiVoice
        self.db.execute(text("INSERT INTO ai_settings(workspace_id,folder_id,key_ciphertext,voice_enabled,voice_max_seconds,deepseek_key_ciphertext) "
                             "VALUES(:w,'b1gtestfolder','sealed-yandex',true,75,'sealed-deepseek')"),{'w':self.workspace})
        self.assertTrue(AiVoice.voice_available(self.db,self.workspace))
        self.db.execute(text("UPDATE ai_settings SET gateway_config='{" + '"recognition_provider":"mimo_plan"' + "}'::jsonb WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertFalse(AiVoice.voice_available(self.db,self.workspace))
        self.db.execute(text("UPDATE ai_settings SET mimo_plan_key_ciphertext='sealed-plan' WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertTrue(AiVoice.voice_available(self.db,self.workspace))
        path=ROOT/'app/backend/migrations/versions/0019_ai_gateways.py'
        spec=importlib.util.spec_from_file_location('gateway_migration',path)
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(self.db)):
            migration.downgrade();migration.upgrade()
        row=self.db.execute(text('SELECT voice_max_seconds,key_ciphertext,deepseek_key_ciphertext,voice_enabled,gateway_config FROM ai_settings WHERE workspace_id=:w'),{'w':self.workspace}).one()
        self.assertEqual(tuple(row),(75,'sealed-yandex','sealed-deepseek',True,{}))

    def test_gateway_storage_seals_keys_routes_actions_and_preserves_independent_connections(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from center_messenger.ai_voice import AiVoice
        from center_messenger.auth_security import AuthError
        from center_messenger.connector_vault import ConnectorVault
        from unittest.mock import patch
        vault=ConnectorVault(None)
        vault._key=lambda: b"t"*32
        outer=self
        class Accounts:
            @contextmanager
            def transaction(self,*args,**kwargs):yield outer.db
            def _workspace(self,*args,**kwargs):return {'role':'owner'}
            def _workspace_chat(self,*args,**kwargs):return {'role':'owner'}
            def throttle(self,*args,**kwargs):pass
        service=AiVoice(SimpleNamespace(accounts=Accounts(),vault=vault))
        actor=SimpleNamespace(user_id=self.user)
        try:
            self.db.execute(text("INSERT INTO ai_settings(workspace_id,folder_id,key_ciphertext,voice_enabled) VALUES(:w,'b1gtestfolder','sealed-yandex',true)"),{'w':self.workspace})
            result=service.gateways.save(actor,self.workspace,'mimo_plan',{'api_key':'tp-'+'x'*30})
            self.assertTrue(result['gateways']['mimo_plan']['configured'])
            from center_messenger.ai_mimo import MimoClient
            from test_ai_yandex import FakeOpener
            service.mimo=MimoClient(FakeOpener({'choices':[{'message':{'content':'Ready'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':4,'completion_tokens':2}}))
            result=service.gateways.verify(actor,self.workspace,'mimo_plan','text')
            self.assertTrue(result['ok']);self.assertTrue(result['gateways']['mimo_plan']['checks']['text']['ok'])
            self.assertNotIn('recognition',result['gateways']['mimo_plan']['checks'])
            service.gateways.verify(actor,self.workspace,'mimo_plan','recognition')
            result=service.settings(actor,self.workspace)
            self.assertEqual(set(result['gateways']['mimo_plan']['checks']),{'text','recognition'})
            self.assertNotIn('tp-'+'x'*30,json.dumps(result))
            self.assertEqual(self.db.execute(text('SELECT key_ciphertext FROM ai_settings WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one(),'sealed-yandex')
            service.gateways.routes(actor,self.workspace,{'recognition_provider':'mimo_plan','recognition_language':'en','synthesis_provider':'mimo_plan','synthesis_voice':'Mia'})
            self.assertTrue(service.voice_available(self.db,self.workspace))
            result=service.gateways.save(actor,self.workspace,'mimo_plan',{'clear_key':True})
            self.assertFalse(result['voice_enabled']);self.assertEqual(result['gateways']['synthesis_provider'],'disabled')
            self.assertFalse(result['gateways']['mimo_plan']['configured'])
            self.assertTrue(result['configured'])
            with self.assertRaises(AuthError):
                service.gateways.save(actor,self.workspace,'unknown',{'api_key':'fake'})
        finally:service.jobs.pool.shutdown(wait=True)

    def test_synthesis_routes_use_the_selected_key_voice_and_billing_and_reset_checks_on_rotation(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        import base64,io,wave
        from center_messenger.ai_voice import AiVoice
        from center_messenger.ai_yandex import YandexClient,silence
        from center_messenger.ai_mimo import MimoClient
        from center_messenger.connector_vault import ConnectorVault
        from test_ai_yandex import FakeOpener
        outer=self
        class Accounts:
            @contextmanager
            def transaction(self,*args,**kwargs):yield outer.db
            def _workspace(self,*args,**kwargs):return {'role':'owner'}
            def _workspace_chat(self,*args,**kwargs):return {'role':'owner'}
            def throttle(self,*args,**kwargs):pass
        vault=ConnectorVault(None);vault._key=lambda:b't'*32
        wire=FakeOpener(silence(1))
        service=AiVoice(SimpleNamespace(accounts=Accounts(),vault=vault),client=YandexClient(wire))
        actor=SimpleNamespace(user_id=self.user)
        try:
            service.save_settings(actor,self.workspace,{'api_key':'AQVN'+'x'*36,'folder_id':'b1gtestfolder'})
            service.gateways.routes(actor,self.workspace,{'recognition_provider':'yandex','recognition_language':'ru','synthesis_provider':'yandex','synthesis_voice':'marina'})
            result=service.gateways.synthesize(actor,self.workspace,'Здравствуйте.')
            self.assertEqual(result['provider'],'yandex');self.assertEqual(result['billing'],'pay_as_you_go');self.assertGreater(result['cost_rub'],0)
            checked=service.gateways.verify(actor,self.workspace,'yandex','synthesis')
            self.assertTrue(checked['gateways']['yandex']['checks']['synthesis']['ok'])
            rotated=service.save_settings(actor,self.workspace,{'api_key':'AQVN'+'z'*36})
            self.assertEqual(rotated['gateways']['yandex']['checks'],{})
            self.assertEqual(rotated['gateways']['synthesis_voice'],'marina')
            output=io.BytesIO()
            with wave.open(output,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(silence(1))
            mimo_wire=FakeOpener({'choices':[{'message':{'audio':{'data':base64.b64encode(output.getvalue()).decode()}},'finish_reason':'stop'}]})
            service.mimo=MimoClient(mimo_wire)
            service.gateways.save(actor,self.workspace,'mimo_plan',{'api_key':'tp-'+'p'*30})
            service.gateways.routes(actor,self.workspace,{'recognition_provider':'yandex','recognition_language':'ru','synthesis_provider':'mimo_plan','synthesis_voice':'Dean'})
            result=service.gateways.synthesize(actor,self.workspace,'Hello.')
            self.assertEqual(result['billing'],'subscription');self.assertEqual(result['cost_rub'],0)
            self.assertEqual(json.loads(mimo_wire.requests[0][0].data)['audio']['voice'],'Dean')
            self.assertEqual(mimo_wire.requests[0][0].get_header('Api-key'),'tp-'+'p'*30)
        finally:service.jobs.pool.shutdown(wait=True)

    def test_active_review_discovers_a_promoted_lead_without_restarting_message_history(self):
        self.customer_phone(7, parent='Old parent', status='lead')
        self.project(self.row())
        old = dict(self.conversation())
        def handler(path, body):
            self.assertEqual((body['is_study'], body['removed']), (1, 0))
            rows = [{'id': 7, 'name': 'Current pupil', 'legal_name': 'Current parent', 'is_study': 1}] if '/1/' in path else []
            return {'items': rows, 'total': len(rows), 'page': body['page']}
        worker, _ = self.active_review_worker(handler)
        for _ in range(4):
            self.assertTrue(worker.step(self.workspace))
        current = self.db.execute(text('SELECT status,contact_id FROM alfa_customers WHERE workspace_id=:w'),
                                  {'w': self.workspace}).mappings().one()
        self.assertEqual((current['status'], current['contact_id']), ('client', self.contact))
        self.assertEqual(self.conversation()['inbound_seq'], old['inbound_seq'])
        self.assertEqual(len(self.messages()), 1)

    def test_missing_optional_fields_in_a_new_branch_reuse_exact_id_facts(self):
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        self.customer_phone(7, parent='Known parent')
        fact = apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'name': 'Pupil', 'is_study': 1, '_branch': 2})
        current = self.db.execute(text('SELECT phones,legal_name FROM alfa_customers WHERE workspace_id=:w AND branch_id=2'),
                                  {'w': self.workspace}).mappings().one()
        self.assertEqual(fact['contact_id'], str(self.contact))
        self.assertEqual((current['phones'], current['legal_name']), (['+79160000000'], 'Known parent'))

    def test_customer_label_repair_is_bounded_idempotent_and_fenced_by_published_run(self):
        from center_messenger import customer_updates
        self.family_worker_fixture()
        self.db.execute(text("UPDATE alfa_customers SET legal_name='Repaired parent' WHERE workspace_id=:w"), {'w': self.workspace})
        self.assertEqual(customer_updates.repair_labels(self.db, self.workspace, self.snapshot, limit=1), 1)
        self.assertEqual(customer_updates.repair_labels(self.db, self.workspace, self.snapshot, limit=1), 0)
        self.assertEqual(self.db.execute(text('SELECT display_name FROM contacts WHERE workspace_id=:w AND id=:c'),
            {'w': self.workspace, 'c': self.contact}).scalar_one(), 'Repaired parent')
        self.assertEqual(customer_updates.repair_labels(self.db, self.workspace, dict(self.snapshot, run=uuid4())), 0)

    def test_customer_rename_updates_parent_pupil_search_and_clears_only_derived_review(self):
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        self.project(self.row())
        original = dict(self.messages()[0])
        state = dict(self.conversation())
        self.db.execute(text('UPDATE alfa_customers SET needs_review=true WHERE workspace_id=:w'), {'w': self.workspace})
        fact = apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'name': 'New pupil', 'legal_name': 'New parent', 'is_study': 1, '_branch': 1})
        self.assertEqual(fact['contact_id'], str(self.contact))
        self.assertEqual(self.db.execute(text('SELECT display_name FROM contacts WHERE workspace_id=:w AND id=:c'),
            {'w': self.workspace, 'c': self.contact}).scalar_one(), 'New parent')
        catalog = self.db.execute(text('SELECT display_name,search_text,needs_review FROM alfa_directory WHERE workspace_id=:w'),
            {'w': self.workspace}).mappings().one()
        self.assertEqual(catalog['display_name'], 'New parent')
        self.assertIn('new parent', catalog['search_text'])
        self.assertIn('new pupil', catalog['search_text'])
        self.assertFalse(catalog['needs_review'])
        self.assertEqual(self.messages()[0]['id'], original['id'])
        self.assertEqual(self.messages()[0]['text'], original['text'])
        self.assertEqual(self.messages()[0]['occurred_at'], original['occurred_at'])
        for field in ('needs_reply', 'inbound_seq', 'handled_inbound_id', 'last_inbound_at'):
            self.assertEqual(self.conversation()[field], state[field], field)

    def test_customer_point_update_keeps_missing_contacts_and_applies_explicit_clear(self):
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        self.customer_phone(7, parent='Kept parent')
        settings = {'connection_id': self.crm}
        apply_customer(self.db, self.workspace, settings, self.snapshot,
                       {'id': 7, 'name': 'Updated pupil', 'is_study': 1, '_branch': 1})
        saved = self.db.execute(text('SELECT phones,legal_name FROM alfa_customers WHERE workspace_id=:w'),
            {'w': self.workspace}).mappings().one()
        self.assertEqual((saved['phones'], saved['legal_name']), (['+79160000000'], 'Kept parent'))
        apply_customer(self.db, self.workspace, settings, self.snapshot,
                       {'id': 7, 'phone': [], 'legal_name': '', '_branch': 1})
        saved = self.db.execute(text('SELECT phones,legal_name FROM alfa_customers WHERE workspace_id=:w'),
            {'w': self.workspace}).mappings().one()
        self.assertEqual((saved['phones'], saved['legal_name']), ([], ''))

    def test_late_identity_response_cannot_overwrite_a_newer_point_observation(self):
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        settings = {'connection_id': self.crm}
        raw = {'id': 7, 'name': 'Fresh', 'legal_name': 'Fresh parent', 'is_study': 1, '_branch': 1}
        apply_customer(self.db, self.workspace, settings, self.snapshot, raw, observed_at=self.at + timedelta(minutes=2))
        old = apply_customer(self.db, self.workspace, settings, self.snapshot, dict(raw, name='Stale'),
                             observed_at=self.at + timedelta(minutes=1))
        self.assertFalse(old['applied'])
        self.assertEqual(self.db.execute(text('SELECT name FROM alfa_customers WHERE workspace_id=:w'),
            {'w': self.workspace}).scalar_one(), 'Fresh')

    def test_current_parent_conflict_is_not_cleared_by_an_ordinary_rename(self):
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        self.add_customer(self.contact, 1, 8)
        self.customer_phone(7, parent='Parent A')
        self.customer_phone(8, parent='Parent B')
        apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'name': 'Renamed pupil', 'is_study': 1, '_branch': 1})
        self.assertTrue(self.db.execute(text('SELECT needs_review FROM alfa_directory WHERE workspace_id=:w'),
            {'w': self.workspace}).scalar_one())

    def test_finance_customer_request_also_refreshes_light_identity_without_extra_crm_calls(self):
        reader, worker, _ = self.family_worker_fixture()
        self.customer_phone(7, parent='Old parent', status='lead')
        self.project(self.row())
        worker.schedule(self.workspace)
        job = worker.claim(self.workspace)
        calls = []
        def call(path, body):
            calls.append(path)
            items = [{'id': 7, 'name': 'Current pupil', 'legal_name': 'Current parent', 'is_study': 1,
                      'balance': '100', 'paid_lesson_count': 2}] if '/customer/index' in path else []
            return {'items': items, 'total': len(items), 'page': body['page']}
        worker.connections.client_factory.return_value.call.side_effect = call
        worker.process(self.workspace, job)
        self.assertEqual(sum('/customer/index' in path for path in calls), 1)
        source = self.db.execute(text('SELECT name,legal_name,status,phones FROM alfa_customers WHERE workspace_id=:w'),
            {'w': self.workspace}).mappings().one()
        self.assertEqual((source['name'], source['legal_name'], source['status']), ('Current pupil', 'Current parent', 'client'))
        self.assertEqual(source['phones'], ['+79160000000'])

    def test_full_import_carries_newer_point_facts_without_reverting_to_staged_names(self):
        from center_messenger import customer_updates
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        staged = dict(self.db.execute(text('SELECT * FROM alfa_customers WHERE workspace_id=:w'), {'w': self.workspace}).mappings().one())
        next_run = uuid4()
        self.db.execute(text("INSERT INTO alfa_sync_runs(workspace_id,id,connection_id,settings_version,requested_by,state) "
            "VALUES(:w,:r,:c,1,:u,'running')"), {'w': self.workspace, 'r': next_run, 'c': self.crm, 'u': self.user})
        customer_updates.record(self.db, self.workspace, dict(self.snapshot, run=next_run), 1, 7, self.at, source='directory')
        apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'name': 'Latest pupil', 'legal_name': 'Latest parent', 'is_study': 1, '_branch': 1},
            observed_at=self.at + timedelta(minutes=2))
        previous = list(self.db.execute(text('SELECT * FROM alfa_customers WHERE workspace_id=:w'), {'w': self.workspace}).mappings())
        updated = customer_updates.carry_forward(self.db, self.workspace, self.crm, next_run, self.run,
            [staged], previous, self.at - timedelta(minutes=1), 1)
        self.assertEqual(updated[0]['name'], 'Latest pupil')
        self.assertEqual(self.db.execute(text('SELECT name FROM alfa_customers WHERE workspace_id=:w AND run_id=:r'),
            {'w': self.workspace, 'r': next_run}).scalar_one(), 'Latest pupil')

    def test_publication_carry_does_not_promote_inherited_fields_of_a_newer_partial_response(self):
        from center_messenger import customer_updates
        from center_messenger.active_review import apply_customer
        self.snapshot['version'] = 1
        staged = dict(self.db.execute(text('SELECT * FROM alfa_customers WHERE workspace_id=:w'), {'w': self.workspace}).mappings().one())
        staged['name'] = 'New full-import name'
        next_run = uuid4()
        self.db.execute(text("INSERT INTO alfa_sync_runs(workspace_id,id,connection_id,settings_version,requested_by,state) "
            "VALUES(:w,:r,:c,1,:u,'running')"), {'w': self.workspace, 'r': next_run, 'c': self.crm, 'u': self.user})
        customer_updates.record(self.db, self.workspace, dict(self.snapshot, run=next_run), 1, 7, self.at,
                                source='directory', fields=['name', 'status'])
        apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'phone': ['+79000000002'], '_branch': 1}, observed_at=self.at + timedelta(minutes=2))
        previous = list(self.db.execute(text('SELECT * FROM alfa_customers WHERE workspace_id=:w'), {'w': self.workspace}).mappings())
        result = customer_updates.carry_forward(self.db, self.workspace, self.crm, next_run, self.run,
            [staged], previous, self.at - timedelta(minutes=1), 1)
        self.assertEqual(result[0]['name'], 'New full-import name')
        self.assertEqual(result[0]['phones'], ['+79000000002'])

    def test_resumed_legacy_finance_checkpoint_does_not_look_like_a_new_identity_observation(self):
        from center_messenger.active_review import apply_customer
        _, worker, _ = self.family_worker_fixture()
        self.project(self.row())
        worker.schedule(self.workspace)
        job = worker.claim(self.workspace)
        checkpoint = worker.checkpoint(job, {}, self.snapshot, self.at.date(), started_at=self.at)
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('checkpoint',CAST(:p AS jsonb)) "
            "WHERE workspace_id=:w AND id=:id"), {'w': self.workspace, 'id': job['id'], 'p': json.dumps(checkpoint)})
        apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'name': 'Fresh name', '_branch': 1}, observed_at=self.at + timedelta(minutes=2))
        def finance(*args, **kwargs):
            kwargs['on_customer']({'id': 7, 'name': 'Old cached name', 'is_study': 1}, False)
            return self.live_family_facts()
        with patch('center_messenger.family_cache.fetch_family_finance', side_effect=finance):
            worker.process(self.workspace, job)
        self.assertEqual(self.db.execute(text('SELECT name FROM alfa_customers WHERE workspace_id=:w'),
                         {'w': self.workspace}).scalar_one(), 'Fresh name')

    def test_full_directory_publication_preserves_id_bindings_and_newer_point_identity(self):
        from center_messenger.alfa_sync import AlfaSync
        from center_messenger.active_review import apply_customer
        self.family_worker_fixture()
        original = self.db.execute(text('SELECT * FROM alfa_customers WHERE workspace_id=:w'), {'w': self.workspace}).mappings().one()
        next_run = uuid4()
        self.db.execute(text("INSERT INTO alfa_sync_runs(workspace_id,id,connection_id,settings_version,requested_by,state,created_at) "
            "VALUES(:w,:r,:c,1,:u,'running',:at)"), {'w': self.workspace, 'r': next_run, 'c': self.crm, 'u': self.user, 'at': self.at})
        self.db.execute(text('UPDATE alfa_sync_control SET running_id=:r WHERE workspace_id=:w'), {'w': self.workspace, 'r': next_run})
        cursor = {'phase': 'customers', 'branches': [{'id': 1}], 'bi': 0, 'part': 0}
        AlfaSync._store_page(self.db, self.workspace, next_run, cursor,
            [{'id': 7, 'name': 'Staged pupil', 'is_study': 1}], observation=(self.crm, self.at, 1))
        apply_customer(self.db, self.workspace, {'connection_id': self.crm}, self.snapshot,
            {'id': 7, 'name': 'Latest pupil', 'legal_name': 'Latest parent', 'is_study': 1, '_branch': 1},
            observed_at=self.at + timedelta(minutes=2))
        control = self.db.execute(text('SELECT * FROM alfa_sync_control WHERE workspace_id=:w'), {'w': self.workspace}).mappings().one()
        job = self.db.execute(text('SELECT * FROM alfa_sync_runs WHERE workspace_id=:w AND id=:r'),
                              {'w': self.workspace, 'r': next_run}).mappings().one()
        service = object.__new__(AlfaSync)
        service._publish(self.db, control, job)
        current = self.db.execute(text('SELECT * FROM alfa_customers WHERE workspace_id=:w AND run_id=:r'),
                                  {'w': self.workspace, 'r': next_run}).mappings().one()
        self.assertEqual((current['name'], current['legal_name']), ('Latest pupil', 'Latest parent'))
        self.assertEqual((current['contact_id'], current['student_id']), (original['contact_id'], original['student_id']))
        self.assertEqual(current['phones'], original['phones'])
        self.assertEqual(self.db.execute(text('SELECT published_id FROM alfa_sync_control WHERE workspace_id=:w'),
            {'w': self.workspace}).scalar_one(), next_run)

    def test_source_reassociation_moves_only_the_exact_record_when_old_history_has_no_peer_proof(self):
        from center_messenger.history_receipts import new_records
        self.snapshot['version'] = 1
        second = uuid4()
        self.add_customer(second, 1, 8)
        self.customer_phone(7, parent='Old parent', status='lead')
        self.customer_phone(8, parent='Current parent', status='client')
        self.project(self.row('51', body='Other history'), self.row('52', body='Keep original body', at=self.at + timedelta(minutes=1)))
        original = next(m for m in self.messages() if m['payload']['crm_record_id'] == '52')
        current = self.row('52', customer=8, body='Upstream edit', at=self.at + timedelta(minutes=1))
        self.project(*new_records(self.db, self.workspace, self.channel, 1, [current]))
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        old = next(m for m in messages if m['payload']['crm_record_id'] == '51')
        moved = next(m for m in messages if m['payload']['crm_record_id'] == '52')
        self.assertEqual((moved['id'], moved['text']), (original['id'], 'Keep original body'))
        self.assertNotEqual(moved['conversation_id'], old['conversation_id'])
        self.assertEqual(moved['payload']['customer_id'], 8)
        self.assertEqual(old['payload']['customer_id'], 7)

if __name__ == "__main__":
    unittest.main()
