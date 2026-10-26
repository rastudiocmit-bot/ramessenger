"""Small allowlisted MAX Bot API client. No polling and no implicit send retries."""

import hashlib
import json
import re
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from pydantic import Field, SecretStr, field_validator

from center_messenger.auth_security import InputModel

ORIGIN = "https://platform-api2.max.ru"
ROOT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"
UPDATE_TYPES = ["message_created", "message_edited", "message_removed", "bot_started"]
ERRORS = {
    "unauthorized": "MAX не принял токен бота. Проверьте токен в MAX для бизнеса.",
    "forbidden": "MAX запретил действие. Проверьте права бота и доступ к диалогу.",
    "rate_limited": "MAX ограничил частоту запросов. Отправка ожидает в очереди.",
    "tls": "Не удалось проверить сертификат API MAX. Требуется проверка TLS на сервере.",
    "unavailable": "API MAX временно недоступен. Попробуйте позже.",
    "invalid_response": "MAX вернул ответ неизвестного формата. Операция не подтверждена.",
    "rejected": "MAX отклонил запрос. Проверьте подключение и получателя.",
    "subscription_changed": (
        "MAX не подтвердил сохранение прежних подключений. Проверьте настройки бота."
    ),
    "unknown": (
        "Результат отправки неизвестен. Проверьте сообщение в MAX перед повторной отправкой."
    ),
    "session_check_pending": "Проверяем подключение AlfaCRM. Отправка продолжится автоматически.",
    "access_changed": "Отправка отменена: доступ сотрудника изменён.",
    "disconnected": "Подключение MAX отключено. Новые сообщения не отправляются.",
}


class MaxError(Exception):
    def __init__(self, code, retry_after=5):
        self.code, self.retry_after = code, max(1, min(int(retry_after), 300))
        super().__init__(ERRORS.get(code, ERRORS["unavailable"]))


class MaxSettingsInput(InputModel):
    name: str = Field(default="MAX · бот", min_length=1, max_length=100)
    token: SecretStr

    @field_validator("token")
    @classmethod
    def token_valid(cls, value):
        raw = value.get_secret_value()
        # MAX does not specify a JWT-only token alphabet. Permit visible ASCII
        # (including base64 +/=), while excluding whitespace and header injection.
        if not re.fullmatch(r"[\x21-\x7e]{8,4096}", raw) or raw.lower().startswith(
            ("http://", "https://")
        ):
            raise ValueError("Invalid bot token")
        return value

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        if not value.strip():
            raise ValueError("Empty name")
        return value.strip()


class MaxChannelInput(MaxSettingsInput):
    pass


def integer_id(value, *, positive=True):
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]{1,19}", str(value)):
        raise MaxError("invalid_response")
    number = int(value)
    if not -(2**63) <= number < 2**63 or (positive and number <= 0):
        raise MaxError("invalid_response")
    return number


def timestamp(value):
    number = integer_id(value)
    try:
        result = datetime.fromtimestamp(number / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        raise MaxError("invalid_response") from None
    if result.year < 2000 or result.year > 2100:
        raise MaxError("invalid_response")
    return result


def message_id(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(c) < 33 for c in value)
    ):
        raise MaxError("invalid_response")
    return value


def user_name(user):
    if not isinstance(user, dict):
        raise MaxError("invalid_response")
    pieces = [user.get("first_name"), user.get("last_name")]
    if not isinstance(pieces[0], str) or len(pieces[0]) > 500:
        raise MaxError("invalid_response")
    return " ".join(p.strip() for p in pieces if isinstance(p, str) and p.strip())[:500]


def tls_context():
    certificate = Path(__file__).with_name("certs").joinpath("max_root_ca.pem").read_text()
    if hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest() != ROOT_SHA256:
        raise MaxError("tls")
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=certificate)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_call(token, method, path, body=None):
    request = Request(
        ORIGIN + path,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(NoRedirect(), HTTPSHandler(context=tls_context())).open(
            request, timeout=12
        ) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise MaxError("invalid_response")
    except HTTPError as error:
        code = {401: "unauthorized", 403: "forbidden", 429: "rate_limited"}.get(
            error.code, "unavailable" if error.code >= 500 or error.code == 408 else "rejected"
        )
        delay = error.headers.get("Retry-After", "5")
        raise MaxError(code, int(delay) if delay.isdigit() else 5) from None
    except (URLError, ssl.SSLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        code = "tls" if isinstance(reason, ssl.SSLCertVerificationError) else "unavailable"
        raise MaxError(code) from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise MaxError("invalid_response") from None
    if not isinstance(data, dict):
        raise MaxError("invalid_response")
    return data


class MaxClient:
    def __init__(self, token, *, caller=http_call):
        self.token, self.caller = token, caller

    def me(self):
        data = self.caller(self.token, "GET", "/me")
        if data.get("is_bot") is not True:
            raise MaxError("invalid_response")
        return {
            "id": integer_id(data.get("user_id")),
            "name": user_name(data),
            "username": str(data.get("username") or "")[:100],
        }

    def subscriptions(self):
        data = self.caller(self.token, "GET", "/subscriptions")
        subscriptions = data.get("subscriptions")
        if not isinstance(subscriptions, list) or len(subscriptions) > 100:
            raise MaxError("invalid_response")
        for subscription in subscriptions:
            if (
                not isinstance(subscription, dict)
                or not isinstance(subscription.get("url"), str)
                or len(subscription["url"]) > 4096
            ):
                raise MaxError("invalid_response")
            types = subscription.get("update_types")
            if types is not None and (
                not isinstance(types, list)
                or len(types) > 100
                or any(not isinstance(t, str) or len(t) > 100 for t in types)
            ):
                raise MaxError("invalid_response")
        return subscriptions

    def subscribe(self, url, secret):
        result = self.caller(
            self.token,
            "POST",
            "/subscriptions",
            {
                "url": url,
                "secret": secret,
                "update_types": UPDATE_TYPES,
            },
        )
        if result.get("success") is not True:
            raise MaxError("rejected")

    def unsubscribe(self, url):
        result = self.caller(self.token, "DELETE", "/subscriptions?" + urlencode({"url": url}))
        if result.get("success") is not True:
            raise MaxError("rejected")

    def send(self, user_id, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise MaxError("rejected")
        recipient = integer_id(user_id)
        data = self.caller(self.token, "POST", f"/messages?user_id={recipient}", {"text": text})
        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("body"), dict):
            raise MaxError("invalid_response")
        body, target = message["body"], message.get("recipient")
        if (
            body.get("text") != text
            or not isinstance(target, dict)
            or target.get("chat_type") != "dialog"
        ):
            raise MaxError("invalid_response")
        if integer_id(target.get("user_id")) != recipient:
            raise MaxError("invalid_response")
        return {"id": message_id(body.get("mid")), "timestamp": timestamp(message.get("timestamp"))}
