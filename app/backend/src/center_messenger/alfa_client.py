"""Bounded read-only v2api client. No HTML forms, redirects, proxies or writes."""

import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urlsplit

from pydantic import EmailStr, Field, SecretStr, field_validator

from center_messenger.auth_security import InputModel

ERRORS = {
    "unauthorized": "АльфаCRM не приняла email или ключ API (v2api).",
    "forbidden": "Проверьте доступ пользователя CRM к API и нужным филиалам.",
    "branch_required": (
        "CRM не открыла список филиалов. Укажите активный филиал в дополнительных настройках."
    ),
    "rate_limited": "АльфаCRM ограничила частоту запросов. Повторите проверку позже.",
    "network": "Не удалось связаться с АльфаCRM. Сохранённые филиалы доступны из базы.",
    "unavailable": "АльфаCRM временно недоступна. Повторите проверку позже.",
    "invalid_response": (
        "CRM вернула неполные или неподдерживаемые данные. Прежние данные сохранены."
    ),
    "import_limit": "Объём импорта превысил лимит пилотной версии. Прежние данные сохранены.",
    "unsafe_address": "Адрес CRM должен указывать на доступный публичный сервер s20.online.",
    "timeout": "Проверка CRM не завершилась за отведённое время. Повторите позже.",
}


