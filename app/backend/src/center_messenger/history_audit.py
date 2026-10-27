"""Read-only operational coverage report: python -m center_messenger.history_audit."""

import argparse
import json
import math
import sys
from uuid import UUID

from sqlalchemy import text

from center_messenger.alfa_history import history_coverage
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser, NETWORK_ERRORS, HISTORY_READ_ATTEMPTS
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import make_engine


BROWSER_ERRORS = {
    "browser_unavailable": "Не удалось запустить серверный браузер или он завершил работу.",
    "crm_unavailable": "Не удалось прочитать страницу журнала AlfaCRM.",
    "reauth_required": "Сессия AlfaCRM завершилась. Восстановите вход MAX через AlfaCRM в подключениях.",
    "invalid_response": "AlfaCRM вернула неполную страницу или журнал неизвестного формата.",
    "unsafe_address": "Адрес AlfaCRM не прошёл проверку безопасности.",
    "network": "Не удалось установить соединение с AlfaCRM.",
    "browser_busy": "Браузер занят синхронизацией или восстановлением. Повторите проверку позже.",
    "redirect_blocked": "AlfaCRM перенаправила браузер на другой домен.",
}
STAGES = {"open_browser", "start_driver", "launch_browser", "open_context", "open_page",
          "navigation", "check_session", "parse_page", "save_session", "read_page"}
CAUSES = {"timeout", "browser_closed", "browser_error"}


def source_failure(error, branch, direction, page, checked, observed):
    # Error messages and Playwright traces can contain private page data. Only
    # approved codes and bounded context are safe to include in the report.
    code = error.code if error.code in BROWSER_ERRORS else "internal"
    failed_page = error.request[2] if error.request else page
    status = error.http_status
    return {"status": "failed", "branch_id": str(branch), "direction": direction,
            "error_code": code, "error": BROWSER_ERRORS.get(code, "Проверку журнала не удалось завершить."),
            "stage": error.stage if error.stage in STAGES else None,
            "cause": error.cause if error.cause in CAUSES else None,
            "http_status": status if isinstance(status, int) and 100 <= status <= 599 else None,
            "failed_page": failed_page + 1, "pages_checked": checked, "observed_rows": observed,
            "network_error": error.network_error if error.network_error in NETWORK_ERRORS else None,
            "attempts": error.attempts if isinstance(error.attempts, int) and 1 <= error.attempts <= HISTORY_READ_ATTEMPTS else None}


def audit_exit_code(reports):
    if any(r.get("source_verification", {}).get("status") == "failed" for r in reports):
        return 1
    return 0 if reports and all(r["state"] == "active" and r["history_complete"] and r["totals_verified"]
        and not r["error_code"] and not any(r.get("source_verification", {}).get(field)
            for field in ("missing_in_database", "duplicate_source_count", "totals_changed"))
        for r in reports) else 2


