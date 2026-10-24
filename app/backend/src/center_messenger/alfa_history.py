"""Durable, independent archive and catch-up cursors for every CRM journal."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

PROJECTION_VERSION = 3
RECHECK_INTERVAL = timedelta(hours=6)
MAX_GAP_RETRIES = 1
MAX_PAGE_FAILURES = 3


def history_pending(cursor):
    if cursor.get("client_repair") and not cursor["client_repair"].get("complete"):
        return True
    if cursor.get("projection_version") != PROJECTION_VERSION or not cursor.get("streams"):
        return True
    return any(
        (not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}"))
        or cursor.get(f"catchup:{key}", 0) > 0
        for key in cursor["streams"]
    )


def retry_history(original, keys=None):
    """A manual retry resumes paused archives without rewinding healthy ones."""
    cursor = dict(original)
    for key in cursor.get("streams", []) if keys is None else keys:
        if cursor.get(f"paused:{key}"):
            cursor[key] = 0
            cursor[f"complete:{key}"] = False
            for prefix in ("paused", "pause_error", "error", "retries", "gap", "scan_pages", "failures", "missing_ids"):
                cursor.pop(f"{prefix}:{key}", None)
    return cursor


def reconcile_history(cursor, original, coverage, now=None):
    """Bound verification passes and retain an explicit, durable gap state."""
    now = now or datetime.now(UTC)
    for stream in coverage["streams"]:
        key = f"{stream['branch_id']}:{stream['direction']}"
        if not cursor.get(f"finished:{key}") or cursor[f"finished:{key}"] == original.get(f"finished:{key}"):
            continue
        if not stream["missing_rows"]:
            for prefix in ("paused", "gap", "retries"):
                cursor.pop(f"{prefix}:{key}", None)
            if cursor.get(f"error:{key}") in {"history_incomplete", "history_gap"}:
                cursor.pop(f"error:{key}", None)
            continue
        cursor[key] = 0
        cursor[f"complete:{key}"] = False
        cursor[f"gap:{key}"] = stream["missing_rows"]
        retries = cursor.get(f"retries:{key}", 0)
        if retries < MAX_GAP_RETRIES:
            cursor[f"retries:{key}"] = retries + 1
            cursor[f"error:{key}"] = "history_incomplete"
        else:
            cursor[f"paused:{key}"] = (now + RECHECK_INTERVAL).isoformat()
            cursor[f"pause_error:{key}"] = "history_gap"
            cursor[f"error:{key}"] = "history_gap"


def verify_saved_pages(db, workspace, connection, requests, results):
    """Never advance past a read source ID that the projector did not retain."""
    expected = {}
    for (branch, _, _), result in zip(requests, results, strict=True):
        if result.get("error"):
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
        if result.get("error"):
            continue
        missing = [str(m["record_id"]) for m in result["messages"]
                   if (str(branch), str(m["record_id"]), m["direction"]) not in saved]
        if missing:
            result["error"] = "history_projection"
            result["missing_ids"] = missing


def history_coverage(db, workspace, connection, cursor, branches):
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
    saved = {(row["branch"], row["direction"]): row for row in rows}
    streams = []
    for branch in branches:
        for direction in ("receive", "sent"):
            key = f"{branch}:{direction}"
            row = saved.get((str(branch), "in" if direction == "receive" else "out"), {})
            count = row.get("source_rows", 0)
            expected = cursor.get(f"total:{key}")
            missing = max(0, expected - count) if expected is not None else None
            complete = (cursor.get("projection_version") == PROJECTION_VERSION
                        and bool(cursor.get(f"complete:{key}"))
                        and not cursor.get(f"catchup:{key}") and not cursor.get(f"error:{key}"))
            streams.append({"branch_id": str(branch), "direction": direction,
                            "source_rows": count, "expected_rows": expected,
                            "unidentified_rows": row.get("unidentified_rows", 0),
                            "missing_rows": missing, "complete": complete,
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
            "history_complete": bool(streams) and all(s["complete"] and not s["missing_rows"] and not s["unidentified_rows"] for s in streams)
                                and not (cursor.get("client_repair") and not cursor["client_repair"].get("complete")),
            "totals_verified": bool(streams) and all(s["expected_rows"] is not None and not s["missing_rows"]
                                                     and not s["unidentified_rows"] for s in streams),
            "backfill_pending": history_pending(cursor),
            "history_paused": any(s["paused_until"] or s["unidentified_rows"] for s in streams),
            "unidentified_rows": sum(s["unidentified_rows"] for s in streams),
            "client_repair": cursor.get("client_repair")}


def plan_history(original, branches, now=None):
    now = now or datetime.now(UTC)
    cursor = dict(original) if original.get("projection_version") == PROJECTION_VERSION else {
        key: original[key] for key in ("client_repair",) if key in original}
    cursor["projection_version"] = PROJECTION_VERSION
    cursor["streams"] = [f"{branch}:{direction}" for branch in branches for direction in ("receive", "sent")]
    requests = []
    for branch in branches:
        for direction in ("receive", "sent"):
            key = f"{branch}:{direction}"
            paused = cursor.get(f"paused:{key}")
            if paused and now >= datetime.fromisoformat(paused):
                cursor = retry_history(cursor, [key])
            finished = cursor.get(f"finished:{key}")
            if finished and cursor.get(f"complete:{key}"):
                if now - datetime.fromisoformat(finished) >= RECHECK_INTERVAL:
                    cursor[key] = 0
                    cursor[f"complete:{key}"] = False
                    cursor.pop(f"retries:{key}", None)
            if not cursor.get(f"complete:{key}") and not cursor.get(f"paused:{key}") and cursor.get(key, 0) == 0:
                cursor[f"started:{key}"] = now.isoformat()
                cursor[f"scan_pages:{key}"] = 0
            requests.append((branch, direction, 0))
            for page in (0 if cursor.get(f"paused:{key}") else cursor.get(key, 0), cursor.get(f"catchup:{key}", 0)):
                request = (branch, direction, page)
                if page > 0 and request not in requests:
                    requests.append(request)
    return cursor, requests


def advance_history(cursor, requests, results, now=None):
    """Advance only successful pages; return all successfully read source rows."""
    now = now or datetime.now(UTC)
    pages = dict(zip(requests, results, strict=True))
    updates = [(branch, result["messages"]) for (branch, _, _), result in pages.items()
               if not result.get("error")]
    for key in cursor["streams"]:
        branch, direction = key.split(":")
        branch = int(branch)
        current = pages[(branch, direction, 0)]
        target = cursor.get(key, 0)
        archive = pages.get((branch, direction, target))
        failed = [result["error"] for (b, d, _), result in pages.items()
                  if b == branch and d == direction and result.get("error")]
        if failed:
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
        elif not cursor.get(f"gap:{key}"):
            cursor.pop(f"error:{key}", None)
            cursor.pop(f"missing_ids:{key}", None)
        else:
            cursor[f"error:{key}"] = "history_incomplete"
        # An unrelated journal or failed old page cannot discard a healthy head.
        # New bursts have a separate cursor and never reset the archive.
        catchup = cursor.get(f"catchup:{key}", 0)
        if not current.get("error"):
            identities = [str(m["record_id"]) for m in current["messages"]]
            previous_head = cursor.get(f"head:{key}")
            if previous_head and current["has_more"] and previous_head not in identities:
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
            result = pages.get((branch, direction, catchup))
            if result and not result.get("error"):
                identities = {str(m["record_id"]) for m in result["messages"]}
                if not result["has_more"] or cursor.get(f"until:{key}") in identities:
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
    return updates
