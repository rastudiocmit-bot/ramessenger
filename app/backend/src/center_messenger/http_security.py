from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from center_messenger.config import Settings


class SecurityMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        mutation = scope["path"].startswith("/api/") and scope["method"] in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        body_limit = 32768 if scope["path"].endswith("/messages") else 8192

        async def secure_send(message):
            if message["type"] == "http.response.start":
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
                        (b"cache-control", b"no-store"),
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
