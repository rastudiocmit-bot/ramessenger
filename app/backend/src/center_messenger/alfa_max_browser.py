"""Isolated server-side browser for AlfaCRM's native MAX pages."""

import ipaddress
import json
import logging
import re
import socket
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from center_messenger.alfa_client import normalize_origin

MAX_HTML_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 1_000_000
MOSCOW = ZoneInfo("Europe/Moscow")
HISTORY_READ_ATTEMPTS = 3
# Chromium net/base/net_error_list.h. Only these fixed tokens can leave the
# browser exception; arbitrary text, URLs and browser state are never retained.
TRANSIENT_NET_ERRORS = frozenset({
    "ERR_ABORTED", "ERR_FAILED", "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED",
    "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_FAILED", "ERR_CONNECTION_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_EMPTY_RESPONSE",
    "ERR_HTTP2_PROTOCOL_ERROR", "ERR_ADDRESS_UNREACHABLE",
})
NETWORK_ERRORS = TRANSIENT_NET_ERRORS | frozenset({
    "ERR_INSUFFICIENT_RESOURCES", "ERR_OUT_OF_MEMORY", "ERR_BLOCKED_BY_CLIENT",
    "ERR_BLOCKED_BY_ADMINISTRATOR", "ERR_BLOCKED_BY_RESPONSE", "ERR_BLOCKED_BY_CSP",
    "ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_COMMON_NAME_INVALID", "ERR_CERT_DATE_INVALID",
    "ERR_SSL_PROTOCOL_ERROR", "ERR_SSL_VERSION_OR_CIPHER_MISMATCH", "ERR_TOO_MANY_REDIRECTS",
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
})


class AlfaBrowserError(Exception):
    def __init__(self, code, *, stage=None, cause=None, request=None, http_status=None, network_error=None, attempts=1):
        self.code = code
        # Diagnostic metadata is deliberately bounded: never retain a URL,
        # response, browser state or the original exception text here.
        self.stage, self.cause = stage, cause
        self.request, self.http_status = request, http_status
        self.network_error, self.attempts = network_error, attempts
        super().__init__(code)


def _browser_cause(error):
    # Playwright has its own TimeoutError. Compare only the class hierarchy;
    # do not inspect messages, which may contain URLs, cookies or form values.
    classes = {kind.__name__ for kind in type(error).__mro__}
    if "TimeoutError" in classes:
        return "timeout"
    if "TargetClosedError" in classes:
        return "browser_closed"
    return "browser_error"


def _browser_network_error(error):
    match = re.search(r"\bnet::(ERR_[A-Z_]+)\b", str(error)[:2048])
    return match[1] if match and match[1] in NETWORK_ERRORS else None


def _retry_history_read(error):
    if error.code != "crm_unavailable" or error.stage not in {"navigation", "check_session"}:
        return False
    if error.http_status is not None:
        return error.http_status in {408, 500, 502, 503, 504}
    if error.network_error:
        return error.network_error in TRANSIENT_NET_ERRORS
    return error.stage == "navigation" and error.cause in {"timeout", "browser_closed", "browser_error"}


def _public_dns(host):
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise AlfaBrowserError("unsafe_address")
    except OSError:
        raise AlfaBrowserError("network") from None


def _customer_link(anchor, origin):
    try:
        address = urlsplit(anchor.get("href", ""))
        if address.scheme or address.netloc:
            base = urlsplit(origin)
            if (address.scheme, address.netloc) != (base.scheme, base.netloc):
                return None
        query = parse_qs(address.query, strict_parsing=False)
        match = re.fullmatch(r"/company/([1-9][0-9]*)/customer/view/?", address.path)
        customer = query.get("id", [""])[0]
        if match and re.fullmatch(r"[1-9][0-9]{0,18}", customer):
            label = ""
            status_node = anchor.find_next_sibling("small")
            if status_node:
                label = " ".join(status_node.get_text(" ", strip=True).lower().split())
            status = "archive" if "архив" in label else "lead" if label == "лид" else "client"
            return {
                "customer_id": int(customer),
                "branch_id": int(match.group(1)),
                "name": " ".join(anchor.get_text(" ", strip=True).split())[:300],
                "status": status,
            }
    except (ValueError, TypeError):
        return None
    return None


def _parse_source_time(value):
    value = " ".join(str(value).split())
    if not value:
        return None
    if re.fullmatch(r"\d{9,13}(?:\.\d+)?", value):
        try:
            epoch = float(value)
            if epoch >= 1_000_000_000_000:
                epoch /= 1000
            return datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MOSCOW)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _message_time(cell):
    """Prefer AlfaCRM's exact timestamp metadata, then parse visible text."""
    elements = [cell, *cell.select("[datetime],[data-timestamp],[data-time],[data-date],[title]")]
    for element in elements:
        attrs = getattr(element, "attrs", {})
        for key in ("datetime", "data-timestamp", "data-time", "data-date", "title"):
            parsed = _parse_source_time(attrs.get(key))
            if parsed:
                return parsed
    visible = " ".join(cell.get_text(" ", strip=True).split())
    exact = _parse_source_time(visible)
    if exact:
        return exact
    match = re.search(
        r"(\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6}))?)?.*?"
        r"(\d{2})\.(\d{2})\.(\d{4})",
        visible,
    )
    if not match:
        match = re.search(
            r"(\d{2})\.(\d{2})\.(\d{4}).*?(\d{2}):(\d{2})"
            r"(?::(\d{2})(?:[.,](\d{1,6}))?)?", visible,
        )
        if not match:
            return None
        day, month, year, hour, minute, second, fraction = match.groups()
    else:
        hour, minute, second, fraction, day, month, year = match.groups()
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second or 0),
            int((fraction or "0").ljust(6, "0")),
            tzinfo=MOSCOW,
        ).astimezone(UTC)
    except ValueError:
        return None


