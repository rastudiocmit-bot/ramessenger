"""Read-only native attachment sampling, truthful reports and 72-hour cutoff."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.attachment_probe import extract_attachment_candidates, recent_message, storage_url
from center_messenger.attachment_audit import (
    BrowserProbe, PROBE_FILE_BYTES, audit_browser, compose_report, file_report, safe_error,
)

ORIGIN = 'https://test.s20.online'


def journal(when, href='/uploads/storage/a.jpg', *, more=False):
    return ('<table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td>Sender</td>'
            f'<td><a href="{href}"><i class="ion-android-attach">Private filename</i></a></td>'
            f'<td><time datetime="{when.isoformat()}"></time></td></tr></tbody></table>'
            + ('<ul class="pagination"><li class="next"><a href="?page=2">Next</a></li></ul>' if more else ''))


def binary(prefix=b'\xff\xd8\xff', mime='image/jpeg'):
    return {'status': 200, 'bytes': 100, 'prefix': list(prefix), 'mime': mime, 'complete': True}


class AttachmentAuditTests(unittest.TestCase):
    def test_exact_cutoff_future_and_missing_times(self):
        now = datetime(2026, 10, 26, 12, tzinfo=UTC)
        self.assertTrue(recent_message(now - timedelta(hours=72), now=now))
        self.assertFalse(recent_message(now - timedelta(hours=72, microseconds=1), now=now))
        self.assertFalse(recent_message(now + timedelta(seconds=1), now=now))
        self.assertFalse(recent_message(None, now=now))
        self.assertFalse(recent_message(now.replace(tzinfo=None), now=now))

    def test_feed_timestamp_is_source_moscow_time_and_avatar_is_not_a_file(self):
        html = ('<div class="feed-element"><img class="img-circle" src="/uploads/storage/avatar.jpg">'
                '<small class="text-muted">24.10.2026 <i></i>14:54</small><div class="m-t-sm">'
                '<a href="/uploads/storage/a.jpg"><i class="ion-android-attach">Private name</i></a>'
                '<a href="https://rastudio.org/parenttesting">Site</a></div></div>')
        result = extract_attachment_candidates(html, origin=ORIGIN)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['occurred_at'], datetime(2026, 10, 24, 11, 54, tzinfo=UTC))

    def test_encoded_traversal_and_fragments_rejected(self):
        for path in ['/uploads/storage/%2e%2e/x', '/uploads/storage/%252e%252e/x',
                     '/uploads/storage/a#secret', '/uploads/storage/a\\b',
                     'https://test.s20.online:8443/uploads/storage/a']:
            with self.subTest(path=path):
                self.assertIsNone(storage_url(path, origin=ORIGIN))

    def test_old_candidate_never_causes_network_download(self):
        probe = Mock(origin=ORIGIN)
        probe.navigate.return_value = journal(datetime.now(UTC) - timedelta(hours=73))
        result = audit_browser(probe, [1], direction='receive')
        probe.fetch.assert_not_called()
        self.assertEqual(result['skipped_not_recent'], 1)
        self.assertFalse(result['image_download_verified'])
        self.assertEqual(result['downloads'], [])

    def test_recent_sample_consumes_bytes_reports_no_private_fields(self):
        probe = Mock(origin=ORIGIN)
        probe.navigate.return_value = journal(datetime.now(UTC) - timedelta(minutes=1))
        probe.fetch.return_value = binary()
        result = audit_browser(probe, [1, 2], pages=2)
        probe.fetch.assert_called_once()  # same file mirrored across journals
        self.assertTrue(result['image_download_verified'])
        self.assertFalse(result['document_download_verified'])
        self.assertFalse(result['delivery_verified'])
        self.assertNotIn('Private', str(result))
        self.assertNotIn('/uploads/', str(result))

    def test_candidate_expiring_between_scan_and_fetch_is_not_fetched(self):
        now = datetime.now(UTC)
        candidate = {'url': ORIGIN + '/uploads/storage/a.jpg', 'occurred_at': now - timedelta(hours=72), 'source_layout': 'journal'}
        probe = Mock(origin=ORIGIN)
        probe.navigate.return_value = journal(now)
        with patch('center_messenger.attachment_audit.extract_attachment_candidates', return_value=[candidate]):
            result = audit_browser(probe, [1], direction='receive', now=now)
        probe.fetch.assert_not_called()
        self.assertEqual(result['skipped_not_recent'], 1)

    def test_html_even_with_image_mime_never_counts_as_a_file(self):
        for mime in ['text/html', 'image/jpeg']:
            result = file_report(binary(b'<!DOCTYPE html><html>Sign in</html>', mime))
            self.assertFalse(result['download_verified'])
            self.assertEqual(result['error_code'], 'html_instead_of_file')

    def test_incomplete_oversized_or_unknown_content_is_inconclusive(self):
        for value in [dict(binary(), complete=False), dict(binary(), bytes=PROBE_FILE_BYTES + 1),
                      binary(b'unknown', 'application/octet-stream'),
                      {'status': 302, 'error': 'http_error'}]:
            self.assertFalse(file_report(value)['download_verified'])

    def test_pdf_signature_and_zip_do_not_claim_office_validation(self):
        pdf = file_report(binary(b'%PDF-1.7', 'application/pdf'))
        archive = file_report(binary(b'PK\x03\x04', 'application/octet-stream'))
        self.assertTrue(pdf['download_verified'])
        self.assertTrue(archive['download_verified'])
        self.assertEqual(archive['signature_type'], 'application/zip')
        self.assertFalse(archive['declared_type_matches_signature'])

    def test_live_route_blocks_mutations_background_images_and_unapproved_urls(self):
        probe = BrowserProbe(ORIGIN, {})
        probe.guard = Mock()
        probe.allowed = {ORIGIN + '/uploads/storage/a.jpg'}
        for method, kind, url in [('POST', 'fetch', next(iter(probe.allowed))),
                                  ('GET', 'image', next(iter(probe.allowed))),
                                  ('GET', 'script', next(iter(probe.allowed))),
                                  ('GET', 'fetch', 'https://elsewhere.test/file')]:
            route = Mock(request=SimpleNamespace(method=method, resource_type=kind, url=url))
            probe.route(route)
            route.abort.assert_called_once()
        probe.guard.assert_not_called()
        route = Mock(request=SimpleNamespace(method='GET', resource_type='fetch', url=next(iter(probe.allowed))))
        probe.route(route)
        probe.guard.assert_called_once_with(route)

    def test_actual_compose_controls_and_limits_are_reported_without_csrf(self):
        markup = ('<form enctype="multipart/form-data"><input type="hidden" name="_csrf" value="secret">'
                  '<input type="file" name="MaxComposeBulkForm[attachment][]" multiple></form>'
                  '<script>let maxFileSize = 0; fileCountError = value.files.length > 10;'
                  'if (totalAttachmentSize > 8388608) {}</script>')
        result = compose_report(markup)
        self.assertTrue(result['file_input'])
        self.assertTrue(result['multiple'])
        self.assertTrue(result['multipart'])
        self.assertEqual(result['max_files_in_script'], 10)
        self.assertEqual(result['max_total_bytes_in_script'], 8_388_608)
        self.assertEqual(result['max_file_bytes_variable'], 0)
        self.assertNotIn('secret', str(result))

    def test_error_text_never_reaches_report(self):
        self.assertEqual(safe_error(RuntimeError('cookie=secret')), {'error_code': 'probe_failed'})


if __name__ == '__main__':
    unittest.main()
