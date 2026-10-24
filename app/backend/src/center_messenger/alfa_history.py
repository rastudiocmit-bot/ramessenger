"""Durable, independent archive and catch-up cursors for every CRM journal."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

PROJECTION_VERSION = 3
RECHECK_INTERVAL = timedelta(hours=6)


def history_pending(cursor):
    if cursor.get("projection_version") != PROJECTION_VERSION or not cursor.get("streams"):
        return True
    return any(
        not cursor.get(f"complete:{key}") or cursor.get(f"catchup:{key}", 0) > 0
        or cursor.get(f"error:{key}")
        for key in cursor["streams"]
    )


def history_coverage(db, workspace, connection, cursor, branches):
    """Read coverage evidence without exposing clients or message contents."""
    rows = db.execute(text(
        "SELECT m.payload->>'branch_id' AS branch,m.direction,"
        "count(DISTINCT m.payload->>'crm_record_id') AS source_rows,"
        "min(m.occurred_at) AS oldest_at,max(m.occurred_at) AS newest_at "
        "FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id "
        "AND c.id=m.conversation_id WHERE m.workspace_id=:w AND c.connection_id=:c "
        "AND m.payload->>'crm_record_id' IS NOT NULL "
        "AND m.payload->>'superseded_by' IS NULL GROUP BY 1,2"
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
                            "missing_rows": missing, "complete": complete,
                            "next_page": cursor.get(key, 0),
                            "catchup_page": cursor.get(f"catchup:{key}", 0),
                            "oldest_at": row.get("oldest_at"), "newest_at": row.get("newest_at"),
                            "last_full_scan_at": cursor.get(f"finished:{key}"),
                            "error_code": cursor.get(f"error:{key}")})
    return {"streams": streams,
            "history_complete": bool(streams) and all(s["complete"] and not s["missing_rows"] for s in streams),
            "totals_verified": bool(streams) and all(s["expected_rows"] is not None and not s["missing_rows"] for s in streams),
            "backfill_pending": history_pending(cursor) or any(not s["complete"] for s in streams)}


def plan_history(original, branches, now=None):
    now = now or datetime.now(UTC)
    cursor = dict(original) if original.get("projection_version") == PROJECTION_VERSION else {}
    cursor["projection_version"] = PROJECTION_VERSION
    cursor["streams"] = [f"{branch}:{direction}" for branch in branches for direction in ("receive", "sent")]
    requests = []
    for branch in branches:
        for direction in ("receive", "sent"):
            key = f"{branch}:{direction}"
            finished = cursor.get(f"finished:{key}")
            if finished and cursor.get(f"complete:{key}"):
                if now - datetime.fromisoformat(finished) >= RECHECK_INTERVAL:
                    cursor[key] = 0
                    cursor[f"complete:{key}"] = False
            if not cursor.get(f"complete:{key}") and cursor.get(key, 0) == 0:
                cursor[f"started:{key}"] = now.isoformat()
            requests.append((branch, direction, 0))
            for page in (cursor.get(key, 0), cursor.get(f"catchup:{key}", 0)):
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
        else:
            cursor.pop(f"error:{key}", None)
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
        if archive and not archive.get("error") and not cursor.get(f"complete:{key}"):
            cursor[key] = target + 1 if archive["has_more"] else 0
            cursor[f"complete:{key}"] = not archive["has_more"]
            if not archive["has_more"]:
                cursor[f"finished:{key}"] = now.isoformat()
    return updates