def _history_url(origin, branch, direction, page):
    base = f"{normalize_origin(origin)}/company/{branch}/max/{direction}"
    return base if page == 0 else f"{base}?page={page + 1}"


def _reject_history_response(reason, branch, direction, page, observed_page=None):
    safe_branch = str(branch) if re.fullmatch(r"[1-9][0-9]{0,18}", str(branch)) else "<invalid>"
    safe_direction = direction if direction in {"receive", "sent"} else "<invalid>"
    safe_page = (
        str(page)
        if isinstance(page, int) and not isinstance(page, bool) and 0 <= page <= 100_000
        else "<invalid>"
    )
    observed = (
        f" observed_page={observed_page}"
        if isinstance(observed_page, int) and not isinstance(observed_page, bool)
        else ""
    )
    logging.getLogger(__name__).warning(
        "AlfaCRM MAX history response rejected reason=%s branch=%s direction=%s page=%s%s",
        reason,
        safe_branch,
        safe_direction,
        safe_page,
        observed,
    )
    raise AlfaBrowserError("invalid_response")


_SYSTEM_ACTOR_MARKERS = (
    "service account",
    "system",
    "систем",
    "сервис",
    "служеб",
    "автомат",
    "auto",
    "рассыл",
    "trigger",
    "триггер",
    "робот",
    "бот",
    "уведом",
    "интеграц",
    "integration",
    "notification",
    "scheduler",
    "schedule",
    "workflow",
    "campaign",
    "маркетинг",
    "массов",
)


def _outgoing_actor(author_cell, row, actor_name, message_text=""):
    """Identify employees from CRM identity metadata; leave ambiguous authors unknown."""
    elements = [row, author_cell, *author_cell.find_all(True)]
    metadata = []
    actor_id = None
    for element in elements:
        for key, raw in element.attrs.items():
            value = " ".join(raw) if isinstance(raw, list) else str(raw)
            if key.startswith("data-") or key in {"class", "role", "title", "aria-label"}:
                metadata.append(f"{key} {value}")
            if key in {"data-user-id", "data-author-id", "data-employee-id", "data-operator-id"}:
                if re.fullmatch(r"[1-9][0-9]{0,18}", value):
                    actor_id = value
        for anchor in element.select("a[href]"):
            address = urlsplit(anchor.get("href", ""))
            if re.search(r"/(?:user|employee|staff|operator)(?:/|$)", address.path, re.I):
                value = parse_qs(address.query).get("id", [""])[0]
                if re.fullmatch(r"[1-9][0-9]{0,18}", value):
                    actor_id = value
    signal = f"{actor_name} {' '.join(metadata)}".casefold()
    if any(marker in signal for marker in _SYSTEM_ACTOR_MARKERS):
        return "automation", actor_id
    explicit_human_role = any(
        re.search(r"(?:^|[=\s:_-])(?:human|employee|operator|manager|staff)(?:$|[\s:_-])", item)
        for item in metadata
    )
    has_crm_sender_phone = any(
        len(re.sub(r"\D", "", element.get_text(" ", strip=True))) in range(10, 16)
        and re.fullmatch(r"[+0-9() .-]+", element.get_text(" ", strip=True))
        for element in author_cell.select("small.text-muted")
    )
    if actor_id or explicit_human_role or has_crm_sender_phone:
        return "human", actor_id
    # A display name alone is insufficient: automatic CRM triggers may use it.
    return "unknown", actor_id


