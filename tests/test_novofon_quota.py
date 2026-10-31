"""Provider quota/retry accounting across API/worker instances, offline."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.novofon_client import Client, AuditError, retry_seconds
from center_messenger.novofon_quota import Budget, MemoryStore, QuotaWait, public_limits
from test_novofon_diagnostic import FakeHTTP, TOKEN


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.
        self.store = MemoryStore()

    def budget(self, token=TOKEN):
        return Budget(token, self.store, lambda: self.now)

    def sample(self, **override):
        return {'limits': {'minute_limit': 60, 'minute_remaining': 59, 'minute_reset': 59,
                           'day_limit': 3000, 'day_remaining': 2999, 'day_reset': 3600, **override}}

    def test_minute_and_day_exhaustion_wait_for_the_reported_reset(self):
        for kind, reset in (('minute', 7), ('day', 3600)):
            with self.subTest(kind=kind):
                self.store = MemoryStore();budget = self.budget()
                budget.observe('get.account', self.sample(**{kind + '_remaining': 0, kind + '_reset': reset}))
                with self.assertRaises(QuotaWait) as wait:budget.reserve('get.calls_report')
                self.assertEqual(wait.exception.retry_after, reset + 1)
                self.now += reset + 2
                budget.reserve('get.calls_report')

    def test_new_client_instances_share_the_exhausted_budget_without_http(self):
        budget = self.budget()
        budget.observe('get.account', self.sample(day_remaining=0, day_reset=5000))
        http = FakeHTTP()
        with self.assertRaises(AuditError) as error:
            Client(TOKEN, http, budget=self.budget()).read('get.account')
        self.assertEqual(error.exception.code, 'quota_exhausted')
        self.assertEqual(error.exception.details['retry_after'], 5001)
        self.assertEqual(http.calls, [])

    def test_known_account_budget_is_shared_between_different_employee_tokens(self):
        first, second, unrelated = self.budget('first-key'), self.budget('second-key'), self.budget('other-key')
        first.bind_account('same-account');second.bind_account('same-account');unrelated.bind_account('other-account')
        first.observe('get.account', self.sample(minute_remaining=0, minute_reset=11))
        with self.assertRaises(QuotaWait):second.reserve('get.calls_report')
        unrelated.reserve('get.account')

    def test_unknown_tariff_starts_with_a_small_burst_then_paces_without_sleep(self):
        budget = self.budget()
        budget.reserve('get.account')
        with self.assertRaises(QuotaWait) as wait:budget.reserve('get.account')
        self.assertGreater(wait.exception.retry_after, 0)

    def test_provider_day_budget_controls_pacing_even_when_minute_budget_is_large(self):
        budget = self.budget()
        budget.observe('get.account', self.sample(day_remaining=30, day_reset=3600))
        for _ in range(3):budget.reserve('get.calls_report')
        with self.assertRaises(QuotaWait) as wait:budget.reserve('get.calls_report')
        self.assertGreaterEqual(wait.exception.retry_after, 120)
        self.now += wait.exception.retry_after + 1
        budget.reserve('get.calls_report')

    def test_concurrent_reservations_never_overdraw_a_shared_remaining_balance(self):
        self.budget().observe('get.account', self.sample(minute_remaining=2, minute_reset=50))
        def reserve(_):
            try:self.budget().reserve('get.calls_report');return True
            except QuotaWait:return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(reserve, range(24))), 2)

    def test_out_of_order_or_concurrent_metadata_does_not_refund_reserved_quota(self):
        budget = self.budget()
        budget.observe('get.account', self.sample(minute_remaining=3, minute_reset=50))
        budget.reserve('get.calls_report');budget.reserve('get.calls_report')
        budget.observe('get.calls_report', self.sample(minute_remaining=3, minute_reset=49))
        budget.reserve('get.calls_report')
        with self.assertRaises(QuotaWait):budget.reserve('get.calls_report')

    def test_observed_method_cost_is_used_before_sending_a_larger_request(self):
        budget = self.budget()
        budget.observe('get.account', self.sample(day_remaining=10))
        budget.observe('get.calls_report', self.sample(day_remaining=6))
        budget.observe('get.sip_lines', self.sample(day_remaining=3))
        with self.assertRaises(QuotaWait):budget.reserve('get.calls_report')

    def test_http_429_retry_after_persists_and_prevents_the_next_http_request(self):
        http = FakeHTTP({'get.account': lambda _: (429, {'Retry-After': '89'}, b'')})
        with self.assertRaises(AuditError) as error:Client(TOKEN, http, budget=self.budget()).read('get.account')
        self.assertEqual(error.exception.details['retry_after'], 89)
        self.now += 10
        next_http = FakeHTTP()
        with self.assertRaises(AuditError) as wait:Client(TOKEN, next_http, budget=self.budget()).read('get.account')
        self.assertGreaterEqual(wait.exception.details['retry_after'], 79)
        self.assertEqual(next_http.calls, [])

    def test_rpc_limit_error_with_day_metadata_pauses_until_day_reset(self):
        def error(payload):
            return 200, {}, json.dumps({'jsonrpc': '2.0', 'id': payload['id'], 'error': {
                'code': -32029, 'message': TOKEN, 'data': {'mnemonic': 'limit_exceeded',
                'params': {'limit_type': 'day', 'extra': TOKEN},
                'metadata': self.sample(day_remaining=0, day_reset=1234)}}}).encode()
        with self.assertRaises(AuditError) as caught:
            Client(TOKEN, FakeHTTP({'get.account': error}), budget=self.budget()).read('get.account')
        self.assertEqual(caught.exception.details['retry_after'], 1235)
        self.assertNotIn(TOKEN, json.dumps(caught.exception.public()))
        with self.assertRaises(QuotaWait):self.budget().reserve('get.calls_report')

    def test_retry_after_supports_http_dates_and_rejects_invalid_values(self):
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_seconds({'Retry-After': 'Sat, 31 Oct 2026 12:01:00 GMT'}, now), 61)
        for value in ('broken', '-4', 'Wed', ''):
            self.assertEqual(retry_seconds({'Retry-After': value}, now), 0)

    def test_only_sanitized_integer_quota_fields_are_public(self):
        self.assertEqual(public_limits({'limits': {'day_limit': 50, 'minute_remaining': True,
                                                   'secret': TOKEN, 'day_remaining': -1}}), {'day_limit': 50})
        self.budget().reserve('get.account')
        self.assertNotIn(TOKEN, repr(self.store.rows))


if __name__ == '__main__':unittest.main()
