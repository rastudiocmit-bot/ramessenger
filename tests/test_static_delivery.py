"""Static delivery: text assets are gzipped and versioned assets are cacheable,
while pages and API answers stay uncompressed-by-rule and never cached."""

import gzip
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "app" / "backend" / "src"
WEB = SRC / "center_messenger" / "web"

try:
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover - the app image always has Starlette
    Starlette = None


def load_security():
    sys.path.insert(0, str(SRC))
    try:
        import center_messenger.config  # noqa: F401
    except ImportError:
        # Only public_url is read from settings here; keep the test runnable
        # where pydantic-settings is not installed.
        stub = types.ModuleType("center_messenger.config")
        stub.Settings = object
        sys.modules["center_messenger.config"] = stub
    from center_messenger import http_security

    return http_security


@unittest.skipIf(Starlette is None, "Starlette is not installed")
class StaticDeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        security = load_security()
        app = Starlette(
            routes=[
                Route("/", lambda request: FileResponse(WEB / "index.html", media_type="text/html")),
                Route("/api/ping", lambda request: JSONResponse({"pong": "x" * 4000})),
                Mount("/assets", StaticFiles(directory=WEB)),
            ]
        )
        app.add_middleware(security.SecurityMiddleware, settings=SimpleNamespace(public_url="https://example.test"))
        app.add_middleware(security.StaticCompressionMiddleware)
        cls.client = TestClient(app)

    def get(self, url):
        return self.client.get(url, headers={"Accept-Encoding": "gzip"})

    def test_versioned_script_is_gzipped_and_cached_for_a_day(self):
        response = self.get("/assets/chat.js?v=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        # A day, not a year: a reused ?v= number must not pin an old file for long.
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400")
        self.assertEqual(response.content, (WEB / "chat.js").read_bytes())

    def test_unchanged_asset_revalidates_cheaply(self):
        first = self.get("/assets/chat.js?v=1")
        again = self.client.get("/assets/chat.js?v=1", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.headers["cache-control"], "public, max-age=86400")

    def test_unversioned_image_is_cached_for_a_day_and_not_recompressed(self):
        response = self.get("/assets/chat-wallpaper.png")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("content-encoding"))
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400")

    def test_page_is_gzipped_but_never_cached(self):
        response = self.get("/")
        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("text/html", response.headers["content-type"])

    def test_api_is_neither_compressed_nor_cached(self):
        response = self.get("/api/ping")
        self.assertIsNone(response.headers.get("content-encoding"))
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_server_timing_and_slow_read_logs_exclude_private_query_and_response_data(self):
        security = load_security()
        with patch.object(security, "perf_counter", side_effect=[10, 11.234]):
            with self.assertLogs("center_messenger.http_security", level="WARNING") as logs:
                response = self.get("/api/ping?private_value=do-not-log")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers["server-timing"],"app;dur=1234.00")
        self.assertEqual(response.headers["cache-control"],"no-store")
        self.assertIn("duration_ms=1234", " ".join(logs.output))
        self.assertNotIn("private_value", " ".join(logs.output))
        self.assertNotIn("do-not-log", " ".join(logs.output))
        self.assertNotIn("pong", " ".join(logs.output))

    def test_missing_asset_is_not_cached(self):
        response = self.get("/assets/missing.js?v=1")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_security_headers_survive(self):
        response = self.get("/assets/chat.js?v=1")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertIn("media-src 'self' blob:", response.headers["content-security-policy"])
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