def parse_history_html(html, *, origin, branch, direction, page=0):
    origin = normalize_origin(origin)
    if (
        not re.fullmatch(r"[1-9][0-9]{0,18}", str(branch))
        or direction not in {"receive", "sent"}
        or not isinstance(page, int)
        or isinstance(page, bool)
        or not 0 <= page <= 100_000
    ):
        _reject_history_response("invalid_request", branch, direction, page)
    if not isinstance(html, str):
        _reject_history_response("invalid_html_type", branch, direction, page)
    html_size = len(html.encode("utf-8", "ignore"))
    if html_size > MAX_HTML_BYTES:
        _reject_history_response("html_too_large", branch, direction, page)
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('input[type="password"]'):
        raise AlfaBrowserError("reauth_required")
    table = soup.select_one("table.crm-table")
    if table is None:
        _reject_history_response("history_table_missing", branch, direction, page)
    active = soup.select_one(".pagination li.active a[data-page]")
    active_page = active.get("data-page", "") if active else ""
    if active_page.isdigit() and int(active_page) != page:
        _reject_history_response(
            "pagination_page_mismatch", branch, direction, page, int(active_page)
        )
    summary = "".join(
        (soup.select_one(".summary") or BeautifulSoup("", "html.parser")).stripped_strings
    )
    summary = re.sub(r"[\s\u00a0]", "", summary)
    range_match = re.search(r"(\d+)[–—-](\d+)из(\d+)", summary)
    next_node = soup.select_one(".pagination li.next:not(.disabled) a[href]")
    has_more = (
        int(range_match.group(2)) < int(range_match.group(3)) if range_match else bool(next_node)
    )
    source_rows = table.select("tbody tr[data-key]")
    for row in table.select("tbody tr"):
        if row.find("td", recursive=False) and not row.has_attr("data-key") and not row.select_one(".empty"):
            _reject_history_response("history_row_identity", branch, direction, page)
    if range_match:
        first, last, total = map(int, range_match.groups())
        empty_range = (first, last, total) == (0, 0, 0) and not source_rows
        if not empty_range and (not (1 <= first <= last <= total) or last - first + 1 != len(source_rows)):
            _reject_history_response("history_row_count", branch, direction, page)
        if page > 0 and first == 1:
            _reject_history_response("pagination_page_mismatch", branch, direction, page)
    else:
        first = last = total = None
        if not source_rows and not has_more:
            total = 0
    if not source_rows and has_more:
        _reject_history_response("history_empty_page", branch, direction, page)
    next_page = page + 1 if has_more else None
    messages = []
    identities = set()
    for row in source_rows:
        key = row.get("data-key", "")
        cells = row.find_all("td", recursive=False)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", key) or len(cells) < 5 or key in identities:
            _reject_history_response("history_row_identity", branch, direction, page)
        identities.add(key)
        customers = []
        seen = set()
        for anchor in row.select("a[href]"):
            item = _customer_link(anchor, origin)
            if item:
                identity = (item["branch_id"], item["customer_id"])
                if identity not in seen:
                    seen.add(identity)
                    customers.append(item)
        actor_name = " ".join(cells[2].get_text(" ", strip=True).split())[:300]
        text_node = BeautifulSoup(str(cells[3]), "html.parser")
        for node in text_node.select("script,style,iframe"):
            node.decompose()
        for node in text_node.select("small"):
            if node.select_one(".ion-android-attach,.ion-paperclip,.fa-paperclip"):
                node.decompose()
        for node in text_node.select("br"):
            node.replace_with("\n")
        for node in text_node.select("p,div,li,blockquote"):
            node.append("\n")
        message_text = text_node.get_text().strip()
        actor_type, actor_id = (
            ("contact", None)
            if direction == "receive"
            else _outgoing_actor(cells[2], row, actor_name, message_text)
        )
        when = _message_time(cells[4])
        if not when:
            # Advancing past an unparsed timestamp permanently lost messages.
            _reject_history_response("history_row_time", branch, direction, page)
        links = []
        for anchor in cells[3].select("a[href]"):
            name = " ".join(anchor.get_text(" ", strip=True).split()) or "Вложение"
            if name:
                links.append({"name": name[:200]})
        messages.append(
            {
                "record_id": key,
                "crm_order": int(key) if re.fullmatch(r"[1-9][0-9]{0,18}", key) else None,
                "branch_id": int(branch),
                "direction": "in" if direction == "receive" else "out",
                "customers": customers,
                "actor_name": actor_name,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "actor_evidence": {"version": 1, "reason": "client_incoming" if direction == "receive" else {"human": "crm_employee", "automation": "crm_automation", "unknown": "unverified_sender"}[actor_type]},
                "text": message_text,
                "occurred_at": when,
                "attachments": links[:20],
                "failed": "danger" in (row.get("class") or []),
            }
        )
    return {"messages": messages, "next_page": next_page, "has_more": has_more,
            "total": total, "range_start": first, "range_end": last, "row_count": len(source_rows)}


def _find_form_markup(value):
    """Find bounded, nested HTML fragments that might contain a compose form."""
    pending = [(value, 0)]
    inspected = 0
    marker = re.compile(r"<(?:form|input|textarea)\b", re.I)
    while pending and inspected < 256:
        item, depth = pending.pop()
        inspected += 1
        if depth > 6:
            continue
        if isinstance(item, str):
            if len(item) <= 1_000_000 and marker.search(item):
                return item
        elif isinstance(item, dict):
            pending.extend((child, depth + 1) for child in list(item.values())[:32])
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item[:32])
    return None


def _compose_response_shape(raw):
    """Describe a CRM response without logging its text, URLs, fields, or tokens."""
    if not isinstance(raw, str):
        return f"kind={type(raw).__name__}"
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        has_form = bool(re.search(r"<(?:form|input|textarea)\b", raw[:1_000_000], re.I))
        return f"kind=html form_markup={'yes' if has_form else 'no'}"
    if not isinstance(value, dict):
        return f"kind=json root={type(value).__name__}"
    modal = value.get("modal_open")
    if "modal_open" not in value:
        modal_kind, modal_fields = "missing", 0
    elif isinstance(modal, dict):
        modal_kind, modal_fields = "object", len(modal)
    elif isinstance(modal, list):
        modal_kind, modal_fields = "array", len(modal)
    elif isinstance(modal, str):
        modal_kind, modal_fields = "string", 0
    elif modal is None:
        modal_kind, modal_fields = "null", 0
    else:
        modal_kind, modal_fields = "scalar", 0
    has_form = _find_form_markup(modal) is not None
    return (
        f"kind=json root_fields={len(value)} modal_open={modal_kind} "
        f"modal_fields={modal_fields} modal_form_markup={'yes' if has_form else 'no'}"
    )


