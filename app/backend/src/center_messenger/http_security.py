import logging
from time import perf_counter

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from center_messenger.config import Settings


COMPRESSIBLE = (".js", ".css", ".html", ".svg", ".json", ".txt")
logger = logging.getLogger(__name__)


def cache_policy(scope, status):
    """Static files may be cached; pages and API answers never are.

    Assets are kept for a day, then revalidated (StaticFiles answers 304 when a
    file is unchanged). A longer, immutable lifetime would rely on every change
    bumping its ?v= number, and two changes have already picked the same number;
    a day bounds that mistake. Without caching a phone re-downloaded several
    megabytes on every visit.
    """
    if status not in (200, 304) or not scope["path"].startswith("/assets/"):
        return b"no-store"
    return b"public, max-age=86400"


class StaticCompressionMiddleware:
    """Gzip the page and text assets. They hold no secrets, so compression cannot
    leak anything, and on a slow mobile link it cuts the transfer about fourfold."""

    def __init__(self, app, minimum_size=1024):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == "/" or (path.startswith("/assets/") and path.lower().endswith(COMPRESSIBLE)):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


class SecurityMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        mutation = scope["path"].startswith("/api/") and scope["method"] in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        path = scope["path"]
        # Voice input carries up to 180 s of mono PCM as base64 JSON; prompts run to
        # 4000 Cyrillic characters. Every other mutation keeps its small limit.
        body_limit = (
            7_700_000 if path.endswith("/voice/transcribe")
            else 32768 if path.endswith("/messages") or "/ai/agents" in path
            else 8192
        )

        async def secure_send(message):
            if message["type"] == "http.response.start":
                elapsed = (perf_counter() - started) * 1000
                overridden = {
                    b"cache-control",
                    b"content-security-policy",
                    b"x-frame-options",
                    b"referrer-policy",
                    b"x-content-type-options",
                    b"strict-transport-security",
                }
                values = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in overridden
                ]
                values.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", cache_policy(scope, message.get("status"))),
                        (b"server-timing", f"app;dur={elapsed:.2f}".encode("ascii")),
                        (
                            b"content-security-policy",
                            (
                                b"default-src 'self'; script-src 'self'; style-src 'self'; "
                                b"img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                                b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                            ),
                        ),
                    ]
                )
                if self.settings.public_url.startswith("https://"):
                    values.append((b"strict-transport-security", b"max-age=15552000"))
                if elapsed >= 1000 and scope["method"] == "GET" and path.startswith("/api/"):
                    # Route templates carry no customer IDs, query parameters,
                    # message contents or credentials. Time to headers includes
                    # auth, database checkout and reads, never the SSE lifetime.
                    route = getattr(scope.get("route"), "path", "unknown")
                    logger.warning("Slow API read route=%s status=%s duration_ms=%.0f",
                                   route, message.get("status"), elapsed)
                message = dict(message, headers=values)
            await send(message)

        if mutation:
            if headers.get("origin") != self.settings.public_url:
                return await JSONResponse({"detail": "Недопустимый источник запроса."}, 403)(
                    scope, receive, secure_send
                )
            if (
                headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return await JSONResponse({"detail": "Ожидается JSON-запрос."}, 415)(
                    scope, receive, secure_send
                )
            try:
                size = int(headers.get("content-length", "0"))
            except ValueError:
                size = body_limit + 1
            if size < 0 or size > body_limit:
                return await JSONResponse({"detail": "Слишком большой запрос."}, 413)(
                    scope, receive, secure_send
                )
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if mutation and message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise HTTPException(413, "Слишком большой запрос.")
            return message

        return await self.app(scope, limited_receive, secure_send)
