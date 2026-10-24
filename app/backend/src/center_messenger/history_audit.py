"""Read-only operational coverage report: python -m center_messenger.history_audit."""

import json
import sys

from sqlalchemy import text

from center_messenger.alfa_history import history_coverage
from center_messenger.config import Settings
from center_messenger.database import make_engine


def main():
    engine = make_engine(Settings())
    reports = []
    try:
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            workspaces = db.execute(text("SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars().all()
        for workspace in workspaces:
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
        print(json.dumps({"workspaces": reports}, ensure_ascii=False, indent=2, default=str))
        return 0 if reports and all(r["state"] == "active" and r["history_complete"] and r["totals_verified"]
                                     and not r["error_code"] for r in reports) else 2
    except Exception as error:
        print(f"History audit unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
