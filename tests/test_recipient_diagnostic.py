"""Offline coverage for the GET-only incident diagnostic and redacted reports."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/backend/src'))
spec = importlib.util.spec_from_file_location('recipient_diagnostic', ROOT / 'scripts/diagnose_max_recipients.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

ORIGIN = 'https://test.s20.online'
BASE = '/company/1/max/bulk?related_class=Customer&related_ids%5B%5D=42'


class RecipientDiagnosticTests(unittest.TestCase):
    def wait_scope(self):
        clock = [0.0]
        self.enterContext(patch.object(audit.time, 'monotonic', side_effect=lambda: clock[0]))
        self.enterContext(patch.object(audit.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0] + n)))
        status = {'state': 'active', 'needs_check': False, 'queued_sends': 0}
        self.enterContext(patch.object(audit, 'browser_status', return_value=status))
        return clock, status

    def test_busy_browser_is_waited_for_without_restarting_the_probe(self):
        clock, _ = self.wait_scope()
        service, lease, report = Mock(), {'token': 'private-token'}, {}
        service.sessions.claim.side_effect = [audit.AlfaBrowserError('browser_busy'),
                                             audit.AlfaBrowserError('browser_busy'), lease]
        with audit.wait_for_browser(service, 'w', 'c', report) as acquired:
            self.assertIs(acquired, lease)
        self.assertEqual(clock[0], 1)
        service.sessions.release.assert_called_once_with(lease)
        self.assertTrue(report['browser_wait']['acquired'])
        self.assertNotIn('private-token', json.dumps(report))

    def test_wait_timeout_does_not_release_or_replace_another_lease(self):
        self.wait_scope()
        service, report = Mock(), {}
        service.sessions.claim.side_effect = audit.AlfaBrowserError('browser_busy')
        with self.assertRaises(audit.AlfaBrowserError) as caught:
            with audit.wait_for_browser(service, 'w', 'c', report, seconds=2):
                self.fail('busy browser must never be entered')
        self.assertEqual(caught.exception.code, 'browser_wait_timeout')
        service.sessions.release.assert_not_called()
        self.assertEqual(report['browser_wait']['waited_seconds'], 2)

    def test_busy_status_reads_a_snapshot_and_does_not_expose_session_tokens(self):
        service, db = Mock(), Mock()
        @contextmanager
        def transaction(**scope):
            self.assertEqual(scope, {'workspace_id': 'w', 'snapshot': True})
            yield db
        service.accounts.transaction = transaction
        service._row.return_value = {'connection_id': 'c', 'state': 'active', 'last_sync_at': None,
            'session_ciphertext': 'encrypted-secret', 'history_cursor': {'session_control': {
                'lease': 'lease-secret', 'until': '2999-01-01T00:00:00+00:00',
                'recovery_password': 'password-secret'}}}
        db.execute.return_value.scalar_one.side_effect = [True, 2]
        report = audit.browser_status(service, 'w', 'c')
        self.assertTrue(report['lease_active'])
        self.assertTrue(report['sync_running'])
        self.assertEqual(report['queued_sends'], 2)
        self.assertNotIn('secret', json.dumps(report))

    def test_auth_check_is_distinguished_from_occupied_browser(self):
        _, status = self.wait_scope()
        status['needs_check'] = True
        service = Mock()
        with self.assertRaises(audit.AlfaBrowserError) as caught:
            with audit.wait_for_browser(service, 'w', 'c', {}):
                self.fail('pending session recovery must not be entered')
        self.assertEqual(caught.exception.code, 'session_check_pending')
        service.sessions.claim.assert_not_called()

    def test_actual_sends_are_allowed_to_finish_before_optional_audit(self):
        clock, status = self.wait_scope()
        status['queued_sends'] = 1
        def finish_send(seconds):
            clock[0] += seconds
            status['queued_sends'] = 0
        with patch.object(audit.time, 'sleep', side_effect=finish_send):
            with audit.wait_for_browser(Mock(), 'w', 'c', {}):
                self.assertGreater(clock[0], 0)

    def test_error_inside_probe_is_not_retried_and_lease_is_released(self):
        self.wait_scope()
        service = Mock()
        with self.assertRaises(audit.AlfaBrowserError):
            with audit.wait_for_browser(service, 'w', 'c', {}):
                raise audit.AlfaBrowserError('browser_busy')
        service.sessions.claim.assert_called_once()
        service.sessions.release.assert_called_once()

    def test_only_exact_customer_routes_are_reported_and_equivalent_links_collapse(self):
        html = (f'<a href="{BASE}&maxUserId=77">One</a>'
                f'<a data-url="{ORIGIN}{BASE}&maxUserId=77">Two</a>'
                '<a href="/company/1/max/bulk?related_ids[]=99&maxUserId=78">Other</a>'
                f'<a href="https://other.example{BASE}&maxUserId=79">Foreign</a>')
        self.assertEqual(len(audit.routes(html, ORIGIN, 1, 42)), 1)

    def test_schema_reports_input_names_without_tokens_ids_or_text(self):
        raw = ('<form action="' + BASE + '&maxUserId=77123456789">'
               '<input name="_csrf" value="secret-csrf"><input name="MaxComposeBulkForm[max_user_id]" value="77123456789">'
               '<textarea name="MaxComposeBulkForm[text]">private message</textarea></form>')
        report = audit.form_report(raw)
        serialized = json.dumps(report)
        self.assertNotIn('secret-csrf', serialized)
        self.assertNotIn('77123456789', serialized)
        self.assertNotIn('private message', serialized)
        self.assertEqual(report['forms'][0]['recipient_fields'][0]['name'], 'MaxComposeBulkForm[max_user_id]')
        self.assertTrue(report['forms'][0]['action_peer_tag'])

    def test_each_ambiguous_native_recipient_is_inspected_without_a_send(self):
        probe = Mock(origin=ORIGIN)
        probe.navigate.return_value = ''.join(f'<a href="{BASE}&maxUserId={n}">MAX</a>' for n in [77, 78])
        form = '<form action="' + BASE + '"><input name="_csrf" value="secret"><input name="MaxComposeBulkForm[related_ids][]" value="42"><textarea name="MaxComposeBulkForm[text]"></textarea></form>'
        probe.fetch.return_value = {'status': 200, 'text': form}
        result = audit.inspect_card(probe, 1, 42, None, Mock())
        self.assertEqual(result['compose_routes'], 2)
        self.assertTrue(all(item['status'] == 'recipient_verified_no_send' for item in result['forms']))
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(probe.method_calls[0][0], 'navigate')
        self.assertEqual([call[0] for call in probe.method_calls], ['navigate', 'fetch', 'fetch'])

    def test_dry_run_uses_production_contact_row_selection_without_sending(self):
        probe = Mock(origin=ORIGIN)
        probe.navigate.return_value = (
            '<div class="col-xs-7 m-b-xs text-right word-break">+7(916)123-45-67'
            f'<a href="{BASE}&maxUserId=77">MAX</a></div>'
            '<div class="col-xs-7 m-b-xs text-right word-break">+7(985)123-45-67'
            f'<a href="{BASE}&maxUserId=78">MAX</a></div>')
        probe.fetch.return_value = {'status': 200, 'text': '<form action="' + BASE + '">'
            '<input name="_csrf" value="synthetic"><input name="MaxComposeBulkForm[related_ids][]" value="42">'
            '<textarea name="MaxComposeBulkForm[text]"></textarea></form>'}
        result = audit.inspect_card(probe, 1, 42, None, Mock(), '+79161234567')
        self.assertEqual(result['sender_resolution']['selected_peer_tag'], audit.peer_tag('77'))
        self.assertNotIn('123-45-67', json.dumps(result))

    def test_peerless_form_reports_real_failure_and_schema(self):
        probe = Mock(origin=ORIGIN)
        probe.navigate.return_value = '<html>No native links</html>'
        probe.fetch.return_value = {'status': 200, 'text': '<form action="' + BASE + '"><input name="_csrf" value="secret"><input name="MaxComposeBulkForm[related_ids][]" value="42"><textarea name="MaxComposeBulkForm[text]"></textarea></form>'}
        result = audit.inspect_card(probe, 1, 42, None, Mock())
        self.assertEqual(result['forms'][0]['error_code'], 'recipient_missing')
        self.assertEqual(result['forms'][0]['schema']['forms'][0]['recipient_fields'], [])

    def test_probe_transport_rejects_every_post_even_on_an_allowed_url(self):
        probe = audit.RecipientProbe(ORIGIN, {})
        address = ORIGIN + BASE
        probe.allowed = {address}
        route = Mock()
        route.request = Mock(method='POST', url=address, resource_type='fetch')
        probe.route(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()


if __name__ == '__main__':
    unittest.main()
