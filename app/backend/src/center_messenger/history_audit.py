"""Read-only operational coverage report: python -m center_messenger.history_audit."""

import argparse
import json
import math
import sys
from uuid import UUID

from sqlalchemy import text

from center_messenger.alfa_history import history_coverage
from center_messenger.alfa_max_browser import AlfaMaxBrowser
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import make_engine


def compare_source_history(browser, origin, session, branch, direction, saved_ids):
    """Compare actual journal IDs; pagination totals alone cannot identify gaps."""
    seen, duplicates, missing = set(), set(), set()
    page, pages, occurrences, total, size = 0, 1, 0, None, None
    totals = set()
    while page < 100_000:
        requests = [(branch, direction, n) for n in range(page, min(page + 5, pages, 100_000))]
        results, session = browser.read_histories(origin, session, requests)
        for request, result in zip(requests, results, strict=True):
            if result.get("error"):
                raise ValueError("Incomplete source verification")
            if result.get("total") is not None:
                totals.add(result["total"])
            if total is None:
                total = result.get("total")
                size = result.get("row_count") or len(result["messages"])
            for message in result["messages"]:
                identity = str(message["record_id"])
                occurrences += 1
                if identity in seen:
                    duplicates.add(identity)
                seen.add(identity)
                if identity not in saved_ids:
                    missing.add(identity)
            if not result["has_more"]:
                extra = saved_ids - seen
                return {"branch_id": str(branch), "direction": direction, "pages_checked": request[2] + 1,
                        "reported_rows": total, "observed_rows": occurrences, "distinct_source_rows": len(seen),
                        "totals_changed": len(totals) > 1,
                        "duplicate_source_count": len(duplicates), "duplicate_source_ids": sorted(duplicates)[:100],
                        "missing_in_database": len(missing), "missing_ids": sorted(missing)[:100],
                        "saved_not_observed": len(extra), "saved_not_observed_ids": sorted(extra)[:100]}
        page += len(requests)
        pages = max(page + 1, math.ceil(total / size) if total is not None and size else page + 1)
    raise ValueError("Source verification page limit exceeded")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID)
    parser.add_argument("--verify-source", action="store_true", help="Read CRM journal IDs; never modify messages or cursors")
    parser.add_argument("--branch", type=int)
    parser.add_argument("--direction", choices=("receive", "sent"))
    args = parser.parse_args(argv)
    if args.verify_source and (not args.branch or args.branch < 1 or not args.direction):
        parser.error("--verify-source requires --branch and --direction")
    config = Settings()
    engine = make_engine(config)
    reports = []
    try:
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            workspaces = db.execute(text("SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars().all()
        for workspace in workspaces:
            if args.workspace and workspace != args.workspace:
                continue
            with engine.begin() as db:
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {"w": str(workspace)})
                settings = db.execute(text(
                    "SELECT connection_id,crm_connection_id,state,history_cursor,last_sync_at,last_error_code "
                    "FROM alfa_max_settings WHERE workspace_id=:w"
                ), {"w": workspace}).mappings().one()
                branches = db.execute(text(
                    "SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                    "AND connection_id=:c AND available ORDER BY external_id"
                ), {"w": workspace, "c": settings["crm_connection_id"]}).scalars().all()
                coverage = history_coverage(db, workspace, settings["connection_id"], settings["history_cursor"], branches)
                reports.append({"workspace_id": str(workspace), "state": settings["state"],
                                "last_sync_at": settings["last_sync_at"],
                                "error_code": settings["last_error_code"], **coverage})
                if args.verify_source:
                    if args.branch not in branches:
                        raise ValueError("Branch unavailable")
                    source = db.execute(text(
                        "SELECT a.origin,m.session_ciphertext FROM alfa_max_settings m JOIN alfa_settings a "
                        "ON a.workspace_id=m.workspace_id AND a.connection_id=m.crm_connection_id "
                        "WHERE m.workspace_id=:w"
                    ), {"w": workspace}).mappings().one()
                    saved_ids = set(db.execute(text(
                        "SELECT DISTINCT m.payload->>'crm_record_id' FROM messages m JOIN conversations c "
                        "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
                        "WHERE m.workspace_id=:w AND c.connection_id=:c AND m.payload->>'branch_id'=:branch "
                        "AND m.direction=:direction AND m.payload->>'superseded_by' IS NULL "
                        "AND m.payload->>'crm_record_id' IS NOT NULL"
                    ), {"w": workspace, "c": settings["connection_id"], "branch": str(args.branch),
                        "direction": "in" if args.direction == "receive" else "out"}).scalars())
            # Release the DB connection before any browser navigation.
            if args.verify_source:
                session = ConnectorVault(config.connection_master_key_file).open_browser_state(
                    workspace, settings["connection_id"], source["session_ciphertext"])
                reports[-1]["source_verification"] = compare_source_history(
                    AlfaMaxBrowser(), source["origin"], session, args.branch, args.direction, saved_ids)
        print(json.dumps({"workspaces": reports}, ensure_ascii=False, indent=2, default=str))
        return 0 if reports and all(r["state"] == "active" and r["history_complete"] and r["totals_verified"]
                                   and not r["error_code"] and not any(r.get("source_verification", {}).get(field)
                                       for field in ("missing_in_database", "duplicate_source_count", "totals_changed"))
                                   for r in reports) else 2
    except Exception as error:
        print(f"History audit unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
