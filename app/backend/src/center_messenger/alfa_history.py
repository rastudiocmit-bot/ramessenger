"""Durable, independent archive and catch-up cursors for every CRM journal."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from uuid import uuid4

PROJECTION_VERSION = 3
RECHECK_INTERVAL = timedelta(hours=6)
MAX_GAP_RETRIES = 1
MAX_PAGE_FAILURES = 3
LIVE_INTERVAL = 15
RANGE_INTERVAL = 2


def history_pending(cursor):
    if cursor.get("client_repair") and not cursor["client_repair"].get("complete"):
        return True
    if cursor.get("projection_version") != PROJECTION_VERSION or not cursor.get("streams"):
        return True
    return any(
        (not cursor.get("archive_paused") and not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}"))
        or cursor.get(f"catchup:{key}", 0) > 0
        for key in cursor["streams"]
    )


def adopt_history(original, branches):
    """Upgrade the policy without rewinding any imported data or unfinished page."""
    cursor = dict(original)
    cursor["projection_version"] = PROJECTION_VERSION
    cursor["streams"] = [f"{b}:{d}" for b in branches for d in ("receive", "sent")]
    if cursor.get("sync_policy") != 1:
        for key in cursor["streams"]:
            if cursor.get(f"finished:{key}") or cursor.get(f"complete:{key}"):
                cursor[f"imported:{key}"] = True
                cursor[f"complete:{key}"] = True
                cursor[key] = 0
                cursor.setdefault(f"proof:{key}", {"status": "legacy", "reason": "previous_scan"})
                # Old numeric gaps compared raw source rows with distinct IDs.
                if cursor.get(f"error:{key}") in {"history_gap", "history_incomplete"} and not cursor.get(f"missing_ids:{key}"):
                    for prefix in ("error", "gap", "paused", "pause_error", "retries"):
                        cursor.pop(f"{prefix}:{key}", None)
        cursor["sync_policy"] = 1
    return cursor


def retry_history(original, keys=None):
    """Retry the failed page only; never rewind a completed import."""
    cursor = dict(original)
    for key in cursor.get("streams", []) if keys is None else keys:
        for prefix in ("paused", "pause_error", "error", "retries", "failures", "live_error",
                       "retry_live", "retry_archive", "retry_catchup",
                       "due_live", "due_archive", "due_catchup"):
            cursor.pop(f"{prefix}:{key}", None)
        # An explicit retry is immediately eligible while keeping the timestamp
        # of the last successful read available to the freshness monitor.
        cursor[f"due_live:{key}"] = datetime.min.replace(tzinfo=UTC).isoformat()
    return cursor


def reconcile_history(cursor, original, coverage, now=None):
    """Source ambiguity is attention, not a reason to loop over the whole archive."""
    for stream in coverage["streams"]:
        key = f"{stream['branch_id']}:{stream['direction']}"
        if cursor.get(f"finished:{key}"):
            cursor[f"imported:{key}"] = True


def verify_saved_pages(db, workspace, connection, requests, results):
    """Never advance past a read source ID that the projector did not retain."""
    expected = {}
    for (branch, _, _), result in zip(requests, results, strict=True):
        if result.get("error") or result.get("skipped"):
            continue
        for message in result["messages"]:
            base = f"{message['direction']}:{branch}:{message['record_id']}"
            keys = [base, base + "|recipient|unlinked"] + [
                base + f"|recipient|{customer['branch_id']}:{customer['customer_id']}"
                for customer in message.get("customers", []) if customer]
            for key in keys:
                expected[key] = (str(branch), str(message["record_id"]), message["direction"])
    if not expected:
        return
    rows = db.execute(text(
        "SELECT s.external_record_id,m.payload->>'branch_id' AS branch,"
        "m.payload->>'crm_record_id' AS record,m.direction FROM source_records s "
        "JOIN messages m ON m.workspace_id=s.workspace_id AND m.id=s.message_id "
        "WHERE s.workspace_id=:w AND s.connection_id=:c AND s.namespace='alfa_max' "
        "AND s.external_record_id=ANY(CAST(:keys AS text[])) "
        "AND (m.payload->>'superseded_by' IS NULL OR m.payload->>'crm_mirror'='true')"
    ), {"w": workspace, "c": connection, "keys": list(expected)}).mappings().all()
    saved = {expected[row["external_record_id"]] for row in rows
             if expected[row["external_record_id"]] == (row["branch"], row["record"], row["direction"])}
    for (branch, _, _), result in zip(requests, results, strict=True):
        if result.get("error") or result.get("skipped"):
            continue
        missing = [str(m["record_id"]) for m in result["messages"]
                   if (str(branch), str(m["record_id"]), m["direction"]) not in saved]
        if missing:
            result["error"] = "history_projection"
            result["missing_ids"] = missing


def coverage_rows(db, workspace, connection):
    """Read coverage evidence without exposing clients or message contents."""
    rows = db.execute(text(
        "SELECT m.payload->>'branch_id' AS branch,m.direction,"
        "count(DISTINCT m.payload->>'crm_record_id') AS source_rows,"
        "count(*) FILTER(WHERE m.payload->>'crm_record_id' IS NULL AND m.send_command_id IS NULL "
        "AND m.payload->>'source'='alfa_max') AS unidentified_rows,"
        "min(m.occurred_at) AS oldest_at,max(m.occurred_at) AS newest_at "
        "FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id "
        "AND c.id=m.conversation_id WHERE m.workspace_id=:w AND c.connection_id=:c "
        "AND (m.payload->>'superseded_by' IS NULL OR m.payload->>'crm_mirror'='true') GROUP BY 1,2"
    ), {"w": workspace, "c": connection}).mappings().all()
    return [dict(row) for row in rows]


def history_coverage(db, workspace, connection, cursor, branches, *, saved_rows=None):
    cursor = adopt_history(cursor, branches)
    rows = coverage_rows(db, workspace, connection) if saved_rows is None else saved_rows
    saved = {(row["branch"], row["direction"]): row for row in rows}
    streams = []
    for branch in branches:
        for direction in ("receive", "sent"):
            key = f"{branch}:{direction}"
            row = saved.get((str(branch), "in" if direction == "receive" else "out"), {})
            count = row.get("source_rows", 0)
            expected = cursor.get(f"total:{key}")
            missing = len(cursor.get(f"missing_ids:{key}", []))
            complete = bool(cursor.get(f"imported:{key}") or cursor.get(f"complete:{key}"))
            proof = cursor.get(f"proof:{key}", {"status": "pending"})
            streams.append({"branch_id": str(branch), "direction": direction,
                            "source_rows": count, "expected_rows": expected,
                            "unidentified_rows": row.get("unidentified_rows", 0),
                            "missing_rows": missing, "complete": complete,
                            "row_difference": max(0, expected - count) if expected is not None else None,
                            "verification": proof, "verified": proof.get("status") == "verified",
                            "new_messages_at": cursor.get(f"live_checked:{key}"),
                            "live_error_code": cursor.get(f"live_error:{key}"),
                            "live_retry_at": (cursor.get(f"retry_live:{key}") or {}).get("next_at"),
                            "history_retry_at": (cursor.get(f"retry_archive:{key}") or {}).get("next_at"),
                            "catchup_retry_at": (cursor.get(f"retry_catchup:{key}") or {}).get("next_at"),
                            "next_page": cursor.get(key, 0),
                            "catchup_page": cursor.get(f"catchup:{key}", 0),
                            "oldest_at": row.get("oldest_at"), "newest_at": row.get("newest_at"),
                            "last_full_scan_at": cursor.get(f"finished:{key}"),
                            "checked_at": cursor.get(f"checked:{key}"),
                            "checked_page": cursor.get(f"page:{key}"),
                            "scan_pages": cursor.get(f"scan_pages:{key}", 0),
                            "retry": cursor.get(f"retries:{key}", 0),
                            "paused_until": cursor.get(f"paused:{key}"),
                            "missing_ids": cursor.get(f"missing_ids:{key}", []),
                            "error_code": cursor.get(f"error:{key}")})
    return {"streams": streams,
            "history_loaded": bool(streams) and all(s["complete"] for s in streams),
            "history_complete": bool(streams) and all(s["complete"] and s["verified"] and not s["missing_rows"] and not s["unidentified_rows"] for s in streams)
                                and not (cursor.get("client_repair") and not cursor["client_repair"].get("complete")),
            "totals_verified": bool(streams) and all(s["verified"] and not s["missing_rows"] and not s["unidentified_rows"] for s in streams),
            "backfill_pending": history_pending(cursor),
            "archive_paused": bool(cursor.get("archive_paused")),
            "history_paused": bool(cursor.get("archive_paused")) or any(s["paused_until"] for s in streams),
            "unidentified_rows": sum(s["unidentified_rows"] for s in streams),
            "client_repair": cursor.get("client_repair")}


def plan_history(original, branches, now=None):
    now = now or datetime.now(UTC)
    cursor = adopt_history(original, branches)
    heads, catchups, archives = [], [], []
    for branch in branches:
        for direction in ("receive", "sent"):
            key = f"{branch}:{direction}"
            paused = cursor.get(f"paused:{key}")
            if paused and now >= datetime.fromisoformat(paused):
                cursor = retry_history(cursor, [key])
            for kind in ("live", "catchup", "archive"):
                cursor.setdefault(f"due_{kind}:{key}", history_due(cursor, key, kind, now).isoformat())
            if not cursor.get("archive_paused") and not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}"):
                if not cursor.get(f"scan_id:{key}"):
                    cursor[f"scan_id:{key}"] = str(uuid4())
                    cursor[f"receipt_start:{key}"] = cursor.get(key, 0)
                    cursor[f"started:{key}"] = now.isoformat()
                    cursor[f"scan_pages:{key}"] = 0
            if retry_ready(cursor, key, "live", now):
                heads.append((branch, direction, 0))
            page = cursor.get(f"catchup:{key}", 0)
            if page > 0 and retry_ready(cursor, key, "catchup", now):
                catchups.append((branch, direction, page))
            page = cursor.get(key, 0)
            if (page > 0 and not cursor.get("archive_paused") and not cursor.get(f"complete:{key}")
                    and not cursor.get(f"paused:{key}") and retry_ready(cursor, key, "archive", now)):
                archives.append((branch, direction, page))
    # Read every journal's head before spending time on old pages.
    ordered = []
    for kind, group in (("live", heads), ("catchup", catchups), ("archive", archives)):
        ordered.extend(sorted(group, key=lambda r: history_due(cursor, f"{r[0]}:{r[1]}", kind, now)))
    requests = list(dict.fromkeys(ordered))
    return cursor, requests


def history_due(cursor, key, kind, now):
    """Backoff replaces the normal schedule for this stream and operation only."""
    value = (cursor.get(f"retry_{kind}:{key}") or {}).get("next_at") or cursor.get(f"due_{kind}:{key}")
    if value:
        return datetime.fromisoformat(value)
    if kind == "live" and cursor.get(f"live_checked:{key}"):
        return datetime.fromisoformat(cursor[f"live_checked:{key}"]) + timedelta(seconds=LIVE_INTERVAL)
    return now


def history_flow(cursor, request):
    branch, direction, page = request
    return "live" if page == 0 else "catchup" if page == cursor.get(f"catchup:{branch}:{direction}", 0) else "archive"


def ready_history(cursor, now):
    """Read-only scheduling view, shared by the worker and browser coordinator."""
    ready = {}
    for key in cursor.get("streams", []):
        kinds = ["live"]
        if cursor.get(f"catchup:{key}", 0) > 0:
            kinds.append("catchup")
        if (cursor.get(key, 0) > 0 and not cursor.get("archive_paused")
                and not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}")):
            kinds.append("archive")
        for kind in kinds:
            due = history_due(cursor, key, kind, now)
            if due <= now:
                ready[kind] = min(ready.get(kind, due), due)
    return ready


def live_freshness(cursor, now=None):
    now = now or datetime.now(UTC)
    lags, unobserved = [], 0
    for key in cursor.get('streams', []):
        value = cursor.get(f'live_checked:{key}')
        if value:
            lags.append(max(0, int((now - datetime.fromisoformat(value)).total_seconds() - LIVE_INTERVAL)))
        else:
            unobserved += 1
    return {'interval_seconds': LIVE_INTERVAL, 'unobserved_journals': unobserved,
            'delayed_journals': sum(lag >= LIVE_INTERVAL for lag in lags),
            'max_lag_seconds': max(lags, default=0)}


def retry_ready(cursor, key, kind, now):
    return history_due(cursor, key, kind, now) <= now


def _retry_result(cursor, key, kind, result, now):
    token = f"retry_{kind}:{key}"
    if result.get("error"):
        attempt = (cursor.get(token) or {}).get("attempts", 0) + 1
        delay = min(300, 60 * 2 ** min(attempt - 1, 3))
        cursor[token] = {"attempts": attempt, "error": result["error"],
                         "next_at": (now + timedelta(seconds=delay)).isoformat()}
    else:
        cursor.pop(token, None)
        cursor[f"due_{kind}:{key}"] = (now + timedelta(seconds=LIVE_INTERVAL if kind == "live" else RANGE_INTERVAL)).isoformat()


def next_history_delay(cursor, now=None):
    """A failed journal never imposes its backoff on healthy journals."""
    now = now or datetime.now(UTC)
    waits = []
    for key in cursor.get("streams", []):
        kinds = ["live"]
        if cursor.get(f"catchup:{key}", 0):
            kinds.append("catchup")
        if cursor.get(key, 0) > 0 and not cursor.get("archive_paused") and not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}"):
            kinds.append("archive")
        for kind in kinds:
            waits.append(max(0, (history_due(cursor, key, kind, now) - now).total_seconds()))
        paused = cursor.get(f"paused:{key}")
        if paused and not cursor.get("archive_paused"):
            waits.append(max(1, (datetime.fromisoformat(paused) - now).total_seconds()))
    return min(waits, default=15)


def advance_history(cursor, requests, results, now=None, *, allow_archive=True):
    """Advance only successful pages; return all successfully read source rows."""
    now = now or datetime.now(UTC)
    pages = dict(zip(requests, results, strict=True))
    updates = [(branch, result["messages"]) for (branch, _, _), result in pages.items()
               if not result.get("error") and not result.get("skipped")]
    for key in cursor["streams"]:
        branch, direction = key.split(":")
        branch = int(branch)
        received = {page: result for (b, d, page), result in pages.items()
                    if b == branch and d == direction and not result.get("skipped")}
        if not received:
            continue
        current = received.get(0)
        target = cursor.get(key, 0)
        archive = received.get(target) if allow_archive and not cursor.get("archive_paused") else None
        failed = [result["error"] for (b, d, _), result in pages.items()
                  if b == branch and d == direction and result.get("error")]
        if current is not None:
            _retry_result(cursor, key, "live", current, now)
            if current.get("error"):
                cursor[f"live_error:{key}"] = current["error"]
            else:
                cursor.pop(f"live_error:{key}", None)
                cursor[f"live_checked:{key}"] = now.isoformat()
        if archive is not None and not cursor.get(f"complete:{key}"):
            _retry_result(cursor, key, "archive", archive, now)
        if failed and not cursor.get(f"complete:{key}"):
            cursor[f"error:{key}"] = failed[0]
            missing = [m for (b, d, _), result in pages.items() if b == branch and d == direction
                       for m in result.get("missing_ids", [])]
            if missing:
                cursor[f"missing_ids:{key}"] = missing[:20]
            if archive and archive.get("error") in {"invalid_response", "history_projection"} and not cursor.get(f"paused:{key}"):
                cursor[f"failures:{key}"] = cursor.get(f"failures:{key}", 0) + 1
                if cursor[f"failures:{key}"] >= MAX_PAGE_FAILURES:
                    cursor[f"paused:{key}"] = (now + RECHECK_INTERVAL).isoformat()
                    cursor[f"pause_error:{key}"] = archive["error"]
        elif cursor.get(f"paused:{key}"):
            cursor[f"error:{key}"] = cursor.get(f"pause_error:{key}", "history_gap")
        elif not cursor.get(f"gap:{key}") and archive is not None:
            cursor.pop(f"error:{key}", None)
            if not cursor.get(f"complete:{key}") and archive and not archive.get("error"):
                cursor.pop(f"missing_ids:{key}", None)
        elif cursor.get(f"gap:{key}"):
            cursor[f"error:{key}"] = "history_incomplete"
        # An unrelated journal or failed old page cannot discard a healthy head.
        # New bursts have a separate cursor and never reset the archive.
        catchup = cursor.get(f"catchup:{key}", 0)
        if current is not None and not current.get("error"):
            identities = [str(m["record_id"]) for m in current["messages"]]
            previous_head = cursor.get(f"head:{key}")
            if previous_head and current["has_more"] and previous_head not in identities and not current.get("known_ids"):
                if catchup:
                    cursor.setdefault(f"queued:{key}", previous_head)
                else:
                    cursor[f"catchup:{key}"] = 1
                    cursor[f"until:{key}"] = previous_head
            if identities:
                cursor[f"head:{key}"] = identities[0]
            if current.get("total") is not None:
                cursor[f"total:{key}"] = current["total"]
        if catchup:
            result = received.get(catchup)
            if result is not None:
                _retry_result(cursor, key, "catchup", result, now)
            if result is not None and not result.get("error"):
                identities = {str(m["record_id"]) for m in result["messages"]}
                if not result["has_more"] or cursor.get(f"until:{key}") in identities or result.get("known_ids"):
                    queued = cursor.pop(f"queued:{key}", None)
                    cursor[f"catchup:{key}"] = 1 if queued else 0
                    if queued:
                        cursor[f"until:{key}"] = queued
                    else:
                        cursor.pop(f"until:{key}", None)
                else:
                    cursor[f"catchup:{key}"] = catchup + 1
        if archive and not archive.get("error") and not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}"):
            cursor.pop(f"failures:{key}", None)
            cursor[f"page:{key}"] = target
            cursor[f"checked:{key}"] = now.isoformat()
            cursor[f"scan_pages:{key}"] = cursor.get(f"scan_pages:{key}", 0) + 1
            cursor[key] = target + 1 if archive["has_more"] else 0
            cursor[f"complete:{key}"] = not archive["has_more"]
            if not archive["has_more"]:
                cursor[f"finished:{key}"] = now.isoformat()
                cursor[f"imported:{key}"] = True
    return updates
