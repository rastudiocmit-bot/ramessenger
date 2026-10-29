"""One ordering contract for claiming and displaying the durable card queue."""
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from center_messenger import card_jobs

QUOTA = ("crm.card.refresh", "crm.card.lessons", "crm.card.refresh", "crm.card.lesson-history", "crm.customer.groups")


def manual(alias='e'):
    return (f"(coalesce(({alias}.payload->>'manual')::boolean,false) AND coalesce("
            f"({alias}.payload->>'manual_until')::timestamptz,coalesce(({alias}.payload->>'requested_at')::timestamptz,"
            f"{alias}.created_at)+make_interval(mins=>:interest_minutes))>statement_timestamp())")


def sends(alias='e'):
    return (f"EXISTS(SELECT 1 FROM message_jobs mj JOIN send_commands sc ON sc.workspace_id=mj.workspace_id "
            f"AND sc.id=mj.subject_id WHERE mj.workspace_id={alias}.workspace_id AND mj.kind='send' "
            "AND mj.channel_kind='alfa_max_browser' "
            "AND sc.state IN ('queued','sending') AND (mj.due_at<=statement_timestamp() "
            "OR mj.lease_until>statement_timestamp()))")


def state(db, workspace, *, lock=False):
    if lock:
        db.execute(text("INSERT INTO crm_card_loader_state(workspace_id) VALUES(:w) ON CONFLICT DO NOTHING"), {'w': workspace})
    row = db.execute(text("SELECT turn,manual_streak FROM crm_card_loader_state WHERE workspace_id=:w" +
                          (" FOR UPDATE" if lock else "")), {'w': workspace}).mappings().one_or_none()
    return dict(row or {'turn': 0, 'manual_streak': 0})


def ordering(value, policy):
    urgent = manual()
    old = "coalesce((e.payload->>'served_at')::timestamptz,e.created_at)<statement_timestamp()-interval '15 minutes'"
    requested = "coalesce((e.payload->>'requested_at')::timestamptz,e.created_at)"
    overdue = f"({urgent} AND {requested}<statement_timestamp()-make_interval(secs=>:max_wait))"
    order = (f"(:background_turn AND NOT {urgent} AND {old}) DESC,{urgent} DESC,{overdue} DESC,"
             f"CASE WHEN {overdue} THEN {requested} END ASC NULLS LAST,"
             f"({urgent} AND coalesce((e.payload->>'priority_boost')::boolean,false)) DESC,"
             f"CASE WHEN {urgent} THEN {requested} END DESC NULLS LAST,"
             f"coalesce((e.payload->>'unanswered')::boolean,false) DESC,"
             f"CASE WHEN NOT {urgent} AND e.topic=:preferred THEN 0 ELSE 1 END,"
             "CASE e.topic WHEN 'crm.card.refresh' THEN 0 WHEN 'crm.card.lessons' THEN 1 "
             "WHEN 'crm.customer.groups' THEN 2 ELSE 3 END,"
             "coalesce((e.payload->>'served_at')::timestamptz,e.created_at),e.id")
    # Topic order inside the same manual family precedes background rotation.
    return order, {'background_turn': value['manual_streak'] >= policy['manual_burst'],
                   'preferred': QUOTA[value['turn'] % len(QUOTA)], 'max_wait': policy['manual_max_wait_seconds'],
                   'interest_minutes': policy['manual_interest_minutes']}


def eligible(policy):
    topics = ','.join("'" + topic + "'" for topic in card_jobs.TOPICS)
    # Keep the source lookup parameterized by this exact job. Without OFFSET 0,
    # stale statistics can turn this into a nested semi-join that scans every
    # customer for every queued card, exceeding the normal API query budget.
    return (f"e.state='pending' AND e.topic IN ({topics}) "
        "AND EXISTS(SELECT 1 FROM alfa_settings a WHERE a.workspace_id=e.workspace_id "
        "AND a.connection_id=CAST(e.payload->>'connection' AS uuid) AND a.version=CAST(e.payload->>'version' AS integer) "
        "AND a.secret_ciphertext IS NOT NULL) "
        "AND EXISTS(SELECT 1 FROM alfa_sync_control sc JOIN alfa_customers c ON c.workspace_id=sc.workspace_id "
        "AND c.run_id=sc.published_id JOIN alfa_branches b ON b.workspace_id=c.workspace_id "
        "AND b.connection_id=sc.connection_id AND b.external_id=c.branch_id AND b.available "
        "WHERE sc.workspace_id=e.workspace_id AND sc.connection_id=CAST(e.payload->>'connection' AS uuid) "
        "AND c.branch_id=CAST(e.payload->>'branch' AS bigint) AND c.external_id=CAST(e.payload->>'customer' AS bigint) "
        "AND c.contact_id=CAST(e.payload->>'contact' AS uuid) AND c.student_id=CAST(e.payload->>'student' AS uuid) OFFSET 0) "
        "AND ((e.payload->>'available_at')::timestamptz<=statement_timestamp() OR "
        "(e.payload->>'error' IN ('access_denied','not_found','invalid_response','crm_unavailable') "
        "AND (e.payload->>'force_at')::timestamptz>(e.payload->>'failed_at')::timestamptz)) "
        "AND coalesce((e.payload->>'lease_until')::timestamptz,'epoch')<=statement_timestamp() "
        "AND NOT " + card_jobs.block_sql() +
        f" AND e.event_key NOT IN(SELECT sibling.event_key FROM outbox_events sibling WHERE sibling.workspace_id=:w "
        f"AND sibling.topic IN ({topics}) AND sibling.state='pending' "
        "AND (sibling.payload->>'lease_until')::timestamptz>statement_timestamp()) "
        + (f"AND ({manual()} OR NOT {sends().replace('e.workspace_id', ':w')}) " if policy['pause_background_on_send'] else ""))


class Deferred(Exception):
    def __init__(self, code, seconds):
        self.code, self.seconds = code, max(1, seconds)
        super().__init__(code)


def urgent(job, now=None):
    until = job.get('manual_until')
    return bool(job.get('manual') and (not until or datetime.fromisoformat(until) > (now or datetime.now(UTC))))


def request_gate(accounts, workspace, policy, job, gate):
    """Count actual wire attempts (including auth) in a rolling minute, not cache hits."""
    def call():
        gate()
        with accounts.transaction(workspace_id=workspace) as db:
            if policy['pause_background_on_send'] and not urgent(job):
                if db.execute(text("SELECT " + sends().replace('e.workspace_id', ':w')), {'w': workspace}).scalar_one():
                    raise Deferred('send_pending', policy['busy_retry_seconds'])
            state(db, workspace, lock=True)
            saved = db.execute(text("SELECT api_requests,clock_timestamp() AS at FROM crm_card_loader_state "
                                    "WHERE workspace_id=:w FOR UPDATE"), {'w': workspace}).mappings().one()
            now = saved['at']
            recent = [datetime.fromisoformat(value) for value in saved['api_requests']
                      if datetime.fromisoformat(value) > now-timedelta(minutes=1)]
            if len(recent) >= policy['requests_per_minute']:
                raise Deferred('quota_wait', math.ceil((min(recent)+timedelta(minutes=1)-now).total_seconds()))
            import json
            db.execute(text("UPDATE crm_card_loader_state SET api_requests=CAST(:p AS jsonb) WHERE workspace_id=:w"),
                       {'w': workspace, 'p': json.dumps([value.isoformat() for value in recent]+[now.isoformat()])})
        return None
    return call
