#!/usr/bin/env python3
"""Bounded, read-only Novofon preflight. No DB, app imports or call control."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_URL = "https://dataapi-jsonrpc.novofon.ru/v2.0"
MEDIA_HOSTS = frozenset({"app.novofon.ru", "media.novofon.ru"})
METHODS = frozenset({"get.account", "get.calls_report", "get.sip_lines"})
MAX_JSON = 2 * 1024 * 1024
MAX_AUDIO_PREFIX = 65536
LIMIT_FIELDS = ("day_limit", "day_remaining", "day_reset", "minute_limit",
                "minute_remaining", "minute_reset")
ERRORS = frozenset({"access_token_invalid", "access_token_expired", "access_denied",
                    "auth_error", "not_authenticated", "ip_not_whitelisted",
                    "limit_exceeded", "component_disabled", "invalid_parameters",
                    "unexpected_parameters", "sort_prohibited", "permission_denied",
                    "forbidden", "access_token_blocked", "account_inactive",
                    "method_component_disabled", "parameter_component_disabled"})


class AuditError(Exception):
    """Only fixed codes and deliberately filtered metadata may reach stdout."""
    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code = code
        self.details = details

    def public(self):
        return {"status": "error", "code": self.code, **self.details}


def integer(value):
    return value if type(value) is int and 0 <= value <= 10**18 else None


def identifier(value):
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        return value
    return None


def metadata(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    if integer(value.get("total_items")) is not None:
        out["total_items"] = value["total_items"]
    limits = value.get("limits")
    if isinstance(limits, dict):
        out["limits"] = {k: limits[k] for k in LIMIT_FIELDS if integer(limits.get(k)) is not None}
    return out


def load_token(filename):
    if not filename:
        value = os.environ.get("NOVOFON_ACCESS_TOKEN", "")
    else:
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise AuditError("token_file_permissions")
                raw = source.read(4097)
                if len(raw) > 4096:
                    raise AuditError("invalid_token")
                value = raw.decode("utf-8")
        except AuditError:
            raise
        except (OSError, UnicodeError):
            raise AuditError("token_file_unreadable") from None
    value = value.strip()
    if not value:
        raise AuditError("credentials_required")
    if not 16 <= len(value) <= 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise AuditError("invalid_token")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_media_url(url, resolver=socket.getaddrinfo):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise AuditError("media_url_rejected") from None
    if (parsed.scheme != "https" or parsed.hostname not in MEDIA_HOSTS
            or port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment or any(ord(c) < 32 or c == "\\" for c in url)):
        raise AuditError("media_url_rejected")
    try:
        addresses = resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise AuditError("media_address_rejected")
    except AuditError:
        raise
    except (OSError, ValueError):
        raise AuditError("media_dns_failed") from None


class HTTP:
    def __init__(self, timeout=8):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, url, *, data=None, media=False):
        headers = {"User-Agent": "CenterMessenger-Novofon-ReadOnlyAudit/1"}
        if media:
            headers.update({"Range": f"bytes=0-{MAX_AUDIO_PREFIX - 1}",
                            "Accept": "audio/*,application/octet-stream"})
        else:
            if url != API_URL or data is None:
                raise AuditError("api_destination_rejected")
            headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        for hop in range(4):
            if media:
                validate_media_url(url)
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="GET" if media else "POST")
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    cap = MAX_AUDIO_PREFIX if media else MAX_JSON
                    body = response.read(cap if media else cap + 1)
                    if len(body) > cap:
                        raise AuditError("response_too_large")
                    return response.status, response.headers, body
            except urllib.error.HTTPError as error:
                status, location = error.code, error.headers.get("Location")
                error.close()
                if media and status in (301, 302, 303, 307, 308) and location and hop < 3:
                    url = urllib.parse.urljoin(url, location)
                    continue
                raise AuditError("media_http_error" if media else "api_http_error",
                                 http_status=status) from None
            except AuditError:
                raise
            except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
                raise AuditError("media_network_error" if media else "api_network_error") from None
        raise AuditError("media_redirect_limit")


class Client:
    def __init__(self, token, transport=None):
        self.token = token
        self.transport = transport or HTTP()
        self.requests = 0
        self.quota = {}

    def read(self, method, **params):
        if method not in METHODS or "access_token" in params:
            raise AuditError("method_rejected")
        limits = self.quota.get("limits", {})
        if limits.get("minute_remaining") == 0 or limits.get("day_remaining") == 0:
            raise AuditError("quota_exhausted", metadata=self.quota)
        request_id = secrets.token_hex(12)
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                              "params": {**params, "access_token": self.token}}).encode()
        self.requests += 1
        status, headers, raw = self.transport.request(API_URL, data=payload)
        if status != 200:
            raise AuditError("api_http_error", http_status=status)
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AuditError("api_response_not_json") from None
        if (not isinstance(doc, dict) or doc.get("jsonrpc") != "2.0" or "id" not in doc
                or (doc["id"] != request_id and not (doc["id"] is None and "error" in doc))):
            raise AuditError("api_response_mismatch")
        if "error" in doc:
            error = doc["error"]
            if not isinstance(error, dict):
                raise AuditError("api_response_schema")
            detail = error.get("data") if isinstance(error.get("data"), dict) else {}
            self.quota = metadata(detail.get("metadata", doc.get("metadata")))
            code = error.get("code")
            mnemonic = detail.get("mnemonic")
            public_ip = None
            # Never expose the raw provider message/params: they may echo a key.
            if mnemonic == "ip_not_whitelisted" and isinstance(detail.get("params"), dict):
                try:
                    address = ipaddress.ip_address(detail["params"].get("ip"))
                    if address.is_global:
                        public_ip = str(address)
                except (ValueError, TypeError):
                    pass
            raise AuditError("api_rejected",
                             rpc_code=code if type(code) is int else None,
                             mnemonic=mnemonic if isinstance(mnemonic, str) and mnemonic in ERRORS else "provider_error",
                             **({"observed_ip": public_ip} if public_ip else {}),
                             metadata=self.quota)
        result = doc.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise AuditError("api_response_schema")
        if any(not isinstance(row, dict) for row in result["data"]):
            raise AuditError("api_response_schema")
        self.quota = metadata(result.get("metadata", result.get("meta_data")))
        return result["data"], self.quota


def recording_url(row):
    full = row.get("full_record_file_link")
    if isinstance(full, str) and full:
        return full
    communication = identifier(row.get("communication_id"))
    if communication:
        for field, kind in (("wav_call_records", "wav"), ("call_records", "talk")):
            entries = row.get(field)
            if isinstance(entries, list):
                for item in entries:
                    record = identifier(item)
                    if record:
                        return f"https://app.novofon.ru/system/media/{kind}/{communication}/{record}/"
    return None


def audio_kind(body):
    if body.startswith(b"RIFF") and body[8:12] == b"WAVE":
        return "wav"
    if body.startswith(b"OggS"):
        return "ogg"
    if body.startswith(b"ID3") or (len(body) > 1 and body[0] == 255 and body[1] & 0xE0 == 0xE0):
        return "mp3_or_aac"
    if body.startswith(b"fLaC"):
        return "flac"
    if len(body) > 11 and body[4:8] == b"ftyp":
        return "mp4_container"
    if body.startswith(b"\x1aE\xdf\xa3"):
        return "webm_container"
    return None


def probe_audio(client, rows):
    for row in rows:
        url = recording_url(row)
        if not url:
            continue
        try:
            status, headers, body = client.transport.request(url, media=True)
            content_type = headers.get("Content-Type", "").split(";", 1)[0].lower()
            if status not in (200, 206):
                raise AuditError("media_http_error", http_status=status)
            if content_type in ("text/html", "application/json") or body.lstrip()[:1] in (b"<", b"{"):
                raise AuditError("media_is_not_audio")
            kind = audio_kind(body)
            if not kind:
                raise AuditError("media_format_unconfirmed")
            return {"status": "prefix_detected", "container": kind,
                    "bytes_checked": len(body), "partial_response": status == 206,
                    "full_audio_validated": False, "asr_tested": False,
                    "browser_playback_tested": False}
        except AuditError as error:
            return error.public()
    return {"status": "no_recording_in_sample"}


def audit(client, *, hours=24, limit=30, probe_recording=False, now=None):
    if not 1 <= hours <= 168 or not 1 <= limit <= 100:
        raise AuditError("invalid_sample_window")
    report = {"read_only": True, "outbound_call_attempts": 0, "database_writes": 0,
              "stage1_complete": False, "checks": {}, "browser_call": {
                  "status": "not_tested", "required_for_first_release": True,
                  "reason": "WebRTC_or_SIP_integration_and_two_way_audio_must_be_verified"},
              "limits": ["Single report page only; not a complete history import",
                         "Audio probe checks a bounded prefix, not the full recording or ASR",
                         "SIP lines and API access do not prove browser calling"]}
    checks = report["checks"]
    now = now or datetime.now(timezone.utc)
    try:
        accounts, meta = client.read("get.account")
        if len(accounts) != 1 or not identifier(accounts[0].get("app_id")):
            raise AuditError("account_schema")
        tz = accounts[0].get("timezone")
        try:
            zone = ZoneInfo(tz) if isinstance(tz, str) else None
        except (ValueError, ZoneInfoNotFoundError):
            zone = None
        if zone is None:
            raise AuditError("account_timezone_invalid")
        checks["account"] = {"status": "ok", "account_id": identifier(accounts[0]["app_id"]),
                             "timezone": tz, "metadata": meta}
    except AuditError as error:
        checks["account"] = error.public()
        report["api_requests"] = client.requests
        return report
    start = now - timedelta(hours=hours)
    try:
        rows, meta = client.read("get.calls_report", limit=limit, offset=0,
                                date_from=start.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
                                date_till=now.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
                                sort=[{"field": "start_time", "order": "desc"}],
                                fields=["id", "communication_id", "direction", "is_lost",
                                        "contact_phone_number", "call_records", "wav_call_records",
                                        "full_record_file_link", "talk_duration", "employees", "is_transfer"])
        if len(rows) > limit or any(not identifier(r.get("id")) or r.get("direction") not in ("in", "out") for r in rows):
            raise AuditError("calls_schema")
        salt = secrets.token_bytes(32)
        def phone_tag(row):
            phone = row.get("contact_phone_number")
            if not isinstance(phone, str) or not phone:
                return None
            return hmac.new(salt, phone.encode(), hashlib.sha256).hexdigest()[:12]
        checks["journal"] = {"status": "ok", "sampled": len(rows), "metadata": meta,
                             "direction_counts": dict(Counter(r["direction"] for r in rows)),
                             "missed_in_sample": sum(r.get("is_lost") is True for r in rows),
                             "transferred_in_sample": sum(r.get("is_transfer") is True for r in rows),
                             "recordings_in_sample": sum(bool(recording_url(r)) for r in rows),
                             "calls": [{"call_id": identifier(r["id"]),
                                        "communication_id": identifier(r.get("communication_id")),
                                        "direction": r["direction"], "phone_tag": phone_tag(r),
                                        "has_recording": bool(recording_url(r))} for r in rows]}
        checks["audio"] = probe_audio(client, rows) if probe_recording else {"status": "not_requested"}
    except AuditError as error:
        checks["journal"] = error.public()
        checks["audio"] = {"status": "not_tested"}
    try:
        lines, meta = client.read("get.sip_lines", limit=100, offset=0,
                                  fields=["id", "type", "status", "physical_state", "billing_state", "employee_id"])
        checks["sip_lines"] = {"status": "ok", "sampled": len(lines), "metadata": meta,
                               "outgoing_capable": sum(r.get("type") == "in_out" for r in lines),
                               "note": "No passwords or browser credentials requested"}
    except AuditError as error:
        checks["sip_lines"] = error.public()
    report["api_requests"] = client.requests
    report["checked_at"] = now.isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Novofon stage 1 checks; never initiates calls.")
    parser.add_argument("--token-file", type=Path, help="Private regular file mode 0600/0400; else NOVOFON_ACCESS_TOKEN")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--probe-recording", action="store_true", help="Read at most 64 KiB of one existing recording")
    args = parser.parse_args(argv)
    if not 1 <= args.hours <= 168 or not 1 <= args.limit <= 100:
        parser.error("hours must be 1..168; limit must be 1..100")
    started = time.monotonic()
    try:
        result = audit(Client(load_token(args.token_file)), hours=args.hours, limit=args.limit,
                       probe_recording=args.probe_recording)
    except AuditError as error:
        result = {"read_only": True, "outbound_call_attempts": 0, "database_writes": 0,
                  "stage1_complete": False, **error.public()}
    except Exception:
        # Do not serialize exceptions containing requests, secrets, URLs or customer data.
        result = {"read_only": True, "stage1_complete": False, "status": "error", "code": "audit_failed"}
    result["duration_seconds"] = round(time.monotonic() - started, 2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = result.get("status") == "error" or any(
        c.get("status") == "error" for c in result.get("checks", {}).values())
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