def _compose_form_diagnostics(raw):
    """Log only the compose form schema; never values, tokens, or message text."""
    try:
        envelope = _json_envelope(raw)
    except AlfaBrowserError as error:
        return f"envelope_error={error.code}"
    markup = envelope.get("html")
    if not markup:
        return "forms=0"
    soup = BeautifulSoup(markup, "html.parser")
    forms = soup.select("form[action]")
    if not forms:
        return "forms=0"
    form = forms[0]
    query = parse_qs(urlsplit(form.get("action", "")).query, keep_blank_values=True)
    peers = query.get("maxUserId", [])
    if len(peers) == 1 and re.fullmatch(r"[1-9][0-9]{0,18}", peers[0]):
        action_peer = "valid"
    elif "maxUserId" in query:
        action_peer = "empty_or_invalid"
    else:
        action_peer = "absent"
    inputs = form.select("input[name]")
    relevant_names = sorted(
        {
            re.sub(r"[^A-Za-z0-9_\[\]-]", "?", item.get("name", ""))[:80]
            for item in inputs
            if re.search(r"(?:max.?user|peer|recipient|contact.?id)", item.get("name", ""), re.I)
        }
    )[:8]
    recipient_count = sum(
        item.get("name") == "MaxComposeBulkForm[related_ids][]" and bool(item.get("value"))
        for item in inputs
    )
    csrf = any(item.get("name") == "_csrf" and bool(item.get("value")) for item in inputs)
    has_text = form.select_one('textarea[name="MaxComposeBulkForm[text]"]') is not None
    names = ",".join(relevant_names) if relevant_names else "none"
    return (
        f"forms={len(forms)} action_maxUserId={action_peer} "
        f"recipient_inputs={recipient_count} csrf={'present' if csrf else 'missing'} "
        f"text_field={'present' if has_text else 'missing'} peer_field_names={names}"
    )


