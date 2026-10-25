"""Page receipts for the one-time import, kept outside the frequently polled cursor."""
import json
from uuid import uuid4

from sqlalchemy import text

TOPIC = "crm.history.page"


def record_pages(db, workspace, connection, original, cursor, requests, results):
    if cursor.get("archive_paused"):
        return
    for (branch, direction, page), result in zip(requests, results, strict=True):
        key = f"{branch}:{direction}"
        scan = cursor.get(f"scan_id:{key}")
        if not scan or original.get(f"complete:{key}") or result.get("error") or result.get("skipped"):
            continue
        # Catch-up pages belong to the new-message feed, not the initial scan.
        if page != original.get(key, 0):
            continue
        ids = [str(m["record_id"]) for m in result["messages"]]
        payload = {"connection": str(connection), "stream": key, "scan": scan,
                   "page": page, "ids": ids, "rows": result.get("row_count", len(ids)),
                   "total": result.get("total")}
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,:topic,:key,CAST(:p AS jsonb),'published') "
            "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload"),
            {"w": workspace, "id": uuid4(), "topic": TOPIC,
             "key": f"{connection}:{scan}:{key}:{page}", "p": json.dumps(payload)})


def finish_pages(db, workspace, connection, original, cursor):
    for key in cursor["streams"]:
        if not cursor.get(f"finished:{key}") or cursor.get(f"finished:{key}") == original.get(f"finished:{key}"):
            continue
        scan = cursor.get(f"scan_id:{key}")
        pages = list(db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w "
            "AND topic=:topic AND payload->>'connection'=:c AND payload->>'scan'=:scan "
            "AND payload->>'stream'=:stream ORDER BY (payload->>'page')::integer"),
            {"w": workspace, "topic": TOPIC, "c": str(connection), "scan": scan, "stream": key}).scalars())
        ids = [identity for page in pages for identity in page["ids"]]
        totals = {page["total"] for page in pages if page["total"] is not None}
        raw = sum(page["rows"] for page in pages)
        contiguous = [p["page"] for p in pages] == list(range(len(pages)))
        stable = len(totals) == 1 and raw == next(iter(totals), None)
        repeats = len(ids) - len(set(ids))
        status = "verified" if contiguous and stable and not repeats else "source_repeats" if repeats else "limited"
        cursor[f"proof:{key}"] = {"status": status, "pages": len(pages), "observed_rows": raw,
            "distinct_ids": len(set(ids)), "duplicate_rows": repeats, "totals_stable": len(totals) == 1,
            "contiguous": contiguous, "checked_at": cursor[f"finished:{key}"]}


def new_records(db, workspace, connection, branch, records):
    """Filter stored immutable source rows; pending send echoes still need reconciliation."""
    if not records:
        return []
    keys = {}
    for message in records:
        base = f"{message['direction']}:{branch}:{message['record_id']}"
        for key in [base, base + "|recipient|unlinked"] + [base + f"|recipient|{c['branch_id']}:{c['customer_id']}" for c in message.get("customers", []) if c]:
            keys[key] = f"{message['direction']}:{message['record_id']}"
    rows = db.execute(text("SELECT s.external_record_id,m.direction,m.payload->>'crm_record_id' AS record "
        "FROM source_records s JOIN messages m ON m.workspace_id=s.workspace_id AND m.id=s.message_id "
        "WHERE s.workspace_id=:w AND s.connection_id=:c AND s.namespace='alfa_max' "
        "AND s.external_record_id=ANY(CAST(:ids AS text[])) "
        "AND m.payload->>'possible_send_command_id' IS NULL "
        "AND (m.payload->>'superseded_by' IS NULL OR m.payload->>'crm_mirror'='true')"),
        {"w": workspace, "c": connection, "ids": list(keys)}).mappings()
    saved = {keys[r["external_record_id"]] for r in rows
             if keys[r["external_record_id"]] == f"{r['direction']}:{r['record']}"}
    return [m for m in records if f"{m['direction']}:{m['record_id']}" not in saved]
