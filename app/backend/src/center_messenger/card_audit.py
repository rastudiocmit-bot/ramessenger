"""Read-only CRM card diagnostics; never contacts AlfaCRM or opens a browser.

Usage:
  python -m center_messenger.card_audit
  python -m center_messenger.card_audit --workspace UUID --conversation UUID
"""

import argparse
import json
from uuid import UUID

from sqlalchemy import text

from center_messenger import card_preference, family_cache
from center_messenger.config import Settings
from center_messenger.crm_api import DirectoryReader
from center_messenger.database import make_engine


def _jobs(db, workspace, connection, version, *, contacts=None):
    params = {"w": workspace, "conn": connection, "version": version,
              "topics": list(family_cache.TOPICS), "contacts": list(map(str, contacts or []))}
    return [dict(row) for row in db.execute(text("""
      WITH ranked AS (
        SELECT id,row_number() OVER (ORDER BY
          coalesce((payload->>'manual')::boolean,false) DESC,
          CASE WHEN coalesce((payload->>'manual')::boolean,false)
            THEN coalesce((payload->>'requested_at')::timestamptz,created_at) END DESC NULLS LAST,
          coalesce((payload->>'served_at')::timestamptz,created_at),id) AS position
        FROM outbox_events WHERE workspace_id=:w AND topic=ANY(CAST(:topics AS text[]))
          AND state='pending' AND payload->>'connection'=CAST(:conn AS text)
          AND payload->>'version'=CAST(:version AS text)
      )
      SELECT e.topic,e.event_key,ranked.position,
        e.payload->>'branch' AS branch_id,e.payload->>'customer' AS customer_id,
        e.payload->>'contact' AS contact_id,e.payload->>'student' AS student_id,
        coalesce((e.payload->>'manual')::boolean,false) AS manual,
        e.payload->>'requested_at' AS requested_at,e.payload->>'started_at' AS started_at,
        e.payload->>'lease_until' AS lease_until,e.payload->>'available_at' AS retry_at,
        e.payload->>'stage' AS stage,(e.payload->>'page')::integer AS page,
        coalesce((e.payload->'checkpoint'->>'pages')::integer,0) AS checkpoint_pages,
        coalesce((e.payload->>'attempts')::integer,0) AS attempts,
        e.payload->>'cause' AS cause,e.payload->>'error' AS error_code,
        coalesce((e.payload->>'paused')::boolean,false) AS paused,
        e.payload->>'version' AS directory_version
      FROM outbox_events e JOIN ranked ON ranked.id=e.id
      WHERE e.workspace_id=:w AND e.topic=ANY(CAST(:topics AS text[]))
        AND e.state='pending' AND e.payload->>'connection'=CAST(:conn AS text)
        AND e.payload->>'version'=CAST(:version AS text)
        AND (cardinality(CAST(:contacts AS text[]))=0 OR e.payload->>'contact'=ANY(CAST(:contacts AS text[])))
      ORDER BY ranked.position,e.topic
    """), params).mappings()]


def _facts(db, workspace, connection, version, run, *, contacts=None):
    return [dict(row) for row in db.execute(text("""
      SELECT c.contact_id,c.student_id,c.branch_id,c.external_id AS customer_id,c.status,
        c.name AS student_name,c.legal_name AS parent_name,
        f.refreshed_at AS finance_updated_at,f.lessons_refreshed_at AS lessons_updated_at,
        f.lessons_full_refreshed_at AS history_updated_at,
        g.payload->>'refreshed_at' AS groups_updated_at
      FROM alfa_customers c
      LEFT JOIN alfa_family_details f ON f.workspace_id=c.workspace_id AND f.connection_id=:conn
        AND f.branch_id=c.branch_id AND f.customer_id=c.external_id AND f.settings_version=:version
      LEFT JOIN outbox_events g ON g.workspace_id=c.workspace_id AND g.topic=:groups
        AND g.event_key=concat(CAST(:conn AS text),':',c.branch_id,':',c.external_id)
        AND g.payload->>'version'=CAST(:version AS text)
      WHERE c.workspace_id=:w AND c.run_id=:run
        AND (cardinality(CAST(:contacts AS uuid[]))=0 OR c.contact_id=ANY(CAST(:contacts AS uuid[])))
      ORDER BY c.contact_id,c.student_id,c.branch_id,c.external_id
    """), {"w": workspace, "conn": connection, "version": version, "run": run,
             "groups": family_cache.GROUPS, "contacts": list(contacts or [])}).mappings()]


def inspect(engine, *, workspace=None, conversation=None):
    result = {"read_only": True, "crm_requests": 0, "workspaces": []}
    with engine.begin() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        scopes = [workspace] if workspace else list(db.execute(text(
            "SELECT workspace_id FROM alfa_settings WHERE secret_ciphertext IS NOT NULL ORDER BY workspace_id"
        )).scalars())
    for scope in scopes:
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {"w": str(scope)})
            snapshot = DirectoryReader._published(db, scope)
            if not snapshot:
                result["workspaces"].append({"workspace_id": str(scope), "configured": False})
                continue
            item = {"workspace_id": str(scope), "configured": True,
                    "connection_id": str(snapshot["connection_id"]),
                    "directory_run": str(snapshot["run"]), "directory_version": snapshot["version"]}
            contacts = []
            if conversation:
                source = db.execute(text("SELECT contact_id FROM conversations WHERE workspace_id=:w "
                    "AND id=:conversation AND kind='direct'"),
                    {"w": scope, "conversation": conversation}).scalar_one_or_none()
                if source:
                    selection = card_preference.selected(db, scope, snapshot, conversation, source)
                    contacts = [UUID(value) for value in selection["contacts"]] if selection else [source]
                    item["conversation_id"] = str(conversation)
                    item["source_contact_id"] = str(source)
                    item["selection"] = card_preference.public(selection)
                else:
                    item["conversation_id"] = str(conversation)
                    item["error_code"] = "conversation_not_found"
            item["facts"] = _facts(db, scope, snapshot["connection_id"], snapshot["version"],
                                   snapshot["run"], contacts=contacts)
            item["jobs"] = _jobs(db, scope, snapshot["connection_id"], snapshot["version"], contacts=contacts)
            item["current"] = family_cache.current(db, scope, snapshot["connection_id"])
            item["queue"] = family_cache.progress(db, scope, snapshot["connection_id"], snapshot)
            result["workspaces"].append(item)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID)
    parser.add_argument("--conversation", type=UUID)
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = make_engine(Settings())
        report = inspect(engine, workspace=args.workspace, conversation=args.conversation)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "exception": type(error).__name__}, ensure_ascii=False))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