class AlfaError(Exception):
    def __init__(self, code, status=None, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(ERRORS[code])


def normalize_origin(value: str) -> str:
    url = urlsplit(value.strip())
    host = url.hostname or ""
    if (
        url.scheme != "https"
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.s20\.online", host)
        or url.port not in {None, 443}
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("Use an HTTPS s20.online CRM origin")
    return "https://" + host


class AlfaSettingsInput(InputModel):
    origin: str = Field(max_length=300)
    email: EmailStr
    api_key: SecretStr | None = None
    seed_branch_id: int | None = Field(default=None, gt=0, le=2147483647)

    @field_validator("origin")
    @classmethod
    def origin_value(cls, value):
        return normalize_origin(value)

    @field_validator("api_key", mode="before")
    @classmethod
    def api_key_value(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or not 16 <= len(value.strip()) <= 4096:
            raise ValueError("Invalid API key length")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid API key characters")
        return value.strip()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self):
        # Resolve exactly once; connect to that validated address while retaining TLS SNI
        # and certificate hostname verification for the original official CRM hostname.
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise AlfaError("network")
        for entry in addresses:
            if not ipaddress.ip_address(entry[4][0]).is_global:
                raise AlfaError("unsafe_address")
        last_error = None
        for family, socktype, proto, _, address in addresses:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AlfaError("timeout")
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(min(self.timeout, remaining))
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as error:
                sock.close()
                last_error = error
        raise OSError("CRM connection unavailable") from last_error


def secure_post(origin, path, body, token, *, deadline):
    if not re.fullmatch(
        r"/v2api/(?:auth/login|(?:[1-9][0-9]*/)?branch/index|"
        r"[1-9][0-9]*/(?:customer|group|study-status|lead-status)/index|"
        r"[1-9][0-9]*/(?:lesson|tariff)/index|"
        r"[1-9][0-9]*/customer-tariff/index\?customer_id=[1-9][0-9]*|"
        r"[1-9][0-9]*/cgi/index\?group_id=[1-9][0-9]*)",
        path,
    ):
        raise AlfaError("invalid_response")
    origin = normalize_origin(origin)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AlfaError("timeout")
    connection = PinnedHTTPSConnection(
        urlsplit(origin).hostname,
        timeout=min(5, remaining),
        context=ssl.create_default_context(),
        deadline=deadline,
    )
    try:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["X-ALFACRM-TOKEN"] = token
        connection.request("POST", path, json.dumps(body).encode(), headers)
        response = connection.getresponse()
        status = response.status
        retry_after = response.getheader("Retry-After")
        if status not in {200, 201}:
            codes = {401: "unauthorized", 403: "forbidden", 429: "rate_limited"}
            code = codes.get(status, "unavailable" if status >= 500 else "invalid_response")
            raise AlfaError(code, status, retry_after)
        raw = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise AlfaError("timeout")
            part = response.read1(min(65536, 2097153 - len(raw)))
            if not part:
                break
            raw.extend(part)
            if len(raw) > 2097152:
                raise AlfaError("invalid_response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise AlfaError("invalid_response")
        return result
    except AlfaError:
        raise
    except (OSError, http.client.HTTPException):
        raise AlfaError("network") from None
    except (ValueError, UnicodeError):
        raise AlfaError("invalid_response") from None
    finally:
        connection.close()


class AlfaClient:
    def __init__(self, origin, email, credentials, gate, *, poster=secure_post, sleep=time.sleep):
        self.origin, self.email, self.credentials = normalize_origin(origin), email, credentials
        self.gate, self.poster, self.sleep = gate, poster, sleep
        self.deadline = time.monotonic() + 20
        self.token = credentials.get("token")
        expiry = credentials.get("token_until", 0)
        if (
            not isinstance(self.token, str)
            or not isinstance(expiry, int | float)
            or not time.time() < expiry < time.time() + 3600
        ):
            self.token = None

    def _wait(self, seconds):
        if seconds < 0 or time.monotonic() + seconds >= self.deadline:
            raise AlfaError("timeout")
        self.sleep(seconds)

    def _post(self, path, body, token=None):
        for attempt in range(3):
            self.gate()
            try:
                return self.poster(self.origin, path, body, token, deadline=self.deadline)
            except AlfaError as error:
                if error.code not in {"rate_limited", "unavailable", "network"} or attempt == 2:
                    raise
                try:
                    delay = (
                        float(error.retry_after) if error.retry_after is not None else 2**attempt
                    )
                    if not 0 <= delay <= 30:
                        delay = 30
                except (ValueError, TypeError):
                    delay = 2**attempt
                self._wait(max(0.35, delay) + secrets.randbelow(100) / 1000)
        raise AlfaError("unavailable")

    def _login(self):
        data = self._post(
            "/v2api/auth/login", {"email": self.email, "api_key": self.credentials["api_key"]}
        )
        token = data.get("token")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or any(ord(c) < 32 for c in token)
        ):
            raise AlfaError("invalid_response")
        self.token = token
        self.credentials.update(token=token, token_until=time.time() + 3300)

    def call(self, path, body):
        if not self.token:
            self._login()
        try:
            result = self._post(path, body, self.token)
        except AlfaError as error:
            if error.status != 401:
                raise
            self._login()
            result = self._post(path, body, self.token)
        self.credentials["token_until"] = time.time() + 3300
        return result

    def branches(self, seed_branch_id=None):
        path = f"/v2api/{seed_branch_id}/branch/index" if seed_branch_id else "/v2api/branch/index"
        result, seen, total = [], set(), None
        for page in range(1000):
            try:
                data = self.call(path, {"is_active": 1, "page": page, "pageSize": 500})
            except AlfaError as error:
                if not seed_branch_id and error.status in {400, 403, 404}:
                    raise AlfaError("branch_required", error.status) from None
                raise
            items = data.get("items")
            if not isinstance(items, list):
                raise AlfaError("invalid_response")
            reported = data.get("total")
            if reported is not None:
                if isinstance(reported, bool) or not str(reported).isdigit():
                    raise AlfaError("invalid_response")
                reported = int(reported)
                if reported > 100000 or (total is not None and total != reported):
                    raise AlfaError("invalid_response")
                total = reported
            if "page" in data and data["page"] != page:
                raise AlfaError("invalid_response")
            if not items:
                if total is not None and len(result) < total:
                    raise AlfaError("invalid_response")
                return result
            for row in items:
                if not isinstance(row, dict):
                    raise AlfaError("invalid_response")
                identifier, name = row.get("id"), row.get("name")
                if isinstance(identifier, bool) or not str(identifier).isdigit():
                    raise AlfaError("invalid_response")
                identifier = int(identifier)
                if not 1 <= identifier <= 9223372036854775807 or identifier in seen:
                    raise AlfaError("invalid_response")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
                    raise AlfaError("invalid_response")
                seen.add(identifier)
                result.append({"id": identifier, "name": name.strip()})
            if total is not None and len(result) >= total:
                if len(result) != total:
                    raise AlfaError("invalid_response")
                return result
        raise AlfaError("invalid_response")