def _log_compose_failure(phase, reason, *, status=None, raw=None, exception=None):
    safe_phase = phase if re.fullmatch(r"[a-z_]{1,40}", phase or "") else "unknown"
    safe_reason = reason if re.fullmatch(r"[a-z_]{1,40}", reason or "") else "unknown"
    safe_status = status if isinstance(status, int) and not isinstance(status, bool) else "none"
    safe_exception = (
        exception
        if isinstance(exception, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", exception)
        else "none"
    )
    logging.getLogger(__name__).warning(
        "AlfaCRM MAX compose form rejected "
        "phase=%s reason=%s status=%s response=%s exception=%s form_schema=%s",
        safe_phase,
        safe_reason,
        safe_status,
        _compose_response_shape(raw) if raw is not None else "unavailable",
        safe_exception,
        _compose_form_diagnostics(raw) if raw is not None else "unavailable",
    )


def _explicit_max_recipient_error(*values):
    """Classify only an explicit AlfaCRM statement that the recipient has no MAX account."""
    parts = []

    def collect(value, depth=0):
        if depth > 5 or len(parts) >= 64:
            return
        if isinstance(value, str):
            parts.append(value[:5000])
        elif isinstance(value, dict):
            for nested in list(value.values())[:32]:
                collect(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value[:32]:
                collect(nested, depth + 1)

    for value in values:
        collect(value)
    text = " ".join(parts).lower().replace("ё", "е")
    if not text:
        return None
    patterns = (
        r"(?:max|макс).{0,100}(?:не найден|не зарегистр|не подключ|отсутств|not found|not registered|not linked|does not exist)",
        r"(?:не найден|не зарегистр|не подключ|отсутств|not found|not registered|not linked|does not exist).{0,100}(?:max|макс)",
    )
    return "max_recipient_missing" if any(re.search(pattern, text, re.I | re.S) for pattern in patterns) else None


def _json_envelope(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"html": raw}
    for _ in range(3):
        if isinstance(value, str):
            try:
                value = json.loads(value)
                continue
            except ValueError:
                return {"html": value} if "<" in value else {"empty": True}
        break
    if not isinstance(value, dict):
        return {"html": raw} if isinstance(value, str) else {"empty": True}
    error = (
        value.get("toastr_error")
        or value.get("errorMessage")
        or value.get("error_message")
        or value.get("errors")
    )
    if error:
        raise AlfaBrowserError(_explicit_max_recipient_error(error) or "crm_rejected")
    for key in ("form", "html", "content", "body", "data", "result"):
        markup = _find_form_markup(value.get(key))
        if markup:
            return {"html": markup}
    modal_markup = _find_form_markup(value.get("modal_open"))
    if modal_markup:
        return {"html": modal_markup}
    urls = []

    def scan(item, depth=0):
        if depth > 5:
            return
        if isinstance(item, str) and (item.startswith("/company/") or item.startswith("https://")):
            urls.append(item)
        elif isinstance(item, dict):
            for nested in list(item.values())[:32]:
                scan(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item[:32]:
                scan(nested, depth + 1)

    scan(value.get("modal_open"))
    return {"modal_urls": urls} if urls else {"empty": True}


def _validated_bulk_url(value, *, origin, branch, customer):
    try:
        parts = urlsplit(value)
        base = urlsplit(origin)
        if parts.scheme or parts.netloc:
            if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
                raise ValueError
        if parts.path != f"/company/{branch}/max/bulk":
            raise ValueError
        query = parse_qs(parts.query, keep_blank_values=True)
        ids = query.get("related_ids[]", [])
        if ids and ids != [str(customer)]:
            raise ValueError
        if query.get("related_class", ["Customer"])[0] != "Customer":
            raise ValueError
        return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))
    except (ValueError, TypeError):
        raise AlfaBrowserError("invalid_recipient") from None


def _validated_compose_url(value, *, origin, branch, customer):
    """Validate a CRM modal URL for one exact customer and branch."""
    address = _validated_bulk_url(value, origin=origin, branch=branch, customer=customer)
    query = parse_qs(urlsplit(address).query, keep_blank_values=True)
    if query.get("related_ids[]") != [str(customer)]:
        raise AlfaBrowserError("invalid_recipient")
    if query.get("related_class", ["Customer"]) != ["Customer"]:
        raise AlfaBrowserError("invalid_recipient")
    return address


def _customer_card_compose_url(markup, *, origin, branch, customer):
    """Find the exact native MAX compose route exposed by a customer card."""
    if not isinstance(markup, str) or len(markup.encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        raise AlfaBrowserError("form_unavailable")
    soup = BeautifulSoup(markup, "html.parser")
    if soup.select_one('input[type="password"]'):
        raise AlfaBrowserError("reauth_required")

    candidates = []
    onclick_url = re.compile(r"""["']((?:https://[^"']+)?/company/\d+/max/bulk[^"']*)["']""")
    for node in soup.select("[href], [data-url], [data-href], [onclick]"):
        values = [node.get(name) for name in ("href", "data-url", "data-href")]
        handler = node.get("onclick", "")
        values.extend(match.group(1) for match in onclick_url.finditer(handler))
        for value in values:
            if not value:
                continue
            try:
                candidates.append(
                    _validated_compose_url(value, origin=origin, branch=branch, customer=customer)
                )
            except AlfaBrowserError:
                continue

    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise AlfaBrowserError("multiple_recipients")
    return None


def parse_compose_form(html, *, origin, branch, customer):
    origin = normalize_origin(origin)
    envelope = _json_envelope(html)
    if envelope.get("modal_urls"):
        valid = []
        for value in envelope["modal_urls"]:
            try:
                valid.append(
                    _validated_compose_url(value, origin=origin, branch=branch, customer=customer)
                )
            except AlfaBrowserError:
                continue
        valid = list(dict.fromkeys(valid))
        if len(valid) == 1:
            return {"compose_url": valid[0]}
        if valid:
            raise AlfaBrowserError("multiple_recipients")
        raise AlfaBrowserError("form_unavailable")
    markup = envelope.get("html")
    if not markup:
        raise AlfaBrowserError("form_unavailable")
    soup = BeautifulSoup(markup, "html.parser")
    if soup.select_one('input[type="password"]'):
        raise AlfaBrowserError("reauth_required")
    for form in soup.select("form[action]"):
        action = _validated_bulk_url(
            form.get("action", ""), origin=origin, branch=branch, customer=customer
        )
        fields = []
        recipient_ids = []
        for item in form.select("input[name]"):
            name, value = item.get("name", ""), item.get("value", "")
            kind = item.get("type", "").lower()
            if name == "MaxComposeBulkForm[related_ids][]" and value:
                recipient_ids.append(value)
            if kind in {"file", "submit", "button", "password", "reset"}:
                continue
            if kind in {"checkbox", "radio"} and not item.has_attr("checked"):
                continue
            fields.append([name, value])
        textarea = form.select_one('textarea[name="MaxComposeBulkForm[text]"]')
        query_ids = parse_qs(urlsplit(action).query, keep_blank_values=True).get(
            "related_ids[]", []
        )
        ids = recipient_ids or query_ids
        if ids != [str(customer)] or (query_ids and query_ids != [str(customer)]):
            raise AlfaBrowserError("invalid_recipient")
        if not any(name == "_csrf" and value for name, value in fields):
            raise AlfaBrowserError("csrf_missing")
        peers = parse_qs(urlsplit(action).query, keep_blank_values=True).get("maxUserId", [])
        peer = peers[0] if len(peers) == 1 else ""
        if not peer or not re.fullmatch(r"[1-9][0-9]{0,18}", peer):
            raise AlfaBrowserError("recipient_missing")
        if textarea is None:
            raise AlfaBrowserError("form_unavailable")
        max_length = textarea.get("maxlength", "8192")
        if not str(max_length).isdigit():
            max_length = "8192"
        return {
            "url": action,
            "fields": fields,
            "max_user_id": peer,
            "customer_id": customer,
            "branch_id": branch,
            "max_length": min(int(max_length), 8192),
        }
    raise AlfaBrowserError(_explicit_max_recipient_error(markup) or "form_unavailable")


class AlfaMaxBrowser:
    """Runs one short-lived isolated Chromium context per CRM operation."""

    @staticmethod
    def _playwright():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise AlfaBrowserError("browser_unavailable") from None
        return sync_playwright

    @staticmethod
    def _route_factory(origin):
        origin = normalize_origin(origin)
        crm_host = urlsplit(origin).hostname
        allowed_hosts = {crm_host, "cdn.alfacrm.pro"}

        def route(request_route):
            try:
                parts = urlsplit(request_route.request.url)
                if parts.scheme != "https" or parts.hostname not in allowed_hosts:
                    request_route.abort()
                    return
                _public_dns(parts.hostname)
                request_route.continue_()
            except AlfaBrowserError:
                request_route.abort()

        return route

    @staticmethod
    def _open(origin, storage_state=None):
        sync_playwright = AlfaMaxBrowser._playwright()
        host = urlsplit(normalize_origin(origin)).hostname
        _public_dns(host)
        manager = browser = context = None
        stage = "start_driver"
        try:
            manager = sync_playwright().start()
            stage = "launch_browser"
            browser = manager.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            stage = "open_context"
            context = browser.new_context(storage_state=storage_state, locale="ru-RU", timezone_id="Europe/Moscow")
            context.route("**/*", AlfaMaxBrowser._route_factory(origin))
            stage = "open_page"
            page = context.new_page()
            return manager, browser, context, page
        except AlfaBrowserError:
            AlfaMaxBrowser._close(manager, browser, context)
            raise
        except Exception as error:
            AlfaMaxBrowser._close(manager, browser, context)
            raise AlfaBrowserError("browser_unavailable", stage=stage, cause=_browser_cause(error)) from None

    @staticmethod
    def _close(manager, browser, context):
        for item in (context, browser, manager):
            try:
                if item:
                    item.close() if item is not manager else item.stop()
            except Exception:
                pass

    @staticmethod
    def _first_visible(page, selectors):
        for selector in selectors:
            candidate = page.locator(selector).first
            try:
                if candidate.is_visible(timeout=200):
                    return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _submit(page):
        button = AlfaMaxBrowser._first_visible(
            page,
            [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Войти")',
                'button:has-text("Подтвердить")',
            ],
        )
        if button is None:
            raise AlfaBrowserError("login_form_unknown")
        button.click(timeout=3000)
        page.wait_for_timeout(900)

    @staticmethod
    def _otp_input(page):
        return AlfaMaxBrowser._first_visible(
            page,
            [
                'input[autocomplete="one-time-code"]',
                'input[name*="code" i]',
                'input[name*="otp" i]',
                'input[inputmode="numeric"]',
            ],
        )

    @staticmethod
    def _password_input(page):
        return AlfaMaxBrowser._first_visible(page, ['input[type="password"]'])

    @staticmethod
    def _check_origin(page, origin):
        if (
            urlsplit(page.url).scheme != "https"
            or urlsplit(page.url).netloc != urlsplit(origin).netloc
        ):
            raise AlfaBrowserError("redirect_blocked")

    def login(self, origin, email, password):
        origin = normalize_origin(origin)
        manager = browser = context = None
        try:
            manager, browser, context, page = self._open(origin)
            page.goto(origin, wait_until="domcontentloaded", timeout=20000)
            self._check_origin(page, origin)
            password_input = self._password_input(page)
            if password_input is None:
                raise AlfaBrowserError("login_form_unknown")
            email_input = self._first_visible(
                page,
                ['input[type="email"]', 'input[name*="email" i]', 'input[name*="login" i]'],
            )
            if email_input is None:
                raise AlfaBrowserError("login_form_unknown")
            email_input.fill(email)
            password_input.fill(password)
            self._submit(page)
            self._check_origin(page, origin)
            if self._otp_input(page) is not None:
                return "otp_required", context.storage_state()
            if self._password_input(page) is not None:
                raise AlfaBrowserError("credentials_rejected")
            return "active", context.storage_state()
        except AlfaBrowserError:
            raise
        except Exception:
            raise AlfaBrowserError("login_failed") from None
        finally:
            self._close(manager, browser, context)

    def verify_otp(self, origin, state, code):
        origin = normalize_origin(origin)
        manager = browser = context = None
        try:
            manager, browser, context, page = self._open(origin, state)
            page.goto(origin, wait_until="domcontentloaded", timeout=20000)
            self._check_origin(page, origin)
            otp = self._otp_input(page)
            if otp is None:
                if self._password_input(page) is None:
                    return "active", context.storage_state()
                raise AlfaBrowserError("otp_unavailable")
            otp.fill(code)
            self._submit(page)
            self._check_origin(page, origin)
            if self._otp_input(page) is not None or self._password_input(page) is not None:
                raise AlfaBrowserError("otp_rejected")
            return "active", context.storage_state()
        except AlfaBrowserError:
            raise
        except Exception:
            raise AlfaBrowserError("otp_rejected") from None
        finally:
            self._close(manager, browser, context)

    def read_history(self, origin, state, branch, direction, page_number=0):
        results, next_state = self.read_histories(origin, state, [(branch, direction, page_number)])
        return results[0], next_state

    @staticmethod
    def _read_history_page(page, origin, branch, direction, page_number):
        stage, request = "navigation", (branch, direction, page_number)
        try:
            response = page.goto(_history_url(origin, branch, direction, page_number),
                                 wait_until="domcontentloaded", timeout=20000)
            stage = "check_session"
            AlfaMaxBrowser._check_origin(page, origin)
            if response is None or response.status != 200:
                raise AlfaBrowserError("crm_unavailable", http_status=response.status if response else None)
            stage = "parse_page"
            return parse_history_html(response.text(), origin=origin, branch=branch,
                                      direction=direction, page=page_number)
        except AlfaBrowserError as error:
            error.stage, error.request = error.stage or stage, request
            raise
        except Exception as error:
            raise AlfaBrowserError("crm_unavailable", stage=stage, request=request,
                                   cause=_browser_cause(error), network_error=_browser_network_error(error)) from None

    def read_histories(self, origin, state, requests, *, on_progress=None, continue_on_error=False,
                       before_read=None, on_result=None):
        origin = normalize_origin(origin)
        for branch, direction, page_number in requests:
            if (
                not re.fullmatch(r"[1-9][0-9]{0,18}", str(branch))
                or direction not in {"receive", "sent"}
                or not 0 <= int(page_number) <= 100_000
            ):
                raise AlfaBrowserError("invalid_response")
        manager = browser = context = None
        stage, request, in_callback = "open_browser", None, False
        try:
            manager, browser, context, page = self._open(origin, state)
            results = []
            for branch, direction, page_number in requests:
                request = (branch, direction, page_number)
                if before_read:
                    in_callback = True
                    allowed = before_read(request)
                    in_callback = False
                    if not allowed:
                        result = {"skipped": True}
                        results.append(result)
                        if on_result:
                            in_callback = True
                            on_result(request, result, state)
                            in_callback = False
                        continue
                if on_progress:
                    # One callback per requested page: the worker advances its
                    # visible page counter here. Bounded retries fit inside its
                    # 300-second lease and must not advance that counter again.
                    in_callback = True
                    on_progress()
                    in_callback = False
                try:
                    for attempt in range(1, HISTORY_READ_ATTEMPTS + 1):
                        try:
                            result = self._read_history_page(page, origin, branch, direction, page_number)
                            break
                        except AlfaBrowserError as error:
                            error.attempts = attempt
                            if attempt == HISTORY_READ_ATTEMPTS or not _retry_history_read(error):
                                raise
                            logging.getLogger(__name__).warning(
                                "AlfaCRM MAX history read retry branch=%s direction=%s page=%s attempt=%s cause=%s network_error=%s",
                                branch, direction, page_number, attempt + 1, error.cause, error.network_error)
                            time.sleep(0.5 * attempt)
                            # Discard an interrupted navigation, retaining the
                            # context/cookies. Restart a browser only if its
                            # context can no longer create a tab.
                            try:
                                renewed = context.storage_state()
                                if isinstance(renewed, dict):
                                    state = renewed
                            except Exception:
                                pass
                            try:
                                page.close()
                            except Exception:
                                pass
                            try:
                                page = context.new_page()
                            except Exception:
                                self._close(manager, browser, context)
                                manager = browser = context = None
                                try:
                                    manager, browser, context, page = self._open(origin, state)
                                except AlfaBrowserError as restart_error:
                                    restart_error.request, restart_error.attempts = request, attempt + 1
                                    raise
                    results.append(result)
                except AlfaBrowserError as error:
                    error.request = error.request or request
                    if not continue_on_error or error.code not in {"invalid_response", "crm_unavailable"}:
                        raise
                    results.append({"error": error.code, "stage": error.stage, "cause": error.cause,
                                    "http_status": error.http_status, "network_error": error.network_error,
                                    "attempts": error.attempts})
                if on_result:
                    try:
                        state = context.storage_state()
                    except Exception:
                        pass
                    # Commit each page before a slow or failed later page. A
                    # callback failure is not a navigation/browser failure.
                    in_callback = True
                    on_result(request, results[-1], state)
                    in_callback = False
            stage = "save_session"
            return results, context.storage_state()
        except AlfaBrowserError as error:
            error.stage = error.stage or stage
            error.request = error.request or request
            raise
        except Exception as error:
            if in_callback:
                raise
            raise AlfaBrowserError("crm_unavailable", stage=stage, cause=_browser_cause(error), request=request,
                                   network_error=_browser_network_error(error)) from None
        finally:
            self._close(manager, browser, context)

    def read_sms_templates(self, origin, state, branch):
        origin = normalize_origin(origin)
        if not re.fullmatch(r"[1-9][0-9]{0,18}", str(branch)):
            raise AlfaBrowserError("invalid_response")
        manager = browser = context = None
        try:
            manager, browser, context, page = self._open(origin, state)
            page.goto(
                f"{origin}/settings/{branch}/sms-template/index",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            self._check_origin(page, origin)
            page.wait_for_timeout(2000)
            soup = BeautifulSoup(page.content(), "html.parser")
            templates = []
            for item in soup.select(".sortable1-item[data-id]"):
                raw_id = item.get("data-id", "")
                name_el = item.select_one(".sms-title small")
                text_el = item.select_one(".sms-text small")
                if not re.fullmatch(r"[1-9][0-9]{0,18}", raw_id) or name_el is None:
                    continue
                name = re.sub(r"\s*\[ID\s+[0-9]+\]\s*$", "", name_el.get_text(" ", strip=True))
                body = text_el.get_text("\n", strip=True) if text_el else ""
                templates.append({"id": int(raw_id), "name": name, "body": body})
            return templates
        except AlfaBrowserError:
            raise
        except Exception:
            raise AlfaBrowserError("crm_unavailable") from None
        finally:
            self._close(manager, browser, context)

    def send(self, origin, state, branch, customer, text):
        origin = normalize_origin(origin)
        if (
            not re.fullmatch(r"[1-9][0-9]{0,18}", str(branch))
            or not re.fullmatch(r"[1-9][0-9]{0,18}", str(customer))
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 8192
        ):
            raise AlfaBrowserError("invalid_recipient")

        card_url = f"{origin}/company/{branch}/customer/view?" + urlencode({"id": str(customer)})
        fallback_url = f"{origin}/company/{branch}/max/bulk?" + urlencode(
            [("related_class", "Customer"), ("related_ids[]", str(customer))]
        )
        manager = browser = context = None
        post_started = False
        phase = "open_browser"
        response_status = None
        response_body = None

        def get_form_response(page, address, *, ajax):
            nonlocal response_status, response_body
            result = page.evaluate(
                """async ({url, ajax, maxBytes}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), 20000);
                  try {
                    const headers = ajax ? {'X-Requested-With': 'XMLHttpRequest'} : {};
                    const response = await fetch(url, {
                      method: 'GET', credentials: 'include', cache: 'no-store',
                      redirect: 'follow', headers, signal: controller.signal
                    });
                    const text = await response.text();
                    if (new TextEncoder().encode(text).length > maxBytes) {
                      throw new Error('response_limit');
                    }
                    return {status: response.status, url: response.url, text};
                  } finally {
                    clearTimeout(timer);
                  }
                }""",
                {"url": address, "ajax": ajax, "maxBytes": MAX_RESPONSE_BYTES},
            )
            response_url = urlsplit(result.get("url", ""))
            if response_url.scheme != "https" or response_url.netloc != urlsplit(origin).netloc:
                raise AlfaBrowserError("redirect_blocked")
            body = result.get("text")
            if (
                not isinstance(body, str)
                or len(body.encode("utf-8", "ignore")) > MAX_RESPONSE_BYTES
            ):
                raise AlfaBrowserError("form_unavailable")
            status = result.get("status")
            response_status = status if isinstance(status, int) else None
            response_body = body
            if not isinstance(status, int) or not 200 <= status < 300:
                if status == 401:
                    raise AlfaBrowserError("reauth_required")
                if status == 403:
                    raise AlfaBrowserError("crm_access_denied")
                if status == 404:
                    raise AlfaBrowserError("max_form_missing")
                if status == 429:
                    raise AlfaBrowserError("crm_rate_limited")
                if isinstance(status, int) and status >= 500:
                    raise AlfaBrowserError("crm_unavailable")
                raise AlfaBrowserError("form_unavailable")
            return status, body

        try:
            manager, browser, context, page = self._open(origin, state)

            phase = "load_customer_card"
            card_response = page.goto(card_url, wait_until="domcontentloaded", timeout=20000)
            self._check_origin(page, origin)
            response_status = card_response.status if card_response is not None else None
            if card_response is None or card_response.status != 200:
                if response_status == 401:
                    raise AlfaBrowserError("reauth_required")
                if response_status == 403:
                    raise AlfaBrowserError("crm_access_denied")
                if response_status == 404:
                    raise AlfaBrowserError("crm_customer_missing")
                raise AlfaBrowserError("form_unavailable")
            card_markup = page.content()
            form_url = _customer_card_compose_url(
                card_markup, origin=origin, branch=branch, customer=customer
            )
            if not form_url:
                form_url = fallback_url

            visited_forms = set()
            form = None
            for _ in range(4):
                address = _validated_compose_url(
                    form_url, origin=origin, branch=branch, customer=customer
                )
                if address in visited_forms:
                    raise AlfaBrowserError("form_unavailable")
                visited_forms.add(address)

                phase = "load_compose_form_ajax"
                response_status, response_body = get_form_response(page, address, ajax=True)
                phase = "parse_compose_form"
                try:
                    form = parse_compose_form(
                        response_body, origin=origin, branch=branch, customer=customer
                    )
                except AlfaBrowserError as error:
                    envelope = _json_envelope(response_body)
                    if error.code != "form_unavailable" or not envelope.get("empty"):
                        raise
                    phase = "load_compose_form_html"
                    response_status, response_body = get_form_response(page, address, ajax=False)
                    phase = "parse_compose_form_html"
                    form = parse_compose_form(
                        response_body, origin=origin, branch=branch, customer=customer
                    )

                if "compose_url" not in form:
                    break
                form_url = _validated_compose_url(
                    form["compose_url"], origin=origin, branch=branch, customer=customer
                )
                form = None
            if not form or "compose_url" in form:
                raise AlfaBrowserError("form_unavailable")

            if form["customer_id"] != int(customer) or form["branch_id"] != int(branch):
                raise AlfaBrowserError("invalid_recipient")
            if len(text) > form["max_length"]:
                raise AlfaBrowserError("message_too_long")

            fields = [
                pair
                for pair in form["fields"]
                if pair[0]
                not in {
                    "MaxComposeBulkForm[text]",
                    "MaxComposeBulkForm[template_id]",
                    "MaxComposeBulkForm[is_reload]",
                }
            ]
            fields.extend(
                [
                    ["MaxComposeBulkForm[text]", text],
                    ["MaxComposeBulkForm[template_id]", ""],
                    ["MaxComposeBulkForm[is_reload]", "0"],
                ]
            )
            address = _validated_bulk_url(
                form["url"], origin=origin, branch=branch, customer=customer
            )
            phase = "submit_form"
            post_started = True
            result = page.evaluate(
                """async ({url, fields}) => {
                  const body = new FormData();
                  for (const [name, value] of fields) body.append(name, value);
                  const response = await fetch(url, {
                    method: 'POST', body, credentials: 'include', redirect: 'error',
                    headers: {'X-Requested-With': 'XMLHttpRequest'}
                  });
                  const text = await response.text();
                  if (text.length > 1000000) throw new Error('response_limit');
                  return {status: response.status, url: response.url, text};
                }""",
                {"url": address, "fields": fields},
            )
            response_url = urlsplit(result.get("url", ""))
            if response_url.scheme != "https" or response_url.netloc != urlsplit(origin).netloc:
                raise AlfaBrowserError("send_unknown")
            try:
                data = json.loads(result.get("text", ""))
            except (TypeError, ValueError):
                data = {}
            errors = data.get("errors")
            if data.get("toastr_error") or errors:
                raise AlfaBrowserError(
                    _explicit_max_recipient_error(data.get("toastr_error"), errors)
                    or "crm_rejected"
                )
            if not (200 <= int(result.get("status", 0)) < 300) or not (
                data.get("toastr_success") or data.get("success") is True
            ):
                raise AlfaBrowserError("send_unknown")
            return {"accepted": True, "branch_id": int(branch), "text_length": len(text)}
        except AlfaBrowserError as error:
            if error.code in {
                "form_unavailable", "max_form_missing", "max_recipient_missing",
                "crm_customer_missing", "crm_access_denied", "crm_rate_limited",
                "recipient_missing", "csrf_missing", "crm_rejected",
            }:
                _log_compose_failure(
                    phase,
                    error.code,
                    status=response_status,
                    raw=response_body,
                )
            raise
        except Exception as error:
            if not post_started:
                _log_compose_failure(
                    phase,
                    "browser_exception",
                    status=response_status,
                    raw=response_body,
                    exception=type(error).__name__,
                )
            raise AlfaBrowserError("send_unknown" if post_started else "form_unavailable") from None
        finally:
            self._close(manager, browser, context)
