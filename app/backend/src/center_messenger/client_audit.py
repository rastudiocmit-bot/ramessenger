"""Read-only local diagnosis of named CRM cards and their messenger links.

Never calls CRM, enqueues work or reads message bodies/credentials. A name is
only a search filter; each matching CRM ID is reported separately, not merged.
"""
import argparse
import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from center_messenger import card_jobs, card_preference
from center_messenger.config import Settings
from center_messenger.crm_api import DirectoryReader
from center_messenger.database import make_engine
from center_messenger.message_order import visible_message
from center_messenger.summary_audit import failure


def inspect(db, workspace, name):
    snapshot = DirectoryReader._published(db, workspace)
    result = {"read_only": True, "crm_requests": 0, "workspace_id": str(workspace), "query": name,
              "matches": [], "conversations": []}
    if not snapshot:
        return result | {"status": "directory_unavailable"}
    pattern = "%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    args = {"w": workspace, "run": snapshot["run"], "conn": snapshot["connection_id"],
            "v": snapshot["version"], "q": pattern, **card_preference.params(snapshot)}
    rows = list(db.execute(text("SELECT a.external_id,a.branch_id,a.contact_id,a.student_id,a.name,a.legal_name,a.status,"
        "coalesce(jsonb_array_length(a.phones),0)>0 AS has_phone,b.available AS branch_available,"
        "f.refreshed_at,f.lessons_refreshed_at,f.lessons_full_refreshed_at,"
        "jsonb_array_length(f.subscriptions) AS subscriptions,jsonb_array_length(f.lessons) AS lessons "
        "FROM alfa_customers a JOIN alfa_branches b ON b.workspace_id=a.workspace_id "
        "AND b.connection_id=:conn AND b.external_id=a.branch_id LEFT JOIN alfa_directory d "
        "ON d.workspace_id=a.workspace_id AND d.run_id=a.run_id AND d.contact_id=a.contact_id "
        "LEFT JOIN alfa_family_details f ON f.workspace_id=a.workspace_id AND f.connection_id=:conn "
        "AND f.branch_id=a.branch_id AND f.customer_id=a.external_id AND f.settings_version=:v "
        "WHERE a.workspace_id=:w AND a.run_id=:run AND (a.name ILIKE :q OR a.legal_name ILIKE :q "
        "OR d.display_name ILIKE :q) ORDER BY a.external_id,a.branch_id LIMIT 51"), args).mappings())
    result.update(status="found" if rows else "not_found", truncated=len(rows)>50,
                  directory_at=snapshot["finished_at"], settings_version=snapshot["version"])
    result["matches"] = [dict(row) for row in rows[:50]]
    contacts = list({row["contact_id"] for row in rows[:50] if row["contact_id"]})
    dialogs = list(db.execute(text("SELECT c.id,c.contact_id,c.external_chat_id,c.crm_link_manual,"
        "c.needs_reply,c.last_inbound_at,c.last_message_at,c.stored_message_count,cn.kind AS channel "
        "FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
        "LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
        + card_preference.join_sql(snapshot) +
        "WHERE c.workspace_id=:w AND c.kind='direct' AND (" + card_preference.family_sql() + " && CAST(:contacts AS uuid[]) "
        "OR c.peer_name ILIKE :q) ORDER BY c.last_message_at DESC NULLS LAST,c.id LIMIT 51"),
        args | {"contacts": contacts}).mappings())
    result["truncated"] |= len(dialogs)>50
    if dialogs:
        result["status"] = "found"
    contacts = list(set(contacts) | {row["contact_id"] for row in dialogs[:50] if row["contact_id"]})
    for row in dialogs[:50]:
        item = dict(row)
        item["selection"] = card_preference.public(card_preference.selected(
            db, workspace, snapshot, row["id"], row["contact_id"]), manual=row["crm_link_manual"])
        item["messages"] = dict(db.execute(text("SELECT count(*) AS saved,"
            "count(*) FILTER(WHERE " + visible_message() + ") AS visible,"
            "count(*) FILTER(WHERE m.direction='in' AND " + visible_message() + ") AS incoming,"
            "count(*) FILTER(WHERE m.actor_type='system' AND " + visible_message() + ") AS system,"
            "min(m.occurred_at) AS oldest_at,max(m.occurred_at) AS newest_at FROM messages m "
            "WHERE m.workspace_id=:w AND m.conversation_id=:c"), {"w": workspace, "c": row["id"]}).mappings().one())
        result["conversations"].append(item)
    active = card_jobs.blocks(db, workspace, snapshot["connection_id"], snapshot["version"])
    jobs = db.execute(text("SELECT (payload-'checkpoint')||jsonb_build_object('topic',topic) FROM outbox_events "
        "WHERE workspace_id=:w AND topic=ANY(CAST(:topics AS text[])) AND state='pending' "
        "AND payload->>'connection'=CAST(:conn AS text) AND payload->>'version'=CAST(:v AS text) "
        "AND payload->>'contact'=ANY(CAST(:contacts AS text[])) ORDER BY topic,event_key LIMIT 201"),
        args | {"topics": list(card_jobs.TOPICS), "contacts": list(map(str, contacts))}).scalars()
    result["jobs"] = [{key: job.get(key) for key in ("topic", "branch", "customer", "version", "error", "cause",
        "http_status", "stage", "page", "attempts", "available_at", "paused", "lease_until")}
        for job in (card_jobs.effective_job(value, active) for value in jobs)]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    if not 3 <= len(args.name.strip()) <= 200:
        parser.error("name must contain 3–200 characters")
    engine = None
    try:
        engine = make_engine(Settings())
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {"w": str(args.workspace)})
            report = inspect(db, args.workspace, args.name.strip())
    except SQLAlchemyError as error:
        report = {"status": "failed", **failure(error)}
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return int(report.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
