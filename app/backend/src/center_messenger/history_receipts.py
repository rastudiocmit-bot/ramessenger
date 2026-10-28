"""Page receipts for the one-time import, kept outside the frequently polled cursor."""
import json
from uuid import uuid4

from sqlalchemy import text

TOPIC = "crm.history.page"


def customer_ids(record):
    return sorted({int(c['customer_id']) for c in record.get('customers', []) if c})


def observed_customers(record):
    return sorted({(int(c['branch_id']), int(c['customer_id'])) for c in record.get('customers', []) if c})


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
    """Keep stored bodies, but refresh changed customer/peer metadata and send echoes."""
    if not records:
        return []
    keys = {}
    for message in records:
        base = f"{message['direction']}:{branch}:{message['record_id']}"
        for key in [base, base + "|recipient|unlinked"] + [base + f"|recipient|{c['branch_id']}:{c['customer_id']}" for c in message.get("customers", []) if c]:
            keys[key] = f"{message['direction']}:{message['record_id']}"
    rows = db.execute(text("SELECT s.external_record_id,m.direction,m.payload->>'crm_record_id' AS record,"
        "m.payload->>'max_user_id' AS max_user_id,m.payload->'crm_customer_ids' AS customer_ids,"
        "m.payload->>'customer_id' AS customer_id,m.payload->'crm_observed_customers' AS observed_customers,"
        "m.payload->>'crm_recipient_stale' AS recipient_stale "
        "FROM source_records s JOIN messages m ON m.workspace_id=s.workspace_id AND m.id=s.message_id "
        "WHERE s.workspace_id=:w AND s.connection_id=:c AND s.namespace='alfa_max' "
        "AND s.external_record_id=ANY(CAST(:ids AS text[])) "
        "AND m.payload->>'possible_send_command_id' IS NULL "
        "AND (m.payload->>'superseded_by' IS NULL OR m.payload->>'crm_mirror'='true')"),
        {"w": workspace, "c": connection, "ids": list(keys)}).mappings()
    incoming = {f"{m['direction']}:{m['record_id']}": m for m in records}
    matched = {}
    for row in rows:
        identity = keys[row["external_record_id"]]
        if identity == f"{row['direction']}:{row['record']}":
            matched.setdefault(identity, []).append(row)
    saved = set()
    for identity, saved_rows in matched.items():
        record = incoming[identity]
        peer = record.get("max_user_id")
        # Bodies are immutable after import; CRM's customer association is not.
        # The journal may relink the SAME MAX row after an operator adds a
        # parent's phone to the active pupil card. Check metadata on pages we
        # already poll, without rescanning history or comparing old text.
        if record['direction'] == 'in':
            wanted = customer_ids(record)
            observed = [list(pair) for pair in observed_customers(record)]
            hints = [row['observed_customers'] for row in saved_rows if row['observed_customers'] is not None]
            # crm_customer_ids belongs to one projected family. The observed
            # signature belongs to the WHOLE source row, including other
            # recipients, so legitimate fan-out does not reproject every poll.
            legacy_ids = sorted({int(value) for row in saved_rows for value in
                (row['customer_ids'] or ([row['customer_id']] if row['customer_id'] else []))})
            changed = (any(hint != observed for hint in hints) if hints
                       else legacy_ids != wanted or len(wanted) > 1)
            # A source signature and its chosen reply address can disagree in
            # legacy/partially repaired projections. Matching the signature
            # alone must not keep an address outside this source/family alive.
            for row in saved_rows:
                selected = int(row['customer_id']) if row['customer_id'] else None
                projected = row['customer_ids'] or ([selected] if selected else [])
                if ((selected is not None and (selected not in wanted or selected not in projected))
                        or (selected is None and wanted)):
                    changed = True
            changed = changed or any(row['recipient_stale'] == 'true' for row in saved_rows)
            if changed:
                continue
        # A later read may add explicit peer evidence to an already imported row.
        # Reproject only for that new evidence; an attribute-less later read must
        # not erase a peer which was proved previously.
        if not peer or all(row["max_user_id"] == peer for row in saved_rows):
            saved.add(identity)
    return [dict(m, source_metadata_refresh=True) if f"{m['direction']}:{m['record_id']}" in matched else m
            for m in records if f"{m['direction']}:{m['record_id']}" not in saved]