def compare_source_history(browser, origin, session, branch, direction, saved_ids, *, on_progress=None):
    """Compare actual journal IDs; pagination totals alone cannot identify gaps."""
    seen, duplicates, missing = set(), set(), set()
    page, pages, occurrences, total, size = 0, 1, 0, None, None
    totals, checked = set(), 0
    while page < 100_000:
        requests = [(branch, direction, n) for n in range(page, min(page + 5, pages, 100_000))]
        if on_progress:
            on_progress({"page": page, "pages_checked": checked})
        try:
            results, session = browser.read_histories(origin, session, requests, continue_on_error=True)
        except AlfaBrowserError as error:
            return source_failure(error, branch, direction, page, checked, occurrences)
        for request, result in zip(requests, results, strict=True):
            if result.get("error"):
                return source_failure(AlfaBrowserError(result["error"], stage=result.get("stage", "read_page"), request=request,
                    cause=result.get("cause"), http_status=result.get("http_status"),
                    network_error=result.get("network_error"), attempts=result.get("attempts", 1)),
                                      branch, direction, request[2], checked, occurrences)
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
            checked += 1
            if not result["has_more"]:
                extra = saved_ids - seen
                return {"status": "complete", "branch_id": str(branch), "direction": direction, "pages_checked": request[2] + 1,
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
    parser.add_argument("--progress", action="store_true", help="Print source page progress to stderr; stdout remains JSON")
    parser.add_argument("--branch", type=int)
    parser.add_argument("--direction", choices=("receive", "sent"))
    args = parser.parse_args(argv)
    if args.verify_source and (not args.branch or args.branch < 1 or not args.direction):
        parser.error("--verify-source requires --branch and --direction")
    engine, reports = None, []
    try:
        config = Settings()
        engine = make_engine(config)
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
                    "SELECT am.connection_id,am.crm_connection_id,am.state,am.history_cursor,am.last_sync_at,am.last_error_code,"
                    "cn.capabilities->'chat_queue_projection' AS chat_queue_projection "
                    "FROM alfa_max_settings am JOIN connections cn ON cn.workspace_id=am.workspace_id "
                    "AND cn.id=am.connection_id WHERE am.workspace_id=:w"
                ), {"w": workspace}).mappings().one()
                branches = db.execute(text(
                    "SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                    "AND connection_id=:c AND available ORDER BY external_id"
                ), {"w": workspace, "c": settings["crm_connection_id"]}).scalars().all()
                coverage = history_coverage(db, workspace, settings["connection_id"], settings["history_cursor"], branches)
                from center_messenger import catalog_review, active_review
                reports.append({"workspace_id": str(workspace), "state": settings["state"],
                                "last_sync_at": settings["last_sync_at"],
                                "error_code": settings["last_error_code"],
                                "chat_queue_projection": settings["chat_queue_projection"],
                                "catalog_review": catalog_review.status(db, workspace, settings["crm_connection_id"]),
                                "active_review": active_review.status(db, workspace, settings["crm_connection_id"]), **coverage})
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
                        "AND m.direction=:direction AND (m.payload->>'superseded_by' IS NULL "
                        "OR m.payload->>'crm_mirror'='true') "
                        "AND m.payload->>'crm_record_id' IS NOT NULL"
                    ), {"w": workspace, "c": settings["connection_id"], "branch": str(args.branch),
                        "direction": "in" if args.direction == "receive" else "out"}).scalars())
            # Release the DB connection before any browser navigation.
            if args.verify_source:
                session = ConnectorVault(config.connection_master_key_file).open_browser_state(
                    workspace, settings["connection_id"], source["session_ciphertext"])
                def progress(value):
                    print(f"History source: branch={args.branch} direction={args.direction} "
                          f"page={value['page'] + 1} checked={value['pages_checked']}", file=sys.stderr, flush=True)
                from center_messenger.accounts import Accounts
                from center_messenger.alfa_service import AlfaConnections
                from center_messenger.alfa_max import AlfaMaxConnections
                from center_messenger.alfa_session import CoordinatedHistoryReader
                native = AlfaMaxConnections(AlfaConnections(Accounts(engine, config), ConnectorVault(config.connection_master_key_file)))
                verification = compare_source_history(
                    CoordinatedHistoryReader(native, workspace, settings['connection_id']),
                    source["origin"], session, args.branch, args.direction, saved_ids,
                    on_progress=progress if args.progress else None)
                reports[-1]["source_verification"] = verification
                if verification["status"] == "failed":
                    print(f"Source verification failed: code={verification['error_code']} "
                          f"stage={verification['stage']} page={verification['failed_page']} "
                          f"cause={verification['cause']} http_status={verification['http_status']} "
                          f"network_error={verification['network_error']} attempts={verification['attempts']}. "
                          + verification["error"], file=sys.stderr, flush=True)
        print(json.dumps({"workspaces": reports}, ensure_ascii=False, indent=2, default=str))
        return audit_exit_code(reports)
    except Exception as error:
        print(f"History audit unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
