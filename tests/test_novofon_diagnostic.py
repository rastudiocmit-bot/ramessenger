import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend" / "src"))
from center_messenger import novofon_client as n


TOKEN = "synthetic-private-token-123456"
PHONE = "+79990001122"
URL = "https://media.novofon.ru/123/record?secret=synthetic-secret"


class FakeHTTP:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def request(self, url, *, data=None, media=False):
        if media:
            self.calls.append(("media", url))
            value = self.overrides.get("media", (206, {"Content-Type": "audio/wav"}, b"RIFF0000WAVEdata"))
            if isinstance(value, Exception):
                raise value
            return value
        payload = json.loads(data)
        method = payload["method"]
        self.calls.append((method, payload))
        value = self.overrides.get(method)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(payload)
        fixtures = {
            "get.account": [{"app_id": 123, "name": "private account name", "timezone": "Europe/Moscow"}],
            "get.calls_report": [{"id": 11, "communication_id": 22, "direction": "out",
                                   "contact_phone_number": PHONE, "full_record_file_link": URL}],
            "get.sip_lines": [{"id": 5, "type": "in_out", "password": TOKEN, "phone_number": PHONE}],
        }
        doc = {"jsonrpc": "2.0", "id": payload["id"], "result": {
            "data": fixtures[method] if value is None else value,
            "metadata": {"total_items": 1, "limits": {"minute_remaining": 50}}}}
        return 200, {}, json.dumps(doc).encode()


