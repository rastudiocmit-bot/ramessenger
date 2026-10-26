"""Read-only cabinet metrics calculated from visible, confirmed message history."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo
import logging
from time import perf_counter

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from center_messenger.auth_security import AuthError
from center_messenger.crm_api import DirectoryReader
from center_messenger.message_order import visible_message, message_order, source_order
from center_messenger.database import log_database_error

logger = logging.getLogger(__name__)


def _query(db, stage, statement, params):
    started = perf_counter()
    try:
        return db.execute(statement, params)
    except SQLAlchemyError as error:
        log_database_error(logger, error, operation=f"summary.{stage}")
        raise
    finally:
        elapsed = (perf_counter() - started) * 1000
        if elapsed >= 250:
            logger.warning("Slow summary calculation stage=%s duration_ms=%.0f", stage, elapsed)


def summary(maximum, actor, workspace, *, now=None):
    from center_messenger.chat_queue import cached_state_sql

    now = now or datetime.now(UTC)
    with maximum.chat_transaction(actor, workspace, snapshot=True) as db:
        db.execute(text("SET LOCAL jit=off"))
        membership = maximum.accounts._workspace(db, actor, workspace)
        if membership["role"] == "operator" or (membership["role"] not in {"owner", "admin"}
                                               and "overview" not in (membership["sections"] or [])):
            raise AuthError(403, "Для сводки нужен доступ к общим сведениям центра.")
        zone = ZoneInfo(membership["timezone"])
        today = now.astimezone(zone).date()
        start = datetime.combine(today - timedelta(days=6), time.min, zone).astimezone(UTC)
        previous = datetime.combine(today - timedelta(days=13), time.min, zone).astimezone(UTC)
        snapshot = DirectoryReader._published(db, workspace)
        params = {"w": workspace, "start": start, "previous": previous, "now": now,
                  "zone": membership["timezone"], "crm": snapshot["connection_id"] if snapshot else None}
        eligible = ("c.workspace_id=:w AND c.kind='direct' AND "
                    "((cn.kind='max_bot' AND ms.connection_id IS NOT NULL) OR "
                    "(cn.kind='alfa_max_browser' AND am.connection_id IS NOT NULL "
                    "AND (CAST(:crm AS uuid) IS NULL OR am.crm_connection_id=CAST(:crm AS uuid))))")
        joins = (" FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id "
                 "AND cn.id=c.connection_id LEFT JOIN max_settings ms ON ms.workspace_id=c.workspace_id "
                 "AND ms.connection_id=c.connection_id LEFT JOIN alfa_max_settings am "
                 "ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id ")
        events = (
                  "WITH events AS (SELECT m.*,c.contact_id FROM messages m JOIN conversations c "
                  "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
                  "JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
                  "LEFT JOIN max_settings ms ON ms.workspace_id=c.workspace_id AND ms.connection_id=c.connection_id "
                  "LEFT JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
                  "WHERE " + eligible + " AND " + visible_message() + " AND m.occurred_at>=:previous "
                  "AND m.occurred_at<=:now) ")
        totals = _query(db, "totals", text(events +
            "SELECT count(*) FILTER(WHERE direction='in' AND actor_type='contact' AND occurred_at>=:start) AS incoming,"
            "count(*) FILTER(WHERE direction='in' AND actor_type='contact' AND occurred_at<:start) AS previous_incoming "
            "FROM events"), params).mappings().one()
        # The same versioned projection used by the chat list; missing/old rows
        # fall back to the exact policy. Do not scan every full timeline again.
        waiting = _query(db, "waiting", text("SELECT count(*)" + joins + "CROSS JOIN LATERAL (" + cached_state_sql() + ") q WHERE " + eligible + " AND " + "q.needs_reply"), params).scalar_one()
        day_rows = _query(db, "days", text(events +
            "SELECT (occurred_at AT TIME ZONE :zone)::date AS day,count(*) AS incoming FROM events "
            "WHERE direction='in' AND actor_type='contact' AND occurred_at>=:start GROUP BY 1"), params).mappings()
        by_day = {row["day"]: row["incoming"] for row in day_rows}
        branch_rows = _query(db, "branches", text(events +
            "SELECT coalesce(payload->>'customer_branch_id',payload->>'crm_branch') AS branch,count(*) AS incoming "
            "FROM events WHERE direction='in' AND actor_type='contact' AND occurred_at>=:start GROUP BY 1"), params).mappings().all()
        names = {str(row["id"]): row["name"] for row in DirectoryReader._branches(db, workspace)}
        branches = [{"id": row["branch"], "name": names.get(row["branch"], "Без филиала" if not row["branch"] else f"Филиал {row['branch']}"),
                     "incoming": row["incoming"]} for row in branch_rows]
        branches.sort(key=lambda row: (-row["incoming"], row["name"]))
        # A response is the first confirmed human send after the latest inbound.
        # Automated sends, failed/queued commands and later follow-ups are excluded
        # from response-time calculations, while all human replies count as work.
        human = "coalesce(m.payload->>'failed','false')<>'true' AND (m.send_command_id IS NULL OR sc.state IN ('accepted','delivered','read'))"
        responses = _query(db, "responses", text(events +
            "SELECT coalesce(u.display_name,nullif(m.payload->>'actor_name',''),'Сотрудник CRM') AS name,"
            "count(*) AS answered,avg(CASE WHEN i.occurred_at IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages p LEFT JOIN send_commands pc ON pc.workspace_id=p.workspace_id AND pc.id=p.send_command_id "
            "WHERE p.workspace_id=m.workspace_id AND p.conversation_id=m.conversation_id AND p.direction='out' "
            "AND p.actor_type='human' AND " + visible_message("p") + " AND coalesce(p.payload->>'failed','false')<>'true' "
            "AND (p.send_command_id IS NULL OR pc.state IN ('accepted','delivered','read')) "
            "AND p.occurred_at>=i.occurred_at AND (p.occurred_at," + source_order("p") + ",p.id) "
            "<(m.occurred_at," + source_order("m") + ",m.id)) THEN extract(epoch FROM m.occurred_at-i.occurred_at) END) AS average_response_seconds,"
            "sum(CASE WHEN i.occurred_at IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages p LEFT JOIN send_commands pc ON pc.workspace_id=p.workspace_id AND pc.id=p.send_command_id "
            "WHERE p.workspace_id=m.workspace_id AND p.conversation_id=m.conversation_id AND p.direction='out' "
            "AND p.actor_type='human' AND " + visible_message("p") + " AND coalesce(p.payload->>'failed','false')<>'true' "
            "AND (p.send_command_id IS NULL OR pc.state IN ('accepted','delivered','read')) "
            "AND p.occurred_at>=i.occurred_at AND (p.occurred_at," + source_order("p") + ",p.id) "
            "<(m.occurred_at," + source_order("m") + ",m.id)) THEN 1 ELSE 0 END) AS response_count "
            "FROM events m LEFT JOIN send_commands sc ON sc.workspace_id=m.workspace_id AND sc.id=m.send_command_id "
            "LEFT JOIN auth_users u ON u.id=sc.requested_by LEFT JOIN LATERAL ("
            "SELECT mi.occurred_at FROM messages mi WHERE mi.workspace_id=m.workspace_id AND mi.conversation_id=m.conversation_id "
            "AND mi.direction='in' AND mi.actor_type='contact' AND " + visible_message("mi") + " "
            "AND mi.occurred_at<=m.occurred_at ORDER BY " + message_order("mi", descending=True) + " LIMIT 1) i ON true "
            "WHERE m.direction='out' AND m.actor_type='human' AND m.occurred_at>=:start AND " + human +
            " GROUP BY 1 ORDER BY answered DESC,name"), params).mappings().all()
        operators = [{"name": row["name"], "answered": row["answered"],
                      "average_response_seconds": float(row["average_response_seconds"]) if row["average_response_seconds"] is not None else None}
                     for row in responses]
        count = sum(row["response_count"] for row in responses)
        average = (sum(float(row["average_response_seconds"]) * row["response_count"] for row in responses
                       if row["average_response_seconds"] is not None) / count) if count else None
        new_leads = 0
        if snapshot:
            params["run"] = snapshot["run"]
            new_leads = _query(db, "new_leads", text(events +
                "SELECT count(DISTINCT e.contact_id) FROM events e WHERE e.direction='in' AND e.actor_type='contact' "
                "AND e.occurred_at>=:start AND EXISTS(SELECT 1 FROM alfa_customers a WHERE a.workspace_id=e.workspace_id "
                "AND a.run_id=:run AND a.contact_id=e.contact_id AND a.status='lead') "
                "AND NOT EXISTS(SELECT 1 FROM alfa_customers active WHERE active.workspace_id=e.workspace_id "
                "AND active.run_id=:run AND active.contact_id=e.contact_id AND active.status='client') AND NOT EXISTS("
                "SELECT 1 FROM messages older JOIN conversations oc ON oc.workspace_id=older.workspace_id AND oc.id=older.conversation_id "
                "WHERE older.workspace_id=e.workspace_id AND oc.contact_id=e.contact_id AND older.direction='in' "
                "AND older.actor_type='contact' AND older.occurred_at<:start AND " + visible_message("older") + ")"), params).scalar_one()
        weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        days = [{"date": str(today - timedelta(days=n)), "label": weekdays[(today - timedelta(days=n)).weekday()],
                 "incoming": by_day.get(today - timedelta(days=n), 0)} for n in range(6, -1, -1)]
        return {**dict(totals), "waiting": waiting, "average_response_seconds": average,
                "new_leads": new_leads, "days": days, "branches": branches, "operators": operators,
                "scope": "Все филиалы", "computed_at": now, "period_start": start}
