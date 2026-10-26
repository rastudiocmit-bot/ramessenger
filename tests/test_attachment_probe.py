"""Native attachment references are distinct from avatars and ordinary links."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.attachment_probe import extract_attachment_candidates, inspect_markup

ORIGIN = 'https://example.s20.online'


def feed(body):
    return '<div class="feed-element">' + body + '</div>'


def attachment(href):
    return '<a href="' + href + '"><i class="ion-android-attach">Private filename</i></a>'


class AttachmentProbeTests(unittest.TestCase):
    def test_native_feed_ignores_avatar_and_ordinary_links(self):
        html = feed('<img class="img-circle" src="/uploads/storage/avatar.jpg">'
                    '<a href="https://rastudio.org">Site</a>'
                    + attachment('/uploads/storage/photo.jpg'))
        rows = extract_attachment_candidates(html, origin=ORIGIN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], ORIGIN + '/uploads/storage/photo.jpg')
        self.assertEqual(rows[0]['source_layout'], 'feed')
        self.assertFalse(rows[0]['verified'])

    def test_journal_and_protocol_relative_document(self):
        html = '<table><tbody><tr data-key="1"><td>' + attachment('//example.s20.online/uploads/storage/a.pdf') + '</td></tr></tbody></table>'
        rows = extract_attachment_candidates(html, origin=ORIGIN)
        self.assertEqual(rows[0]['source_layout'], 'journal')
        self.assertTrue(rows[0]['url'].endswith('a.pdf'))

    def test_unsafe_or_unrecognized_locations_not_candidates(self):
        for url in ['javascript:alert(1)', 'http://example.s20.online/uploads/storage/a',
                    'https://evil.test/uploads/storage/a', '/customer/view?id=1',
                    'https://user:pass@example.s20.online/uploads/storage/a']:
            with self.subTest(url=url):
                self.assertEqual(extract_attachment_candidates(feed(attachment(url)), origin=ORIGIN), [])

    def test_deduplicate_within_message_but_preserve_separate_messages(self):
        item = attachment('/uploads/storage/a.jpg')
        rows = extract_attachment_candidates(feed(item + item) + feed(item), origin=ORIGIN)
        self.assertEqual(len(rows), 2)

    def test_report_never_leaks_names_urls_or_tokens(self):
        result = inspect_markup(feed(attachment('/uploads/storage/private.jpg?token=secret')), origin=ORIGIN)
        self.assertEqual(result, {'status': 'markup_only', 'attachment_candidates': 1,
                                 'layouts': ['feed'], 'download_verified': False,
                                 'delivery_verified': False})

    def test_oversized_input_rejected(self):
        with self.assertRaises(ValueError):
            inspect_markup('x' * 2_000_001, origin=ORIGIN)
