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
        context.new_page.return_value = page
        page.goto.side_effect = navigation
        page.goto.return_value = response or SimpleNamespace(status=200)
        with patch.object(AlfaMaxBrowser, '_open', return_value=(manager, browser, context, page)), \
             patch.object(AlfaMaxBrowser, '_check_origin', side_effect=check), \
             patch('center_messenger.alfa_max_browser.time.sleep'):
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


class HistoryNavigationRecoveryTests(unittest.TestCase):
    def setup_pages(self):
        manager, browser, context, first, replacement = [Mock() for _ in range(5)]
        context.new_page.return_value = replacement
        context.storage_state.return_value = {'cookies': [{'name': 'session', 'value': 'synthetic'}]}
        response = SimpleNamespace(status=200, text=lambda: '<table class="crm-table"></table>')
        first.goto.return_value = response
        replacement.goto.return_value = response
        return manager, browser, context, first, replacement

    def test_a_transport_reset_retries_the_same_page_in_a_fresh_tab(self):
        from playwright.sync_api import Error
        manager,browser,context,first,replacement = self.setup_pages()
        first.goto.side_effect = Error('page.goto: net::ERR_CONNECTION_RESET at https://private.test/?cookie=secret')
        heartbeat = Mock()
        with patch.object(AlfaMaxBrowser, '_open', return_value=(manager,browser,context,first)), \
             patch.object(AlfaMaxBrowser, '_check_origin'), patch('center_messenger.alfa_max_browser.time.sleep') as delay:
            results,state = AlfaMaxBrowser().read_histories('https://test.s20.online',{},[(4,'sent',19)],on_progress=heartbeat)
        self.assertEqual(len(results),1)
        self.assertNotIn('error',results[0])
        self.assertEqual(first.goto.call_args.args,replacement.goto.call_args.args)
        self.assertEqual(heartbeat.call_count,1, 'Retry must not advance the worker page counter')
        first.close.assert_called_once()
        context.new_page.assert_called_once()
        delay.assert_called_once_with(0.5)
        self.assertEqual(state,context.storage_state.return_value)

    def test_exhausted_retries_are_bounded_and_only_emit_approved_network_codes(self):
        from playwright.sync_api import Error
        manager,browser,context,first,replacement = self.setup_pages()
        failure = Error('net::ERR_CONNECTION_RESET at https://private.test/?cookie=secret')
        first.goto.side_effect = replacement.goto.side_effect = failure
        with patch.object(AlfaMaxBrowser, '_open', return_value=(manager,browser,context,first)), \
             patch.object(AlfaMaxBrowser, '_check_origin'), patch('center_messenger.alfa_max_browser.time.sleep') as delay:
            results,_ = AlfaMaxBrowser().read_histories('https://test.s20.online',{},[(4,'sent',19)],continue_on_error=True)
        self.assertEqual(first.goto.call_count+replacement.goto.call_count,3)
        self.assertEqual([c.args[0] for c in delay.call_args_list],[0.5,1.0])
        self.assertEqual(results[0]['attempts'],3)
        self.assertEqual(results[0]['network_error'],'ERR_CONNECTION_RESET')
        self.assertNotIn('private',json.dumps(results))
        self.assertNotIn('secret',json.dumps(results))

    def test_security_and_resource_errors_do_not_start_more_navigation_attempts(self):
        from playwright.sync_api import Error
        for code in ('ERR_CERT_AUTHORITY_INVALID','ERR_BLOCKED_BY_CLIENT','ERR_OUT_OF_MEMORY'):
            with self.subTest(code=code):
                manager,browser,context,first,_ = self.setup_pages()
                first.goto.side_effect = Error('net::'+code+' at https://private.test')
                with patch.object(AlfaMaxBrowser, '_open', return_value=(manager,browser,context,first)), \
                     patch('center_messenger.alfa_max_browser.time.sleep') as delay:
                    results,_ = AlfaMaxBrowser().read_histories('https://test.s20.online',{},[(4,'sent',19)],continue_on_error=True)
                self.assertEqual(first.goto.call_count,1)
                self.assertEqual(results[0]['network_error'],code)
                context.new_page.assert_not_called()
                delay.assert_not_called()

    def test_http_503_is_retried_but_403_is_reported_immediately(self):
        for status in (503,403):
            with self.subTest(status=status):
                manager,browser,context,first,replacement = self.setup_pages()
                first.goto.return_value = SimpleNamespace(status=status)
                with patch.object(AlfaMaxBrowser, '_open', return_value=(manager,browser,context,first)), \
                     patch.object(AlfaMaxBrowser, '_check_origin'), patch('center_messenger.alfa_max_browser.time.sleep') as delay:
                    results,_ = AlfaMaxBrowser().read_histories('https://test.s20.online',{},[(4,'sent',19)],continue_on_error=True)
                if status==503:
                    self.assertNotIn('error',results[0])
                    self.assertEqual(replacement.goto.call_count,1)
                else:
                    self.assertEqual(results[0]['http_status'],403)
                    context.new_page.assert_not_called()
                    delay.assert_not_called()

    def test_crashed_context_is_reopened_with_the_last_known_session(self):
        from playwright._impl._errors import TargetClosedError
        manager,browser,context,first,_ = self.setup_pages()
        manager2,browser2,context2,replacement,_ = self.setup_pages()
        first.goto.side_effect = context.new_page.side_effect = context.storage_state.side_effect = TargetClosedError()
        session = {'cookies':[{'name':'session','value':'last-known'}]}
        with patch.object(AlfaMaxBrowser, '_open', side_effect=[(manager,browser,context,first),(manager2,browser2,context2,replacement)]) as opening, \
             patch.object(AlfaMaxBrowser, '_check_origin'), patch('center_messenger.alfa_max_browser.time.sleep'):
            results,_ = AlfaMaxBrowser().read_histories('https://test.s20.online',session,[(4,'sent',19)])
        self.assertNotIn('error',results[0])
        self.assertEqual(opening.call_args.args[1],session)
        context.close.assert_called_once()
        context2.close.assert_called_once()
        manager.stop.assert_called_once()
        manager2.stop.assert_called_once()

    def test_unrecognized_tokens_are_not_exposed_as_network_errors(self):
        from playwright.sync_api import Error
        from center_messenger.alfa_max_browser import _browser_network_error
        self.assertIsNone(_browser_network_error(Error('net::ERR_PRIVATE_CUSTOMER_NAME at private URL')))
        self.assertEqual(_browser_network_error(Error('net::ERR_ABORTED at private URL')),'ERR_ABORTED')

    def test_failed_page_preserves_the_verified_prefix_inside_the_same_batch(self):
        browser = Mock()
        def read(origin,state,requests,**kwargs):
            self.assertTrue(kwargs['continue_on_error'])
            results=[]
            for _,_,page in requests:
                if page==19:
                    results.append({'error':'crm_unavailable','stage':'navigation','cause':'browser_error',
                                    'network_error':'ERR_CONNECTION_RESET','attempts':3})
                else:
                    results.append({'messages':[{'record_id':str(page*20+n+1)} for n in range(20)],
                                    'row_count':20,'total':684,'has_more':True})
            return results,state
        browser.read_histories.side_effect=read
        report=compare_source_history(browser,'https://test.s20.online',{},4,'sent',set())
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['failed_page'],20)
        self.assertEqual(report['pages_checked'],19)
        self.assertEqual(report['observed_rows'],380)
        self.assertEqual(report['network_error'],'ERR_CONNECTION_RESET')
        self.assertEqual(report['attempts'],3)
        self.assertNotIn('missing_in_database',report)


if __name__ == '__main__':
    unittest.main()
