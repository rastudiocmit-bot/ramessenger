"""Offline coverage for the GET-only incident diagnostic and redacted reports."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/backend/src'))
spec = importlib.util.spec_from_file_location('recipient_diagnostic', ROOT / 'scripts/diagnose_max_recipients.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

ORIGIN = 'https://test.s20.online'
BASE = '/company/1/max/bulk?related_class=Customer&related_ids%5B%5D=42'


class RecipientDiagnosticTests(unittest.TestCase):
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
