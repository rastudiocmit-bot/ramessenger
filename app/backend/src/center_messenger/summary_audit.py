"""Bounded, read-only Summary diagnostics; never contacts CRM or opens a browser.

Default: runtime database settings and aggregate connection/lock counters.
--calculate: execute the real Summary calculation once per selected workspace,
reporting stage timings and safe error codes, without names/message contents.
This is not a load test, and the CLI pool is separate from the running API pool.
"""

import argparse
import json
from time import perf_counter
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from center_messenger.cabinet_summary import calculate
from center_messenger.config import Settings
from center_messenger.database import make_engine


def failure(error):
    return {"exception": type(error).__name__,
            "sqlstate": getattr(getattr(error, "orig", None), "sqlstate", None)}


def inspect(engine, *, workspace=None, run_calculation=False):
    report = {"read_only": True, "crm_requests": 0, "workspaces": [],
              "pool_scope": "diagnostic_process_only"}
    with engine.begin() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        report["database"] = dict(db.execute(text(
            "SELECT current_setting('statement_timeout') AS statement_timeout,"
            "current_setting('lock_timeout') AS lock_timeout,current_setting('jit') AS jit,"
            "current_setting('server_version') AS version,rolsuper AS superuser,rolbypassrls AS bypass_rls "
            "FROM pg_roles WHERE rolname=current_user")).mappings().one())
        report["activity"] = dict(db.execute(text(
            "SELECT count(*) AS connections,count(*) FILTER(WHERE state='active') AS active,"
            "count(*) FILTER(WHERE wait_event_type='Lock') AS waiting_for_lock "
            "FROM pg_stat_activity WHERE datname=current_database()")).mappings().one())
        scopes = [workspace] if workspace else list(db.execute(text(
            "SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars())
    if not run_calculation:
        return report
    for scope in scopes:
        item = {"workspace_id": str(scope), "stages": [], "status": "ok"}
        started = perf_counter()
        try:
            with engine.begin() as db:
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {"w": str(scope)})
                zone = db.execute(text("SELECT timezone FROM workspaces WHERE id=:w"), {"w": scope}).scalar_one()
                value = calculate(db, scope, zone, diagnostics=item["stages"])
                item.update(computed_at=value["computed_at"].isoformat(),
                            period_start=value["period_start"].isoformat())
        except SQLAlchemyError as error:
            item.update(status="failed", **failure(error))
        item["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        report["workspaces"].append(item)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID)
    parser.add_argument("--calculate", action="store_true")
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = make_engine(Settings())
        result = inspect(engine, workspace=args.workspace, run_calculation=args.calculate)
    except SQLAlchemyError as error:
        result = {"status": "failed", **failure(error)}
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get("status") == "failed" or any(
        item["status"] == "failed" for item in result.get("workspaces", [])))


if __name__ == "__main__":
    raise SystemExit(main())
