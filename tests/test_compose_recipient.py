"""Regression coverage for deterministic native MAX compose recipient selection."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from center_messenger.alfa_max_browser import (
    AlfaBrowserError,
    AlfaMaxBrowser,
    _compose_url_from_values,
    parse_compose_form,
    parse_history_html,
)


ORIGIN = 'https://test.s20.online'
BASE = '/company/1/max/bulk?related_class=Customer&related_ids%5B%5D=42'


class ComposeRecipientTests(unittest.TestCase):
    def select(self, links, **options):
        return _compose_url_from_values(links, origin=ORIGIN, branch=1, customer=42, **options)

    def history(self, cells, *, attributes=''):
        html = ('<table class="crm-table"><tbody><tr data-key="900"' + attributes + '>' +
                cells + '</tr></tbody></table>')
        return parse_history_html(html, origin=ORIGIN, branch=1, direction='receive')['messages'][0]

    def history_cells(self, extra=''):
        return ('<td></td><td><a href="/company/1/customer/view?id=42">Client</a>' + extra + '</td>'
                '<td data-user-id="999">Parent +79160000000</td><td>Hello</td>'
                '<td>24.10.2026 03:10</td>')

    def test_compose_links_dedupe_equivalent_urls(self):
        links = [
            BASE + '&maxUserId=77',
            'https://test.s20.online/company/1/max/bulk?maxUserId=77&related_ids[]=42&related_class=Customer',
        ]
        self.assertIn('maxUserId=77', self.select(links))

    def test_compose_links_use_only_unique_identified_peer(self):
        links = [
            BASE,
            BASE + '&maxUserId=77',
            '/company/1/max/bulk?maxUserId=77&related_ids[]=42&related_class=Customer',
        ]
        self.assertIn('maxUserId=77', self.select(links))

    def test_compose_links_keep_distinct_or_unresolved_recipients_blocked(self):
        for links in ([BASE + '&maxUserId=77', BASE + '&maxUserId=78'],
                      [BASE + '&view=one', BASE + '&view=two']):
            with self.subTest(links=links), self.assertRaises(AlfaBrowserError) as raised:
                self.select(links)
            self.assertEqual(raised.exception.code, 'multiple_recipients')

    def test_send_accepts_duplicate_card_links_for_same_peer(self):
        page, context = Mock(), Mock()
        page.url = 'https://test.s20.online/company/1/customer/view?id=42'
        bulk = ORIGIN + BASE + '&maxUserId=77'
        equivalent = ORIGIN + '/company/1/max/bulk?maxUserId=77&related_ids[]=42&related_class=Customer'
        form = ('<form action="' + bulk.replace('&', '&amp;') + '"><input name="_csrf" value="synthetic">'
                '<input name="MaxComposeBulkForm[related_ids][]" value="42">'
                '<textarea name="MaxComposeBulkForm[text]"></textarea></form>')
        page.goto.return_value = SimpleNamespace(status=200)
        page.content.return_value = ('<a href="' + bulk.replace('&', '&amp;') + '">MAX</a>'
                                     '<a href="' + equivalent.replace('&', '&amp;') + '">MAX</a>')
        page.evaluate.side_effect = [
            {'status': 200, 'url': bulk, 'text': form},
            {'status': 200, 'url': bulk, 'text': '{"success":true}'},
        ]
        context.storage_state.return_value = {'cookies': [], 'origins': []}
        with patch.object(AlfaMaxBrowser, '_open', return_value=(Mock(), Mock(), context, page)):
            result = AlfaMaxBrowser()._send_inline(ORIGIN, {'cookies': []}, 1, 42, 'Synthetic reply')
        self.assertTrue(result['accepted'])
        self.assertEqual(page.evaluate.call_count, 2)
        self.assertIn('maxUserId=77', page.evaluate.call_args_list[1].args[1]['url'])

    def test_send_multiple_peers_stops_before_form_or_post(self):
        page = Mock()
        page.url = 'https://test.s20.online/company/1/customer/view?id=42'
        page.goto.return_value = SimpleNamespace(status=200)
        first = BASE.replace('&', '&amp;') + '&amp;maxUserId=77'
        second = BASE.replace('&', '&amp;') + '&amp;maxUserId=78'
        page.content.return_value = '<a href="' + first + '">MAX</a><a href="' + second + '">MAX</a>'
        with patch.object(AlfaMaxBrowser, '_open', return_value=(Mock(), Mock(), Mock(), page)):
            with self.assertRaises(AlfaBrowserError) as raised:
                AlfaMaxBrowser()._send_inline(ORIGIN, {'cookies': []}, 1, 42, 'Synthetic reply')
        self.assertEqual(raised.exception.code, 'multiple_recipients')
        page.evaluate.assert_not_called()

    def test_modal_redirect_dedupes_same_peer(self):
        first = BASE + '&maxUserId=77'
        second = '/company/1/max/bulk?maxUserId=77&related_ids[]=42&related_class=Customer'
        raw = json.dumps({'modal_open': {'first': first, 'second': second}})
        parsed = parse_compose_form(raw, origin=ORIGIN, branch=1, customer=42)
        self.assertIn('maxUserId=77', parsed['compose_url'])

    def test_modal_redirect_still_rejects_multiple_peers(self):
        raw = json.dumps({'modal_open': [BASE + '&maxUserId=77', BASE + '&maxUserId=78']})
        with self.assertRaises(AlfaBrowserError) as raised:
            parse_compose_form(raw, origin=ORIGIN, branch=1, customer=42)
        self.assertEqual(raised.exception.code, 'multiple_recipients')

    def test_modal_redirect_inherits_card_peer_into_peerless_url(self):
        card = ORIGIN + BASE + '&maxUserId=77'
        raw = json.dumps({'modal_open': BASE})
        parsed = parse_compose_form(raw, origin=ORIGIN, branch=1, customer=42, compose_url=card)
        self.assertIn('maxUserId=77', parsed['compose_url'])

    def test_history_reply_link_proves_the_incoming_conversation_peer(self):
        reply = BASE.replace('&', '&amp;') + '&amp;maxUserId=77'
        message = self.history(self.history_cells('<a href="' + reply + '">Reply</a>'))
        self.assertEqual(message['max_user_id'], '77')

    def test_history_explicit_peer_attribute_is_bound_to_the_crm_row(self):
        message = self.history(self.history_cells(), attributes=' data-max-user-id="77"')
        self.assertEqual(message['max_user_id'], '77')

    def test_history_never_infers_peer_from_name_phone_or_generic_user_id(self):
        message = self.history(self.history_cells())
        self.assertIsNone(message['max_user_id'])

    def test_history_conflicting_peer_evidence_is_not_a_recipient_choice(self):
        first = BASE.replace('&', '&amp;') + '&amp;maxUserId=77'
        second = BASE.replace('&', '&amp;') + '&amp;maxUserId=78'
        cells = self.history_cells('<a href="' + first + '">One</a><a href="' + second + '">Two</a>')
        self.assertIsNone(self.history(cells)['max_user_id'])

    def test_expected_dialog_peer_selects_only_the_matching_compose_route(self):
        selected = self.select([BASE + '&maxUserId=77', BASE + '&maxUserId=78'], expected_peer='77')
        self.assertIn('maxUserId=77', selected)
        with self.assertRaises(AlfaBrowserError) as raised:
            self.select([BASE + '&maxUserId=77', BASE + '&maxUserId=78'], expected_peer='79')
        self.assertEqual(raised.exception.code, 'invalid_recipient')

    def test_modal_redirect_retains_the_expected_dialog_peer(self):
        card = ORIGIN + BASE + '&maxUserId=77'
        raw = json.dumps({'modal_open': [BASE + '&maxUserId=78', BASE + '&maxUserId=77']})
        parsed = parse_compose_form(raw, origin=ORIGIN, branch=1, customer=42, compose_url=card)
        self.assertIn('maxUserId=77', parsed['compose_url'])

    def test_send_uses_proven_dialog_peer_among_multiple_card_recipients(self):
        page, context = Mock(), Mock()
        page.url = ORIGIN + '/company/1/customer/view?id=42'
        selected = ORIGIN + BASE + '&maxUserId=77'
        other = ORIGIN + BASE + '&maxUserId=78'
        form = ('<form action="' + selected.replace('&', '&amp;') + '"><input name="_csrf" value="synthetic">'
                '<input name="MaxComposeBulkForm[related_ids][]" value="42">'
                '<textarea name="MaxComposeBulkForm[text]"></textarea></form>')
        page.goto.return_value = SimpleNamespace(status=200)
        page.content.return_value = ('<a href="' + other.replace('&', '&amp;') + '">Other</a>'
                                     '<a href="' + selected.replace('&', '&amp;') + '">Current</a>')
        page.evaluate.side_effect = [
            {'status': 200, 'url': selected, 'text': form},
            {'status': 200, 'url': selected, 'text': '{"success":true}'},
        ]
        context.storage_state.return_value = {'cookies': [], 'origins': []}
        with patch.object(AlfaMaxBrowser, '_open', return_value=(Mock(), Mock(), context, page)):
            result = AlfaMaxBrowser()._send_inline(
                ORIGIN, {'cookies': []}, 1, 42, 'Synthetic reply', expected_peer='77')
        self.assertTrue(result['accepted'])
        self.assertIn('maxUserId=77', page.evaluate.call_args_list[1].args[1]['url'])
        self.assertNotIn('maxUserId=78', page.evaluate.call_args_list[1].args[1]['url'])


if __name__ == '__main__':
    unittest.main()