class DiagnosticTests(unittest.TestCase):
    def test_read_only_and_all_checks_stay_separate(self):
        http = FakeHTTP()
        result = n.audit(n.Client(TOKEN, http), probe_recording=True)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["stage1_complete"])
        self.assertEqual(result["outbound_call_attempts"], 0)
        self.assertEqual(result["checks"]["audio"]["status"], "prefix_detected")
        self.assertFalse(result["checks"]["audio"]["full_audio_validated"])
        self.assertEqual(result["browser_call"]["status"], "not_tested")
        self.assertEqual([x[0] for x in http.calls], ["get.account", "get.calls_report", "media", "get.sip_lines"])

    def test_report_never_contains_raw_phone_token_name_or_url(self):
        report = json.dumps(n.audit(n.Client(TOKEN, FakeHTTP()), probe_recording=True))
        for secret in (TOKEN, PHONE, URL, "synthetic-secret", "private account name", '"password":'):
            self.assertNotIn(secret, report)
        self.assertIn('"call_id": "11"', report)

    def test_report_uses_account_timezone_not_host_timezone(self):
        http = FakeHTTP()
        n.audit(n.Client(TOKEN, http), now=datetime(2026, 10, 30, 22, 30, tzinfo=timezone.utc))
        params = http.calls[1][1]["params"]
        self.assertEqual(params["date_till"], "2026-10-31 01:30:00")
        self.assertEqual(params["date_from"], "2026-10-30 01:30:00")

    def test_empty_page_does_not_claim_recordings_are_unavailable(self):
        result = n.audit(n.Client(TOKEN, FakeHTTP({"get.calls_report": []})), probe_recording=True)
        self.assertEqual(result["checks"]["journal"]["sampled"], 0)
        self.assertEqual(result["checks"]["audio"]["status"], "no_recording_in_sample")

    def test_unknown_timezone_stops_before_journal(self):
        http = FakeHTTP({"get.account": [{"app_id": 123, "timezone": "invalid/zone"}]})
        result = n.audit(n.Client(TOKEN, http))
        self.assertEqual(result["checks"]["account"]["code"], "account_timezone_invalid")
        self.assertEqual(len(http.calls), 1)

    def test_missing_call_identity_fails_whole_sample(self):
        http = FakeHTTP({"get.calls_report": [{"direction": "in", "contact_phone_number": PHONE}]})
        result = n.audit(n.Client(TOKEN, http))
        self.assertEqual(result["checks"]["journal"]["code"], "calls_schema")

    def test_jsonrpc_error_in_http200_is_not_success(self):
        def rejected(payload):
            doc = {"jsonrpc": "2.0", "id": payload["id"], "error": {
                "code": -32602, "message": TOKEN + URL, "data": {"mnemonic": "unexpected_parameters"}}}
            return 200, {}, json.dumps(doc).encode()
        result = n.audit(n.Client(TOKEN, FakeHTTP({"get.calls_report": rejected})))
        self.assertEqual(result["checks"]["journal"]["code"], "api_rejected")
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(URL, json.dumps(result))

    def test_mismatched_rpc_id_is_rejected(self):
        def wrong_id(payload):
            return 200, {}, b'{"jsonrpc":"2.0","id":"wrong","result":{"data":[]}}'
        with self.assertRaisesRegex(n.AuditError, "api_response_mismatch"):
            n.Client(TOKEN, FakeHTTP({"get.account": wrong_id})).read("get.account")

    def test_mutating_methods_are_impossible(self):
        http = FakeHTTP()
        with self.assertRaisesRegex(n.AuditError, "method_rejected"):
            n.Client(TOKEN, http).read("start.employee_call")
        self.assertEqual(http.calls, [])

    def test_exhausted_quota_prevents_further_api_requests(self):
        def quota(payload):
            doc = {"jsonrpc": "2.0", "id": payload["id"], "result": {
                "data": [{"app_id": 123, "timezone": "UTC"}],
                "metadata": {"limits": {"minute_remaining": 0}}}}
            return 200, {}, json.dumps(doc).encode()
        http = FakeHTTP({"get.account": quota})
        result = n.audit(n.Client(TOKEN, http))
        self.assertEqual(result["checks"]["journal"]["code"], "quota_exhausted")
        self.assertEqual(len(http.calls), 1)

    def test_login_html_is_never_reported_as_audio(self):
        http = FakeHTTP({"media": (200, {"Content-Type": "text/html"}, b"<html>login</html>")})
        result = n.audit(n.Client(TOKEN, http), probe_recording=True)
        self.assertEqual(result["checks"]["audio"]["code"], "media_is_not_audio")

    def test_disabled_probe_does_not_request_audio(self):
        http = FakeHTTP()
        result = n.audit(n.Client(TOKEN, http))
        self.assertEqual(result["checks"]["audio"]["status"], "not_requested")
        self.assertNotIn("media", [x[0] for x in http.calls])

    def test_recording_url_uses_communication_id_and_record_id(self):
        url = n.recording_url({"id": 11, "communication_id": 22, "wav_call_records": ["record_9"]})
        self.assertEqual(url, "https://app.novofon.ru/system/media/wav/22/record_9/")
        self.assertIsNone(n.recording_url({"id": 11, "call_records": ["record"]}))

    def test_media_url_has_exact_allowlist_and_public_addresses(self):
        public = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        n.validate_media_url(URL, public)
        for url in ("http://media.novofon.ru/a", "https://media.novofon.ru.evil/a", "https://127.0.0.1/a",
                    "https://token@media.novofon.ru/a", "https://media.novofon.ru:8443/a"):
            with self.subTest(url=url), self.assertRaises(n.AuditError):
                n.validate_media_url(url, public)
        private = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(n.AuditError, "media_address_rejected"):
            n.validate_media_url(URL, private)

    def test_api_cannot_redirect_secret_to_another_host(self):
        self.assertIsNone(n.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))
        with self.assertRaisesRegex(n.AuditError, "api_destination_rejected"):
            n.HTTP().request("https://evil.test", data=b"private")

    def test_api_response_is_bounded_before_json_parsing(self):
        class Response:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, count):
                self.requested = count
                return b"x" * count
        transport = n.HTTP()
        response = Response()
        with patch.object(transport.opener, "open", return_value=response):
            with self.assertRaisesRegex(n.AuditError, "response_too_large"):
                transport.request(n.API_URL, data=b"{}")
        self.assertEqual(response.requested, n.MAX_JSON + 1)

    def test_media_redirect_is_revalidated_and_no_api_secret_is_forwarded(self):
        transport = n.HTTP()
        captured = []
        def public_only(url):
            captured.append(url)
            if "evil.test" in url:
                raise n.AuditError("media_url_rejected")
        error = n.urllib.error.HTTPError(URL, 302, "redirect", {"Location": "https://evil.test/record"}, None)
        with patch.object(n, "validate_media_url", side_effect=public_only):
            with patch.object(transport.opener, "open", side_effect=error) as opener:
                with self.assertRaisesRegex(n.AuditError, "media_url_rejected"):
                    transport.request(URL, media=True)
        self.assertEqual(len(captured), 2)
        self.assertEqual(opener.call_count, 1)
        request = opener.call_args.args[0]
        self.assertIsNone(request.data)
        self.assertNotIn("Authorization", dict(request.header_items()))
        self.assertNotIn(TOKEN, repr(request.header_items()))

    def test_media_request_reads_only_the_prefix(self):
        class Response:
            status = 206
            headers = {"Content-Type": "audio/wav"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, count):
                self.count = count
                return b"RIFF0000WAVE"
        response = Response()
        transport = n.HTTP()
        with patch.object(n, "validate_media_url"), patch.object(transport.opener, "open", return_value=response):
            transport.request(URL, media=True)
        self.assertEqual(response.count, n.MAX_AUDIO_PREFIX)

    def test_metadata_drops_arbitrary_strings(self):
        self.assertEqual(n.metadata({"total_items": TOKEN, "limits": {"minute_remaining": 3, "name": TOKEN}}),
                         {"limits": {"minute_remaining": 3}})

    def test_token_file_private_and_regular(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "token")
            path.write_text(TOKEN + "\n")
            path.chmod(0o600)
            self.assertEqual(n.load_token(path), TOKEN)
            path.chmod(0o644)
            with self.assertRaisesRegex(n.AuditError, "token_file_permissions"):
                n.load_token(path)
            link = Path(root, "link")
            link.symlink_to(path)
            with self.assertRaises(n.AuditError):
                n.load_token(link)

    def test_missing_credentials_produces_safe_actionable_report(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(stream):
            self.assertEqual(n.main([]), 2)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["code"], "credentials_required")
        self.assertFalse(result["stage1_complete"])

    def test_partial_stream_is_not_claimed_as_full_asr_or_playback(self):
        result = n.probe_audio(n.Client(TOKEN, FakeHTTP()), [{"full_record_file_link": URL}])
        for key in ("full_audio_validated", "asr_tested", "browser_playback_tested"):
            self.assertFalse(result[key])


if __name__ == "__main__":
    unittest.main()
