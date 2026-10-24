"""Safe, actionable failures from the read-only source verification command."""
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser
from center_messenger.history_audit import audit_exit_code, compare_source_history, main, source_failure


class SourceAuditTests(unittest.TestCase):
    def verify(self, browser, **kwargs):
        return compare_source_history(browser, 'https://test.s20.online', {'cookies': []}, 4, 'sent', {'1'}, **kwargs)

    def test_browser_start_failure_retains_actionable_code_without_false_comparison(self):
        browser = Mock()
        browser.read_histories.side_effect = AlfaBrowserError('browser_unavailable', stage='launch_browser', cause='browser_error')
        result = self.verify(browser)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'browser_unavailable')
        self.assertEqual(result['stage'], 'launch_browser')
        self.assertEqual(result['failed_page'], 1)
        self.assertEqual(result['pages_checked'], 0)
        self.assertNotIn('missing_in_database', result)
        self.assertNotIn('saved_not_observed', result)

    def test_timeout_after_a_successful_batch_reports_partial_progress_and_actual_page(self):
        browser, progress = Mock(), []
        browser.read_histories.side_effect = [
            ([{'messages': [{'record_id': '1'}], 'total': 4, 'row_count': 1, 'has_more': True}], {'cookies': []}),
            AlfaBrowserError('crm_unavailable', stage='navigation', cause='timeout', request=(4, 'sent', 2)),
        ]
        result = self.verify(browser, on_progress=progress.append)
        self.assertEqual(result['cause'], 'timeout')
        self.assertEqual(result['failed_page'], 3)
        self.assertEqual(result['pages_checked'], 1)
        self.assertEqual(result['observed_rows'], 1)
        self.assertEqual(progress, [{'page': 0, 'pages_checked': 0}, {'page': 1, 'pages_checked': 1}])
        self.assertNotIn('saved_not_observed_ids', result)

    def test_unknown_codes_or_browser_metadata_never_echo_private_details(self):
        secret = 'https://secret.s20.online/?token=private-cookie-and-client'
        error = AlfaBrowserError(secret, stage=secret, cause=secret, http_status=999)
        result = source_failure(error, 4, 'sent', 0, 0, 0)
        self.assertEqual(result['error_code'], 'internal')
        self.assertIsNone(result['stage'])
        self.assertIsNone(result['cause'])
        self.assertIsNone(result['http_status'])
        self.assertNotIn(secret, json.dumps(result))

    def test_a_page_error_is_an_explicit_failed_report(self):
        browser = Mock()
        browser.read_histories.return_value = ([{'error': 'invalid_response'}], {})
        result = self.verify(browser)
        self.assertEqual(result['error_code'], 'invalid_response')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stage'], 'read_page')

    def test_successful_source_comparison_reports_actual_missing_ids_and_duplicates(self):
        browser = Mock()
        browser.read_histories.side_effect = [
            ([{'messages': [{'record_id': '1'}], 'total': 3, 'row_count': 1, 'has_more': True}], {}),
            ([{'messages': [{'record_id': '1'}], 'total': 3, 'has_more': True},
              {'messages': [{'record_id': '2'}], 'total': 3, 'has_more': False}], {}),
        ]
        result = self.verify(browser)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['missing_ids'], ['2'])
        self.assertEqual(result['duplicate_source_ids'], ['1'])
        self.assertEqual(result['distinct_source_rows'], 2)

    def test_exit_codes_distinguish_unavailable_incomplete_and_complete_reports(self):
        ready = {'state': 'active', 'history_complete': True, 'totals_verified': True, 'error_code': None}
        self.assertEqual(audit_exit_code([ready]), 0)
        self.assertEqual(audit_exit_code([{**ready, 'history_complete': False}]), 2)
        self.assertEqual(audit_exit_code([{**ready, 'source_verification': {'status': 'failed'}}]), 1)
        self.assertEqual(audit_exit_code([{**ready, 'source_verification': {'status': 'complete', 'duplicate_source_count': 1}}]), 2)

    def test_configuration_failure_returns_a_safe_error_instead_of_a_traceback(self):
        stderr = io.StringIO()
        with patch('center_messenger.history_audit.Settings', side_effect=ValueError('private credentials')), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([]), 1)
        self.assertIn('ValueError', stderr.getvalue())
        self.assertNotIn('private', stderr.getvalue())


class BrowserAuditContextTests(unittest.TestCase):
    def read_failure(self, *, navigation=None, response=None, check=None):
        manager, browser, context, page = [Mock() for _ in range(4)]
        page.goto.side_effect = navigation
        page.goto.return_value = response or SimpleNamespace(status=200)
        with patch.object(AlfaMaxBrowser, '_open', return_value=(manager, browser, context, page)), \
             patch.object(AlfaMaxBrowser, '_check_origin', side_effect=check):
            with self.assertRaises(AlfaBrowserError) as caught:
                AlfaMaxBrowser().read_histories('https://test.s20.online', {}, [(4, 'sent', 2)])
        context.close.assert_called_once()
        browser.close.assert_called_once()
        manager.stop.assert_called_once()
        return caught.exception

    def test_navigation_timeout_is_identifiable_without_changing_worker_error_codes(self):
        from playwright.sync_api import TimeoutError
        error = self.read_failure(navigation=TimeoutError('private URL and response'))
        self.assertEqual(error.code, 'crm_unavailable')
        self.assertEqual(error.cause, 'timeout')
        self.assertEqual(error.stage, 'navigation')
        self.assertEqual(error.request, (4, 'sent', 2))
        self.assertNotIn('private', json.dumps(error.__dict__))

    def test_browser_closure_is_distinguished_from_a_crm_timeout(self):
        from playwright._impl._errors import TargetClosedError
        error = self.read_failure(navigation=TargetClosedError('private browser state'))
        self.assertEqual(error.cause, 'browser_closed')
        self.assertEqual(error.code, 'crm_unavailable')

    def test_http_failure_preserves_status_and_page(self):
        error = self.read_failure(response=SimpleNamespace(status=503))
        self.assertEqual(error.http_status, 503)
        self.assertEqual(error.stage, 'check_session')
        self.assertEqual(error.request, (4, 'sent', 2))

    def test_expired_session_remains_a_specific_reauthentication_failure(self):
        error = self.read_failure(check=AlfaBrowserError('reauth_required'))
        self.assertEqual(error.code, 'reauth_required')
        self.assertEqual(error.stage, 'check_session')


if __name__ == '__main__':
    unittest.main()
