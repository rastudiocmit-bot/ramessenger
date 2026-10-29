import copy
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))
from center_messenger.alfa_client import AlfaClient, AlfaError, retry_seconds
from center_messenger.alfa_family import pages, fetch_family_finance
from center_messenger.card_jobs import CheckpointClient, PageYield, failure


class CardJobPolicyTests(unittest.TestCase):
    def test_retry_after_date_and_large_seconds_remain_intact(self):
        now = datetime(2026, 10, 27, tzinfo=UTC)
        for value in ("900", format_datetime(now + timedelta(seconds=900), usegmt=True)):
            result, scope = failure(AlfaError("rate_limited", 429, value), 0, now=now)
            self.assertEqual(result["http_status"], 429)
            self.assertEqual(result["error"], "rate_limited")
            self.assertEqual(datetime.fromisoformat(result["available_at"]), now+timedelta(seconds=900))
            self.assertEqual(scope, "connection")
        self.assertIsNone(retry_seconds("NaN"))
        self.assertIsNone(retry_seconds("bad header"))

    def test_long_server_pause_does_not_become_timeout_or_an_early_retry(self):
        error = AlfaError("rate_limited", 429, "900")
        poster, sleep = Mock(side_effect=error), Mock()
        client = AlfaClient("https://test.s20.online", "a@example.org", {}, Mock(), poster=poster, sleep=sleep)
        with self.assertRaises(AlfaError) as caught:
            client._post("/v2api/1/customer/index", {})
        self.assertIs(caught.exception, error)
        self.assertEqual(poster.call_count, 1)
        sleep.assert_not_called()

    def test_final_401_has_one_token_refresh_and_retains_the_http_cause(self):
        poster = Mock(side_effect=[AlfaError("unauthorized",401), {"token":"renewed"}, AlfaError("unauthorized",401)])
        client = AlfaClient("https://test.s20.online", "a@example.org",
            {"token":"old", "token_until":time.time()+300, "api_key":"synthetic"}, Mock(), poster=poster)
        with self.assertRaises(AlfaError) as caught:
            client.call("/v2api/1/customer/index", {"id":7})
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual([call.args[1] for call in poster.call_args_list],
            ["/v2api/1/customer/index", "/v2api/auth/login", "/v2api/1/customer/index"])

    def test_policy_distinguishes_permanent_scoped_and_temporary_failures(self):
        for error, code, scope in ((AlfaError("unauthorized",401),"credentials_required","connection"),
            (AlfaError("forbidden",403),"access_denied","operation"),
            (AlfaError("not_found"),"not_found",None),
            (AlfaError("invalid_response"),"invalid_response",None),
            (AlfaError("network"),"crm_unavailable",None)):
            result, actual = failure(error, 0)
            self.assertEqual((result["error"], actual), (code,scope))
            self.assertEqual(result["paused"], scope is not None)
        one, _ = failure(AlfaError("network"), 0)
        two, _ = failure(AlfaError("network"), 1, now=datetime.fromisoformat(one["failed_at"]))
        self.assertGreater(two["available_at"], one["available_at"])
        self.assertEqual(failure(None,3,busy=True)[0]["attempts"],3)

    def test_workspace_retry_policy_controls_busy_and_exponential_delays(self):
        policy = {"busy_retry_seconds": 7, "retry_initial_seconds": 20, "retry_max_seconds": 65}
        now = datetime(2026, 10, 29, tzinfo=UTC)
        busy, _ = failure(None, 4, busy=True, now=now, policy=policy)
        first, _ = failure(AlfaError("network"), 0, now=now, policy=policy)
        capped, _ = failure(AlfaError("network"), 9, now=now, policy=policy)
        invalid, _ = failure(AlfaError("invalid_response"), 0, now=now, policy=policy)
        self.assertEqual(datetime.fromisoformat(busy["available_at"]), now + timedelta(seconds=7))
        self.assertEqual(datetime.fromisoformat(first["available_at"]), now + timedelta(seconds=20))
        self.assertEqual(datetime.fromisoformat(capped["available_at"]), now + timedelta(seconds=65))
        self.assertEqual(datetime.fromisoformat(invalid["available_at"]), now + timedelta(seconds=65))

    def test_paged_sections_yield_and_resume_without_refetching_saved_pages(self):
        saved = {"responses": {}}
        wire = Mock()
        wire.call.side_effect = lambda path, body: {"items":[{"id":body["page"]+1}],"total":5,"page":body["page"]}
        durable = []
        client = CheckpointClient(wire, saved, lambda value: durable.append(copy.deepcopy(value)), limit=3)
        with self.assertRaises(PageYield):
            pages(client, "/v2api/1/lesson/index", {})
        self.assertEqual(len(durable),3)
        restored = CheckpointClient(wire, durable[-1], lambda value: None, limit=3)
        self.assertEqual(len(pages(restored, "/v2api/1/lesson/index", {})),5)
        self.assertEqual([call.args[1]["page"] for call in wire.call.call_args_list],list(range(5)))

    def test_changed_totals_or_duplicate_pages_cannot_be_published(self):
        for second in ({"items":[{"id":2}],"page":1,"total":3},
                       {"items":[{"id":1}],"page":1,"total":2}):
            client=Mock()
            client.call.side_effect=[{"items":[{"id":1}],"page":0,"total":2},second]
            with self.assertRaises(AlfaError):
                pages(client,"/v2api/1/lesson/index",{})

    def test_absent_exact_customer_is_a_not_found_state(self):
        client=Mock()
        client.call.return_value={"items":[],"page":0,"total":0}
        with self.assertRaises(AlfaError) as caught:
            fetch_family_finance(client,1,7,source_customer={"id":7,"branch_id":1})
        self.assertEqual(caught.exception.code,"not_found")
        self.assertEqual([call.args[1]["removed"] for call in client.call.call_args_list],[0,2])
