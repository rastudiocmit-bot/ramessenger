"""Durable, tenant-scoped CRM card refreshes; no operator session is required.

Jobs use the existing transactional outbox and facts use alfa_family_details in
PostgreSQL's persistent volume. A lease, not an open browser, owns each request.
"""
import json
import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_family import FUTURE_DAYS, fetch_family_finance, fetch_family_lessons, merge_family_lessons
from center_messenger.auth_security import AuthError
from center_messenger import group_links, card_preference, card_jobs, card_loader_settings, card_queue

TOPIC = "crm.card.refresh"
LESSONS = "crm.card.lessons"
HISTORY = "crm.card.lesson-history"
GROUPS = group_links.TOPIC
TOPICS = (TOPIC, LESSONS, HISTORY, GROUPS)
LEASE = timedelta(seconds=90)
TARIFF_CACHE = timedelta(minutes=30)
SUBSCRIPTIONS_STALE_SQL = """EXISTS(SELECT 1 FROM jsonb_array_elements(f.subscriptions) item
    WHERE NOT(item ? 'subject_ids') OR NOT(item ? 'lesson_type_ids'))"""
LESSONS_STALE_SQL = """EXISTS(SELECT 1 FROM jsonb_array_elements(f.lessons) item
    WHERE NOT(item ? 'branch_id') OR NOT(item ? 'subject_id') OR NOT(item ? 'lesson_type_id')
      OR NOT(item ? 'customer_id') OR NOT(item ? 'customer_verified'))"""
MAPPING_STALE_SQL = "(" + SUBSCRIPTIONS_STALE_SQL + " OR " + LESSONS_STALE_SQL + ")"

SECTION_META = {
    TOPIC: ("finance", "Баланс и абонементы", "refreshed_at", "finance_interval_minutes"),
    LESSONS: ("lessons", "Занятия", "lessons_refreshed_at", "lessons_interval_minutes"),
    HISTORY: ("history", "История занятий", "lessons_full_refreshed_at", "history_interval_minutes"),
    GROUPS: ("groups", "Группы", "groups_refreshed_at", "groups_interval_minutes"),
}


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _active_lease(job, now):
    lease = _parse_time(job.get("lease_until"))
    return bool(job.get("lease_owner") and lease and lease > now)


def _manual_fields(contact, *, source_contact=None, conversation=None, inherited=None, policy=None):
    if contact is None:
        return {"manual": False, "requested_at": None, "manual_until": None,
                "source_contact": None, "conversation": None,
                "priority_boost": False, "watchdog_at": None}
    now = datetime.now(UTC)
    inherited = inherited or {}
    policy = policy or card_loader_settings.DEFAULTS
    return {"manual": True, "requested_at": inherited.get("requested_at") or now.isoformat(),
            "manual_until": inherited.get("manual_until") or (now + timedelta(minutes=policy["manual_interest_minutes"])).isoformat(),
            "source_contact": str(source_contact or inherited.get("source_contact") or contact),
            "conversation": str(conversation or inherited.get("conversation"))
                if conversation or inherited.get("conversation") else None,
            "priority_boost": bool(inherited.get("priority_boost")),
            "watchdog_at": inherited.get("watchdog_at")}


def enqueue(db, workspace, snapshot, *, contact=None, force=False, topics=TOPICS,
            source_contact=None, conversation=None, manual_context=None):
    """Independent queues: finances first, recent lessons, then the daily history.

    All three keep their own leases and retry clocks. A failed history request
    cannot hold the next balance refresh. Read clients require explicit opening.
    """
    count = 0
    # One operator action owns one clock across every section. Separate clocks
    # let the last inserted yearly-history job outrank the balance shown first.
    policy = card_loader_settings.get(db, workspace)
    manual = _manual_fields(contact, source_contact=source_contact, conversation=conversation,
                            inherited=manual_context, policy=policy)
    for topic, column, interval, stale in (
        (TOPIC, "refreshed_at", policy["finance_interval_minutes"], SUBSCRIPTIONS_STALE_SQL),
        (LESSONS, "lessons_refreshed_at", policy["lessons_interval_minutes"], "false"),
        (HISTORY, "lessons_full_refreshed_at", policy["history_interval_minutes"], LESSONS_STALE_SQL),
    ):
        if topic not in topics:
            continue
        count += _enqueue(db, workspace, snapshot, contact=contact, force=force,
                         topic=topic, column=column, interval=interval, stale=stale,
                         manual=manual, policy=policy)
    if GROUPS in topics:
        count += group_links.enqueue(db, workspace, snapshot, contact=contact, force=force,
                                    source_contact=source_contact, conversation=conversation,
                                    manual_context=manual, policy=policy)
    return count


def _enqueue(db, workspace, snapshot, *, contact, force, topic, column, interval, stale, manual, policy):
    # SQL fragments below come only from the fixed policy in enqueue().
    return db.execute(text("WITH pending_families AS MATERIALIZED (" + card_preference.pending_sql(snapshot) + ") " + f"""
      INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state)
      SELECT :w,gen_random_uuid(),:topic,concat(:conn,':',c.branch_id,':',c.external_id),
        jsonb_build_object('connection',CAST(:conn AS text),'branch',c.branch_id::text,
          'customer',c.external_id::text,'contact',c.contact_id::text,'student',c.student_id::text,
          'version',:version,'unanswered',(:preload AND c.contact_id IN (SELECT contact_id FROM pending_families)),
          'manual',:manual,'requested_at',CAST(:requested_at AS text),
          'manual_until',CAST(:manual_until AS text),'source_contact',CAST(:source_contact AS text),
          'conversation',CAST(:conversation AS text),'due_at',coalesce(f.{column}+make_interval(mins=>:interval),now()),
          'priority_boost',:priority_boost,'watchdog_at',CAST(:watchdog_at AS text),
          'force_at',CASE WHEN :force THEN CAST(clock_timestamp() AS text) ELSE NULL END,
          'available_at',now(),'attempts',0),'pending'
      FROM alfa_customers c JOIN alfa_branches b ON b.workspace_id=c.workspace_id
        AND b.connection_id=:conn AND b.external_id=c.branch_id AND b.available
      LEFT JOIN alfa_family_details f ON f.workspace_id=c.workspace_id AND f.connection_id=:conn
        AND f.branch_id=c.branch_id AND f.customer_id=c.external_id AND f.settings_version=:version
      WHERE c.workspace_id=:w AND c.run_id=:run AND c.contact_id IS NOT NULL AND c.student_id IS NOT NULL
        AND (CAST(:contact AS uuid) IS NULL OR c.contact_id=CAST(:contact AS uuid))
        AND (:manual OR (((:preload AND c.contact_id IN (SELECT contact_id FROM pending_families))
          OR (:active AND c.status='client')) AND (NOT :clients_only OR c.status='client')))
        AND (:finance OR f.customer_id IS NOT NULL)
        AND (:force OR f.{column} IS NULL OR (:repeat AND f.{column}<=now()-make_interval(mins=>CASE
          WHEN NOT :manual AND NOT (:preload AND c.contact_id IN (SELECT contact_id FROM pending_families))
          THEN :active_interval ELSE :interval END)) OR {stale})
      ON CONFLICT (workspace_id,topic,event_key) DO UPDATE SET
        state='pending',payload=CASE WHEN outbox_events.state='pending'
          AND outbox_events.payload->>'version'=EXCLUDED.payload->>'version'
          AND outbox_events.payload->>'contact'=EXCLUDED.payload->>'contact'
          AND outbox_events.payload->>'student'=EXCLUDED.payload->>'student'
          THEN outbox_events.payload||jsonb_build_object(
            'unanswered',EXCLUDED.payload->'unanswered',
            'manual',coalesce((outbox_events.payload->>'manual')::boolean,false) OR :manual,
            'requested_at',CASE WHEN :manual THEN CAST(:requested_at AS text) ELSE outbox_events.payload->>'requested_at' END,
            'manual_until',CASE WHEN :manual THEN CAST(:manual_until AS text) ELSE outbox_events.payload->>'manual_until' END,
            'source_contact',CASE WHEN :manual THEN CAST(:source_contact AS text) ELSE outbox_events.payload->>'source_contact' END,
            'conversation',CASE WHEN :manual THEN CAST(:conversation AS text) ELSE outbox_events.payload->>'conversation' END,
            'priority_boost',CASE WHEN :manual THEN :priority_boost ELSE coalesce((outbox_events.payload->>'priority_boost')::boolean,false) END,
            'watchdog_at',CASE WHEN :manual THEN CAST(:watchdog_at AS text) ELSE outbox_events.payload->>'watchdog_at' END,
            'force_at',CASE WHEN :force THEN EXCLUDED.payload->>'force_at' ELSE outbox_events.payload->>'force_at' END)
          ELSE EXCLUDED.payload END
      WHERE (outbox_events.state<>'pending' AND coalesce((outbox_events.payload->>'lease_until')::timestamptz,'epoch')<=now())
        OR outbox_events.payload->>'version'<>EXCLUDED.payload->>'version'
        OR outbox_events.payload->>'contact'<>EXCLUDED.payload->>'contact'
        OR outbox_events.payload->>'student'<>EXCLUDED.payload->>'student'
        OR (:manual AND (outbox_events.payload->>'manual' IS DISTINCT FROM 'true'
          OR coalesce((outbox_events.payload->>'requested_at')::timestamptz,'epoch')<CAST(:requested_at AS timestamptz)))
        OR outbox_events.payload->'unanswered' IS DISTINCT FROM EXCLUDED.payload->'unanswered'
        OR :force
      """), {"w": workspace, "topic": topic, "conn": snapshot["connection_id"],
               "run": snapshot["run"], "version": snapshot["version"],
               "contact": contact, "manual": contact is not None, "force": force,
               "preload": policy["preload_unanswered"], "active": policy["preload_active_cards"],
               "clients_only": policy["automatic_clients_only"], "active_interval": policy["active_interval_minutes"],
               "interval": max(1, interval), "repeat": interval > 0,
               "finance": topic == TOPIC, **manual, **card_preference.params(snapshot)}).rowcount


def cache_status(db, workspace, connection, cards, contact, version, *, contacts=None, snapshot=None):
    contacts = list(contacts or [contact])
    sources = [card for student in cards for card in student["cards"]]
    now = datetime.now(UTC)
    policy = card_loader_settings.get(db, workspace)
    order, ordering = card_queue.ordering(card_queue.state(db, workspace), policy)
    jobs = list(db.execute(text("""
      WITH ranked AS (
        SELECT e.id,row_number() OVER (ORDER BY """ + order + ") AS position FROM outbox_events e "
        "WHERE e.workspace_id=:w AND " + card_queue.eligible(policy) + """
      )
      SELECT (e.payload-'checkpoint')||jsonb_build_object('topic',e.topic,'job_id',CAST(e.id AS text),
        'created_at',e.created_at,'queue_position',ranked.position,'checkpoint_pages',
        coalesce((e.payload->'checkpoint'->>'pages')::integer,0))
      FROM outbox_events e LEFT JOIN ranked ON ranked.id=e.id
      WHERE e.workspace_id=:w AND e.topic IN (:finance,:lessons,:history,:groups)
        AND e.state='pending' AND e.payload->>'connection'=:conn AND e.payload->>'version'=:v
        AND e.payload->>'contact'=ANY(CAST(:contacts AS text[]))
    """), {"w": workspace, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS,
         "conn": str(connection), "v": str(version), "contacts": list(map(str, contacts)), **ordering}).scalars())
    # Outbox JSON keeps source identifiers as strings.  Normalising both sides
    # is essential: otherwise a real queued/leased job disappears from the
    # public status when the prepared card contains integer AlfaCRM IDs.
    keys = {(str(card["branch_id"]), str(card["id"])) for card in sources}
    jobs = [job for job in jobs if (job["branch"], job["customer"]) in keys]
    active_blocks = card_jobs.blocks(db, workspace, connection, version) if jobs else []
    jobs = [card_jobs.effective_job(job, active_blocks) for job in jobs]
    if jobs and policy['pause_background_on_send'] and db.execute(text(
        "SELECT " + card_queue.sends().replace('e.workspace_id', ':w')), {'w': workspace}).scalar_one():
        jobs = [dict(job, cause='send_pending', available_at=(now+timedelta(seconds=policy['busy_retry_seconds'])).isoformat())
                if not job.get('error') and not card_queue.urgent(job, now) and not _active_lease(job, now) else job for job in jobs]
    runnable = [job for job in jobs if not job.get("paused")]
    loading = any(_active_lease(job, now) for job in runnable)
    retries = [job["available_at"] for job in runnable
               if job.get("available_at") and (job.get("error") or job.get("cause") in {"busy", "quota_wait", "send_pending"})]
    failures = [{"code": job["error"], "http_status": job.get("http_status"), "cause": job.get("cause"),
                 "operation": job["topic"], "branch_id": job["branch"], "customer_id": job["customer"],
                 "stage": job.get("stage"), "page": job.get("page"), "paused": bool(job.get("paused")),
                 "retry_at": job.get("available_at"), "text": (
                     "Не удалось обновить баланс и абонементы. " if job["topic"] == TOPIC else
                     "Не удалось обновить группы. " if job["topic"] == GROUPS else
                     "Не удалось обновить занятия. Баланс и абонементы обновляются отдельно. ")
                     + card_jobs.LABELS.get(job["error"], card_jobs.LABELS["crm_unavailable"])}
                for job in jobs if job.get("error")]
    mapping_pending = any(card.get("needs_mapping_refresh") for card in sources)

    sections = {}
    for topic, (key, label, field, interval_field) in SECTION_META.items():
        interval_minutes = policy[interval_field]
        interval = timedelta(minutes=interval_minutes) if interval_minutes else None
        section_jobs = [job for job in jobs if job["topic"] == topic]
        values = [_parse_time(card.get(field)) for card in sources]
        cached = [value for value in values if value]
        fresh = [value for value in cached if interval is None or value + interval > now]
        leased = [job for job in section_jobs if _active_lease(job, now)]
        busy = [job for job in section_jobs if job.get("cause") == "busy"]
        errors = [job for job in section_jobs if job.get("error")]
        paused = [job for job in errors if job.get("paused")]
        waiting = [job for job in section_jobs if (_parse_time(job.get("available_at")) or now) > now]
        partial = any(int(job.get("checkpoint_pages") or 0) > 0 for job in section_jobs)
        if leased:
            status = "loading"
        elif busy:
            status = "busy"
        elif paused:
            status = "error"
        elif errors and waiting:
            status = "retry_wait"
        elif waiting and any(job.get("cause") in {"quota_wait", "send_pending"} for job in section_jobs):
            status = "retry_wait"
        elif partial:
            status = "partial"
        elif section_jobs:
            status = "queued"
        elif len(fresh) == len(sources) and sources:
            status = "ready"
        elif len(cached) == len(sources) and sources:
            status = "stale"
        elif cached:
            status = "partial"
        else:
            status = "missing"
        section_retry = [_parse_time(job.get("available_at")) for job in section_jobs
                         if job.get("error") or job.get("cause") in {"busy", "quota_wait", "send_pending"}]
        section_retry = [value for value in section_retry if value]
        requested = [_parse_time(job.get("requested_at")) or _parse_time(job.get("created_at")) for job in section_jobs]
        requested = [value for value in requested if value]
        started = [_parse_time(job.get("started_at")) for job in leased]
        started = [value for value in started if value]
        positions = [int(job["queue_position"]) for job in section_jobs if job.get("queue_position")]
        error_job = errors[0] if errors else None
        sections[key] = {
            "label": label, "status": status, "updated_at": min(cached).isoformat() if cached else None,
            "interval_minutes": interval_minutes, "refresh_once": interval is None,
            "finished_at": min(cached).isoformat() if cached else None,
            "requested_at": min(requested).isoformat() if requested else None,
            "started_at": min(started).isoformat() if started else None,
            "retry_at": min(section_retry).isoformat() if section_retry else None,
            "queue_position": min(positions) if positions else None,
            "wait_exceeded": any(job.get("manual") and (_parse_time(job.get("requested_at")) or now)
                < now-timedelta(seconds=policy["manual_max_wait_seconds"]) for job in section_jobs),
            "sources": len(sources), "cached_sources": len(cached), "fresh_sources": len(fresh),
            "source_customer_ids": sorted({str(job["customer"]) for job in section_jobs}
                                          or {str(card["id"]) for card in sources}),
            "directory_version": version,
            "checkpoint_pages": sum(int(job.get("checkpoint_pages") or 0) for job in section_jobs),
            "attempts": max([int(job.get("attempts") or 0) for job in section_jobs], default=0),
            "queue_wait_ms": max([int(job.get("queue_wait_ms") or 0) for job in section_jobs], default=0),
            "duration_ms": max([int(job.get("duration_ms") or 0) for job in section_jobs], default=0),
            "error_code": error_job.get("error") if error_job else None,
            "error_message": card_jobs.LABELS.get(error_job.get("error"), card_jobs.LABELS["crm_unavailable"])
                             if error_job else None,
            "stage": next((job.get("stage") for job in leased if job.get("stage")), None),
            "page": next((job.get("page") for job in leased if job.get("page") is not None), None),
        }

    finance_at = _parse_time(sections["finance"]["updated_at"])
    lessons_at = _parse_time(sections["lessons"]["updated_at"])
    history_at = _parse_time(sections["history"]["updated_at"])
    groups_at = _parse_time(sections["groups"]["updated_at"])
    required = [sections[name]["status"] for name in ("finance", "lessons", "groups")]
    complete = bool(required) and all(status == "ready" for status in required) and not mapping_pending
    statuses = [section["status"] for section in sections.values()]
    state = next((status for status in ("loading", "busy", "retry_wait", "error", "partial", "queued")
                  if status in statuses), "ready" if complete else "stale" if any(_parse_time(card.get("refreshed_at")) for card in sources) else "missing")
    times = [_parse_time(card.get("refreshed_at")) for card in sources]
    times = [value for value in times if value]
    ready = len(times) == len(sources) and bool(sources)
    updated = min(times) if ready else None
    automatic = policy["preload_unanswered"] and (card_preference.is_pending(db, workspace, snapshot, contacts) if snapshot else db.execute(text(
        "SELECT EXISTS(SELECT 1 FROM conversations WHERE workspace_id=:w AND contact_id=:c AND needs_reply AND kind='direct')"),
        {"w": workspace, "c": contact}).scalar_one())
    positions = [section["queue_position"] for section in sections.values() if section["queue_position"]]
    finance_interval = timedelta(minutes=policy["finance_interval_minutes"])
    lessons_interval = timedelta(minutes=policy["lessons_interval_minutes"])
    groups_interval = timedelta(minutes=policy["groups_interval_minutes"])
    return {"auto_refresh": bool(automatic), "ready": ready, "complete": complete, "incomplete": not complete,
            "state": state, "loading": loading, "refreshing": loading, "busy": any(job.get("cause") == "busy" for job in jobs),
            "pending": bool(jobs), "queued": sum(not _active_lease(job, now) for job in jobs),
            "queue_position": min(positions) if positions else None, "sections": sections,
            "wait_exceeded": any(section["wait_exceeded"] for section in sections.values()),
            "max_wait_seconds": policy["manual_max_wait_seconds"],
            "updated_at": updated.isoformat() if updated else None,
            "paused": any(job.get("paused") for job in jobs), "failures": failures,
            "next_refresh_at": (updated + finance_interval).isoformat() if updated else None,
            "interval_minutes": policy["finance_interval_minutes"], "sources": len(sources), "cached_sources": len(times),
            "mapping_pending": mapping_pending,
            "lessons_updated_at": lessons_at.isoformat() if lessons_at else None,
            "lessons_history_updated_at": history_at.isoformat() if history_at else None,
            "lessons_interval_minutes": policy["lessons_interval_minutes"],
            "lessons_history_interval_hours": policy["history_interval_minutes"] / 60,
            "lessons_history_refresh_once": policy["history_interval_minutes"] == 0,
            "groups_updated_at": groups_at.isoformat() if groups_at else None,
            "groups_interval_hours": policy["groups_interval_minutes"] / 60,
            "groups_refreshing": any(job["topic"] == GROUPS and _active_lease(job, now) for job in runnable),
            "groups_pending": any(job["topic"] == GROUPS for job in jobs),
            "lessons_refreshing": any(job["topic"] == LESSONS and _active_lease(job, now) for job in runnable),
            "lessons_pending": any(job["topic"] == LESSONS for job in jobs),
            "lessons_history_refreshing": any(job["topic"] == HISTORY and _active_lease(job, now) for job in runnable),
            "lessons_history_pending": any(job["topic"] == HISTORY for job in jobs),
            "overdue": not ready or mapping_pending or updated + finance_interval <= now
                       or lessons_at is None or lessons_at + lessons_interval <= now
                       or groups_at is None or groups_at + groups_interval <= now,
            "retry_at": min(retries, default=None),
            "error": failures[0]["text"] if failures else None}


def current(db, workspace, connection):
    """Only a leased, current-source job can be displayed as processing now."""
    row = db.execute(text("""
      SELECT e.topic,(e.payload-'checkpoint')||jsonb_build_object('checkpoint_pages',
        coalesce((e.payload->'checkpoint'->>'pages')::integer,0)) AS payload,
        c.name AS student_name,coalesce(nullif(c.legal_name,''),d.display_name,c.name) AS name
      FROM outbox_events e JOIN alfa_settings s ON s.workspace_id=e.workspace_id
        AND s.connection_id=:conn AND s.secret_ciphertext IS NOT NULL
      JOIN alfa_sync_control sc ON sc.workspace_id=s.workspace_id AND sc.connection_id=s.connection_id
      JOIN alfa_customers c ON c.workspace_id=s.workspace_id AND c.run_id=sc.published_id
        AND c.branch_id::text=e.payload->>'branch' AND c.external_id::text=e.payload->>'customer'
        AND c.contact_id::text=e.payload->>'contact' AND c.student_id::text=e.payload->>'student'
      JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND b.connection_id=s.connection_id
        AND b.external_id=c.branch_id AND b.available
      LEFT JOIN alfa_directory d ON d.workspace_id=c.workspace_id AND d.run_id=c.run_id AND d.contact_id=c.contact_id
      WHERE e.workspace_id=:w AND e.topic IN (:finance,:lessons,:history,:groups) AND e.state='pending'
        AND e.payload->>'connection'=CAST(:conn AS text) AND e.payload->>'version'=s.version::text
        AND e.payload->>'lease_owner' IS NOT NULL AND (e.payload->>'lease_until')::timestamptz>now()
      ORDER BY e.created_at,e.id LIMIT 1
    """), {"w": workspace, "conn": connection, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS}).mappings().one_or_none()
    if not row:
        return None
    job = row["payload"]
    return {"name": row["name"], "student_name": row["student_name"],
            "customer_id": job["customer"], "branch_id": job["branch"],
            "started_at": job.get("started_at"), "lease_until": job["lease_until"],
            "stage": job.get("stage"), "page": job.get("page"),
            "api_calls": int(job.get("checkpoint_pages") or 0),
            "attempts": int(job.get("attempts") or 0),
            "manual": bool(job.get("manual")), "operation": {
                TOPIC: "reading_finance", LESSONS: "reading_recent_lessons", HISTORY: "reading_lesson_history", GROUPS: "reading_customer_groups"
            }[row["topic"]]}


def progress(db, workspace, connection, snapshot):
    """Count families needing an answer, without multiplying branch source cards."""
    at = datetime.now(UTC).isoformat()
    policy = card_loader_settings.get(db, workspace)
    empty = {"configured": bool(snapshot), "total": 0, "ready": 0, "cached": 0,
             "queued": 0, "manual_queued": 0, "background_queued": 0, "loading": 0,
             "busy": 0, "stalled": 0, "oldest_wait_seconds": 0,
             "errors": 0, "next_retry_at": None,
             "interval_minutes": policy["finance_interval_minutes"], "as_of": at}
    if not snapshot or snapshot["connection_id"] != connection:
        return empty
    row = db.execute(text("""
      WITH pending_families AS MATERIALIZED (""" + card_preference.pending_sql(snapshot) + """), families AS (
        SELECT c.contact_id,
          bool_and(f.customer_id IS NOT NULL) AS cached,
          bool_and(f.customer_id IS NOT NULL AND f.refreshed_at>now()-make_interval(mins=>:finance_interval)
            AND f.lessons_refreshed_at>now()-make_interval(mins=>:lessons_interval)
            AND NOT """ + MAPPING_STALE_SQL + """) AS ready
        FROM alfa_customers c JOIN alfa_branches b ON b.workspace_id=c.workspace_id
          AND b.connection_id=:conn AND b.external_id=c.branch_id AND b.available
        JOIN alfa_settings s ON s.workspace_id=c.workspace_id AND s.connection_id=:conn
          AND s.secret_ciphertext IS NOT NULL
        LEFT JOIN alfa_family_details f ON f.workspace_id=c.workspace_id AND f.connection_id=:conn
          AND f.branch_id=c.branch_id AND f.customer_id=c.external_id AND f.settings_version=s.version
        WHERE c.workspace_id=:w AND c.run_id=:run AND c.contact_id IS NOT NULL AND c.student_id IS NOT NULL
          AND c.contact_id IN (SELECT contact_id FROM pending_families)
        GROUP BY c.contact_id
      ), jobs AS (
        SELECT e.payload-'checkpoint' AS payload FROM outbox_events e JOIN alfa_settings s ON s.workspace_id=e.workspace_id
          AND s.connection_id=:conn AND s.secret_ciphertext IS NOT NULL
        WHERE e.workspace_id=:w AND e.topic IN (:finance,:lessons,:history,:groups) AND e.state='pending'
          AND e.payload->>'connection'=CAST(:conn AS text) AND e.payload->>'version'=s.version::text
          AND EXISTS(SELECT 1 FROM families f WHERE f.contact_id::text=e.payload->>'contact')
      )
      SELECT (SELECT count(*) FROM families) AS total,
        (SELECT count(*) FROM families WHERE ready) AS ready,
        (SELECT count(*) FROM families WHERE cached) AS cached,
        (SELECT count(DISTINCT payload->>'contact') FROM jobs) AS queued,
        (SELECT count(DISTINCT payload->>'contact') FROM jobs WHERE payload ? 'error') AS errors,
        (SELECT min((payload->>'available_at')::timestamptz) FROM jobs WHERE payload ? 'error') AS next_retry_at
    """), {"w": workspace, "conn": connection, "run": snapshot["run"],
             "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS,
             "finance_interval": policy["finance_interval_minutes"],
             "lessons_interval": policy["lessons_interval_minutes"],
             **card_preference.params(snapshot)}).mappings().one()
    queue = dict(db.execute(text("""
      SELECT count(*) FILTER(WHERE coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now()) AS queue_depth,
        count(*) FILTER(WHERE coalesce((payload->>'manual')::boolean,false)
          AND coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now()) AS manual_queued,
        count(*) FILTER(WHERE NOT coalesce((payload->>'manual')::boolean,false)
          AND coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now()) AS background_queued,
        count(*) FILTER(WHERE coalesce((payload->>'lease_until')::timestamptz,'epoch')>now()) AS loading,
        count(*) FILTER(WHERE payload->>'cause'='busy') AS busy,
        count(*) FILTER(WHERE (coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now()
          AND coalesce((payload->>'requested_at')::timestamptz,created_at)<now()-make_interval(secs=>:priority_seconds))
          OR (coalesce((payload->>'lease_until')::timestamptz,'epoch')>now()
            AND coalesce((payload->>'last_progress_at')::timestamptz,
              (payload->>'started_at')::timestamptz,created_at)<now()-interval '60 seconds')) AS stalled,
        coalesce(max(EXTRACT(EPOCH FROM now()-coalesce((payload->>'requested_at')::timestamptz,created_at)))
          FILTER(WHERE coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now()),0)::integer AS oldest_wait_seconds
      FROM outbox_events WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history,:groups)
        AND state='pending' AND payload->>'connection'=CAST(:conn AS text)
        AND payload->>'version'=CAST(:version AS text)
    """), {"w": workspace, "conn": connection, "version": snapshot["version"],
             "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS,
             "priority_seconds": policy["manual_priority_seconds"]}).mappings().one())
    return empty | dict(row) | queue | {"groups": group_links.progress(db, workspace, snapshot)}


class _TariffCache:
    """Only group/tariff metadata is shared; client money and CGI are read live."""
    def __init__(self, client, cache, scope):
        self.client, self.cache, self.scope = client, cache, scope

    def call(self, path, body):
        if not path.endswith(('/tariff/index', '/group/index')):
            return self.client.call(path, body)
        key = (self.scope, path, json.dumps(body, sort_keys=True))
        saved = self.cache.get(key)
        if saved and saved[0] > time.monotonic():
            return saved[1]
        data = self.client.call(path, body)
        if len(self.cache) >= 256:
            self.cache.clear()
        self.cache[key] = (time.monotonic() + TARIFF_CACHE.total_seconds(), data)
        return data


class FamilyCacheWorker:
    def __init__(self, connections):
        self.connections, self.accounts = connections, connections.accounts
        self.stopping = threading.Event()
        self.owner, self.tariffs = str(uuid4()), {}
        self._schedule_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="crm-family-cache", daemon=True)

    def schedule(self, workspace):
        from center_messenger.crm_api import DirectoryReader
        with self.accounts.transaction(workspace_id=workspace) as db:
            snapshot = DirectoryReader._published(db, workspace)
            if snapshot:
                from center_messenger.family_identity import repair
                repair(db, workspace, snapshot)
                from center_messenger.crm_review import repair as repair_review
                repair_review(db, workspace, snapshot)
                from center_messenger.customer_updates import repair_labels
                repair_labels(db, workspace, snapshot)
                card_preference.repair(db, workspace, snapshot)
                enqueue(db, workspace, snapshot)
                self.reconcile_manual(db, workspace, snapshot)
                self.watchdog(db, workspace, snapshot)

    def watchdog(self, db, workspace, snapshot):
        """Make an opened card recoverable when it waited without a lease."""
        policy = card_loader_settings.get(db, workspace)
        db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object("
            "'priority_boost',true,'watchdog_at',CAST(clock_timestamp() AS text)) "
            "WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history,:groups) AND state='pending' "
            "AND payload->>'connection'=CAST(:conn AS text) AND payload->>'version'=CAST(:version AS text) "
            "AND coalesce((payload->>'manual')::boolean,false) "
            "AND coalesce((payload->>'manual_until')::timestamptz,'infinity')>clock_timestamp() "
            "AND coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now() "
            "AND (payload->>'available_at')::timestamptz<=now() "
            "AND coalesce((payload->>'requested_at')::timestamptz,created_at)<now()-make_interval(secs=>:priority_seconds)"),
            {"w": workspace, "conn": snapshot["connection_id"], "version": snapshot["version"],
             "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS,
             "priority_seconds": policy["manual_priority_seconds"]})

    def reconcile_manual(self, db, workspace, snapshot):
        """Recover an incomplete open card even if an obsolete job was retired."""
        rows = list(db.execute(text("""
          SELECT DISTINCT ON (coalesce(payload->>'source_contact',payload->>'contact'))
            coalesce(payload->>'source_contact',payload->>'contact') AS source_contact,
            payload->>'conversation' AS conversation,
            (payload->>'requested_at')::timestamptz AS requested_at,
            (payload->>'manual_until')::timestamptz AS manual_until
          FROM outbox_events WHERE workspace_id=:w
            AND topic IN (:finance,:lessons,:history,:groups)
            AND state='pending'
            AND coalesce((payload->>'manual')::boolean,false)
            AND (payload->>'manual_until')::timestamptz>now()
          ORDER BY coalesce(payload->>'source_contact',payload->>'contact'),
            (payload->>'requested_at')::timestamptz DESC NULLS LAST LIMIT 100
        """), {"w": workspace,
                 "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS}).mappings())
        for row in rows:
            try:
                source_contact = UUID(row["source_contact"])
                conversation = UUID(row["conversation"]) if row["conversation"] else None
            except (TypeError, ValueError):
                continue
            card_preference.refresh(db, workspace, snapshot, [source_contact])
            selection = card_preference.selected(db, workspace, snapshot, conversation, source_contact)
            targets = selection["contacts"] if selection else [source_contact]
            for target in targets:
                enqueue(db, workspace, snapshot, contact=target, source_contact=source_contact,
                        conversation=conversation)
            # Reconciliation must not turn a ten-minute observation window into
            # a permanent background refresh. Restore the original deadline.
            db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object("
                "'requested_at',CAST(:requested AS text),'manual_until',CAST(:until AS text)) "
                "WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history,:groups) AND state='pending' "
                "AND payload->>'connection'=CAST(:conn AS text) AND payload->>'version'=CAST(:version AS text) "
                "AND payload->>'contact'=ANY(CAST(:targets AS text[]))"),
                {"w": workspace, "conn": snapshot["connection_id"], "version": snapshot["version"],
                 "requested": row["requested_at"], "until": row["manual_until"],
                 "targets": list(map(str, targets)), "finance": TOPIC, "lessons": LESSONS,
                 "history": HISTORY, "groups": GROUPS})
            db.execute(text("UPDATE outbox_events SET state='published',"
                "payload=(payload-'lease_owner'-'lease_until'-'checkpoint')||"
                "jsonb_build_object('rebased_at',CAST(clock_timestamp() AS text)) "
                "WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history,:groups) AND state='pending' "
                "AND coalesce(payload->>'source_contact',payload->>'contact')=:source "
                "AND payload->>'contact'<>ALL(CAST(:targets AS text[]))"),
                {"w": workspace, "source": str(source_contact), "targets": list(map(str, targets)),
                 "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS})
        # Current-version jobs now carry every still-open manual request.  Old
        # settings/directory jobs are fenced so they cannot occupy the queue or
        # publish a late result against a replaced Customer ID.
        db.execute(text("UPDATE outbox_events SET state='published',"
            "payload=(payload-'lease_owner'-'lease_until'-'checkpoint')||"
            "jsonb_build_object('rebased_at',CAST(clock_timestamp() AS text)) "
            "WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history,:groups) AND state='pending' "
            "AND (payload->>'connection'<>CAST(:conn AS text) "
            "OR payload->>'version'<>CAST(:version AS text) OR NOT EXISTS(SELECT 1 "
            "FROM alfa_sync_control sc JOIN alfa_customers c ON c.workspace_id=sc.workspace_id "
            "AND c.run_id=sc.published_id WHERE sc.workspace_id=outbox_events.workspace_id "
            "AND sc.connection_id=:conn AND c.branch_id::text=outbox_events.payload->>'branch' "
            "AND c.external_id::text=outbox_events.payload->>'customer' "
            "AND c.contact_id::text=outbox_events.payload->>'contact' "
            "AND c.student_id::text=outbox_events.payload->>'student'))"),
            {"w": workspace, "conn": snapshot["connection_id"], "version": snapshot["version"],
             "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS})

    def claim(self, workspace):
        with self.accounts.transaction(workspace_id=workspace) as db:
            # Shared across both scheduler slots and any other API process.
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            policy = card_loader_settings.get(db, workspace)
            scheduler = card_queue.state(db, workspace, lock=True)
            leased = db.execute(text("SELECT count(*) FROM outbox_events WHERE workspace_id=:w "
                "AND topic=ANY(CAST(:topics AS text[])) AND state='pending' "
                "AND (payload->>'lease_until')::timestamptz>clock_timestamp()"),
                {"w": workspace, "topics": list(TOPICS)}).scalar_one()
            if leased >= policy['parallel_card_jobs']:
                return None
            order, parameters = card_queue.ordering(scheduler, policy)
            row = db.execute(text("SELECT e.id,e.topic,e.payload,e.created_at FROM outbox_events e WHERE e.workspace_id=:w AND "
                + card_queue.eligible(policy) + " ORDER BY " + order + " LIMIT 1 FOR UPDATE OF e SKIP LOCKED"),
                {"w": workspace, **parameters}).mappings().one_or_none()
            if not row:
                return None
            job = dict(row['payload'])
            now = db.execute(text('SELECT clock_timestamp()')).scalar_one()
            requested = _parse_time(job.get('requested_at')) or row['created_at']
            if job.get('manual') and not job.get('manual_until'):
                job['manual_until'] = (requested+timedelta(minutes=policy['manual_interest_minutes'])).isoformat()
            manual_until = _parse_time(job.get('manual_until'))
            urgent = bool(job.get('manual') and (not manual_until or manual_until > now))
            streak = scheduler['manual_streak'] + 1 if urgent else 0
            db.execute(text("UPDATE crm_card_loader_state SET turn=turn+1,manual_streak=:streak WHERE workspace_id=:w"),
                       {"w": workspace, "streak": streak})
            job.update(lease_owner=str(uuid4()), lease_until=(now + LEASE).isoformat(),
                       started_at=now.isoformat(), last_progress_at=now.isoformat(),
                       queue_wait_ms=max(0, int((now-requested).total_seconds()*1000)), served_at=now.isoformat())
            db.execute(text("UPDATE outbox_events SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                       {"w": workspace, "id": row['id'], "p": json.dumps(job)})
            return {**job, "id": row['id'], "topic": row['topic']}

    def checkpoint(self, job, owned, snapshot, today, *, started_at=None):
        scope = [job["connection"], job["version"], str(snapshot["run"]), job["branch"],
                 job["customer"], job["contact"], job["student"], job["topic"], job.get("force_at")]
        saved = owned.get("checkpoint") or {}
        if saved.get("scope") != scope:
            saved = {"scope": scope, "today": today.isoformat(), "started_at": (started_at or datetime.now(UTC)).isoformat(),
                     "responses": {}, "pages": 0}
        return saved

    def checkpoint_client(self, client, workspace, job, checkpoint, settings, credentials, *, limit=3):
        def persist(value):
            with self.accounts.transaction(workspace_id=workspace) as db:
                db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                current = self.connections._row(db, workspace)
                from center_messenger.crm_api import DirectoryReader
                snapshot = DirectoryReader._published(db, workspace)
                if (not current or not current["secret_ciphertext"] or current["version"] != job["version"]
                    or str(current["connection_id"]) != job["connection"] or not snapshot
                    or str(snapshot["run"]) != checkpoint["scope"][2] or not self._source(db, workspace, job)):
                    raise card_jobs.LostCardLease()
                written = db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object("
                    "'checkpoint',CAST(:p AS jsonb),'stage',CAST(:stage AS text),'page',CAST(:page AS integer),"
                    "'last_progress_at',CAST(clock_timestamp() AS text),'lease_until',"
                    "CAST(clock_timestamp()+make_interval(secs=>:lease_seconds) AS text)) "
                    "WHERE workspace_id=:w AND id=:id "
                    "AND state='pending' AND payload->>'lease_owner'=:owner "
                    "AND (payload->>'lease_until')::timestamptz>now() RETURNING id"),
                    {"w": workspace, "id": job["id"], "owner": job["lease_owner"], "p": json.dumps(value),
                     "stage": value.get("stage", "prepare"), "page": value.get("page"),
                     "lease_seconds": int(LEASE.total_seconds())}).scalar_one_or_none()
                if not written:
                    raise card_jobs.LostCardLease()
                db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:s WHERE workspace_id=:w AND version=:v"),
                    {"w": workspace, "v": job["version"],
                     "s": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})
        return card_jobs.CheckpointClient(client, checkpoint, persist, limit=limit)

    def yield_job(self, workspace, job):
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until'-'error'-'paused')"
                "||jsonb_build_object('available_at',now()) WHERE workspace_id=:w AND id=:id AND state='pending' "
                "AND payload->>'lease_owner'=:owner AND (payload->>'lease_until')::timestamptz>now()"),
                {"w": workspace, "id": job["id"], "owner": job["lease_owner"]})

    def _source(self, db, workspace, job):
        return db.execute(text("SELECT c.contact_id,c.student_id,c.status,c.branch_id,c.external_id AS id "
            "FROM alfa_customers c JOIN alfa_sync_control s ON s.workspace_id=c.workspace_id AND s.published_id=c.run_id "
            "JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND b.connection_id=s.connection_id "
            "AND b.external_id=c.branch_id AND b.available WHERE c.workspace_id=:w AND s.connection_id=:conn "
            "AND c.branch_id=:b AND c.external_id=:customer AND c.contact_id=CAST(:contact AS uuid) "
            "AND c.student_id=CAST(:student AS uuid)"),
            {"w": workspace, "conn": job["connection"], "b": int(job["branch"]),
             "customer": int(job["customer"]), "contact": job["contact"], "student": job["student"]}).mappings().one_or_none()

    def requeue_current(self, db, workspace, job, owned=None):
        """Retire an obsolete source and preserve an operator's refresh request.

        Directory publication and Customer IDs may change while AlfaCRM IO is in
        flight. The old source is fenced, then the same visible conversation is
        resolved against the newest published directory and enqueued again.
        """
        owned = dict(owned or job)
        manual = bool(owned.get("manual") or job.get("manual"))
        force = bool(owned.get("force_at") or job.get("force_at"))
        db.execute(text("UPDATE outbox_events SET state='published',payload=(payload-'lease_owner'-'lease_until'"
            "-'error'-'checkpoint'-'paused'-'cause'-'http_status'-'failed_at'-'stage'-'page')"
            "||jsonb_build_object('available_at',now(),'attempts',0,'rebased_at',CAST(clock_timestamp() AS text)) "
            "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:owner"),
            {"w": workspace, "id": job["id"], "owner": job["lease_owner"]})
        if not manual:
            return
        from center_messenger.crm_api import DirectoryReader
        snapshot = DirectoryReader._published(db, workspace)
        if not snapshot:
            return
        try:
            source_contact = UUID(owned.get("source_contact") or job["contact"])
            conversation = UUID(owned["conversation"]) if owned.get("conversation") else None
        except (TypeError, ValueError):
            return
        card_preference.refresh(db, workspace, snapshot, [source_contact])
        selection = card_preference.selected(db, workspace, snapshot, conversation, source_contact)
        targets = selection["contacts"] if selection else [source_contact]
        for target in targets:
            enqueue(db, workspace, snapshot, contact=target, force=force,
                    source_contact=source_contact, conversation=conversation,
                    manual_context=owned)

    def process(self, workspace, job):
        if job["topic"] == GROUPS:
            return group_links.process(self, workspace, job)
        # Short transactions before/after IO. Workspace/settings changes fence
        # publication, and a stale worker can never publish through a stolen lease.
        paged = None
        try:
            with self.accounts.transaction(workspace_id=workspace) as db:
                owned = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id "
                    "AND state='pending' AND payload->>'lease_owner'=:owner FOR UPDATE"),
                    {"w": workspace, "id": job["id"], "owner": job["lease_owner"]}).scalar_one_or_none()
                if owned is None:
                    return
                if card_jobs.blocked(db, workspace, job):
                    db.execute(text("UPDATE outbox_events SET payload=payload-'lease_owner'-'lease_until' "
                        "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:o"),
                        {"w": workspace, "id": job["id"], "o": job["lease_owner"]})
                    return
                # Explicit opening can promote a queued automatic request,
                # including after the last pending message has been answered.
                job = {**job, "manual": owned.get("manual", False), "force_at": owned.get("force_at")}
                settings = self.connections._row(db, workspace)
                source = self._source(db, workspace, job)
                from center_messenger.crm_api import DirectoryReader
                snapshot = DirectoryReader._published(db, workspace)
                policy = card_loader_settings.get(db, workspace)
                pending = (policy["preload_unanswered"] and
                           card_preference.is_pending(db, workspace, snapshot, [UUID(job["contact"])]) if snapshot else False)
                if (not settings or not settings["secret_ciphertext"] or
                    str(settings["connection_id"]) != job["connection"] or settings["version"] != job["version"] or
                    not source or not (job.get("manual") or (pending or (policy["preload_active_cards"] and source["status"] == "client"))
                    and (not policy["automatic_clients_only"] or source["status"] == "client"))):
                    self.requeue_current(db, workspace, job, owned)
                    return
                settings, source = dict(settings), dict(source)
                saved = db.execute(text("SELECT * FROM alfa_family_details WHERE workspace_id=:w AND connection_id=:conn "
                    "AND branch_id=:b AND customer_id=:customer AND settings_version=:v"),
                    {"w": workspace, "conn": job["connection"], "b": int(job["branch"]),
                     "customer": int(job["customer"]), "v": job["version"]}).mappings().one_or_none()
                if job["topic"] != TOPIC and saved is None:
                    self.finish(db, workspace, job)
                    return
                column = {TOPIC: "refreshed_at", LESSONS: "lessons_refreshed_at", HISTORY: "lessons_full_refreshed_at"}[job["topic"]]
                interval_minutes = policy[SECTION_META[job["topic"]][3]]
                interval = timedelta(minutes=interval_minutes) if interval_minutes else None
                metadata_stale = saved and (any("subject_ids" not in item or "lesson_type_ids" not in item
                    for item in saved["subscriptions"]) if job["topic"] == TOPIC else job["topic"] == HISTORY and
                    any(any(field not in item for field in ("branch_id", "subject_id", "lesson_type_id", "customer_id", "customer_verified"))
                        for item in saved["lessons"]))
                if (saved and saved[column] and (interval is None or saved[column] + interval > datetime.now(UTC))
                    and not metadata_stale and not job.get("force_at")):
                    self.finish(db, workspace, job)
                    return
                today = db.execute(text("SELECT CAST(now() AT TIME ZONE timezone AS date) FROM workspaces WHERE id=:w"),
                                   {"w": workspace}).scalar_one()
                checkpoint = self.checkpoint(job, owned, snapshot, today,
                    started_at=db.execute(text('SELECT clock_timestamp()')).scalar_one())
                if job['topic'] == TOPIC and 'identity_observed_at' not in checkpoint:
                    checkpoint['identity_observed_at'] = checkpoint['started_at']
                today = date.fromisoformat(checkpoint["today"])
                if job["topic"] == HISTORY and "history_only" not in checkpoint:
                    recent_at = saved["lessons_refreshed_at"] if saved else None
                    checkpoint["history_only"] = bool(recent_at and not metadata_stale
                        and recent_at + timedelta(minutes=policy["lessons_interval_minutes"]) > datetime.fromisoformat(checkpoint["started_at"])
                        and recent_at.astimezone(ZoneInfo(snapshot.get("timezone", "UTC"))).date() == today)
            started_at = datetime.fromisoformat(checkpoint["started_at"])
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials,
                    card_queue.request_gate(self.accounts, workspace, policy, job, gate))
                client = _TariffCache(client, self.tariffs, (str(workspace), job["connection"], job["version"]))
                client = paged = self.checkpoint_client(client, workspace, job, checkpoint, settings, credentials,
                                                        limit=policy["requests_per_turn"])
                source["branch_id"] = int(job["branch"])
                observed_customers = []
                if job["topic"] == TOPIC:
                    data = fetch_family_finance(client, int(job["branch"]), int(job["customer"]), source_customer=source,
                        on_customer=lambda raw, archived: observed_customers.append((raw, archived)))
                else:
                    data = fetch_family_lessons(client, int(job["branch"]), int(job["customer"]),
                                                today=today, full=job["topic"] == HISTORY,
                                                history_only=checkpoint.get("history_only", False))
                with self.accounts.transaction(workspace_id=workspace) as db:
                    # Same lock order as existing settings/message writers.
                    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                    current = self.connections._row(db, workspace)
                    published = DirectoryReader._published(db, workspace)
                    own = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id AND "
                        "state='pending' AND payload->>'lease_owner'=:owner AND (payload->>'lease_until')::timestamptz>now() "
                        "FOR UPDATE"), {"w": workspace, "id": job["id"], "owner": job["lease_owner"]}).scalar_one_or_none()
                    if own is None:
                        return
                    if (not current or not current["secret_ciphertext"] or current["version"] != settings["version"] or
                        str(current["connection_id"]) != job["connection"] or not published or published["run"] != snapshot["run"]
                        or not self._source(db, workspace, job)):
                        self.requeue_current(db, workspace, job, own)
                        return
                    params = {"w": workspace, "conn": job["connection"], "b": int(job["branch"]),
                              "id": int(job["customer"]), "v": job["version"], "observed": started_at}
                    if job["topic"] == TOPIC:
                        from center_messenger.active_review import apply_customer
                        for raw, archived in observed_customers:
                            apply_customer(db, workspace, settings, snapshot, dict(raw, _branch=int(job['branch'])),
                                archive=archived, observed_at=datetime.fromisoformat(checkpoint['identity_observed_at']), source='card')
                        db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,"
                            "settings_version,balance,paid_lesson_count,subscriptions,refreshed_at) VALUES "
                            "(:w,:conn,:b,:id,:v,:balance,:paid,CAST(:subs AS jsonb),:observed) "
                            "ON CONFLICT (workspace_id,connection_id,branch_id,customer_id) DO UPDATE SET "
                            "balance=EXCLUDED.balance,paid_lesson_count=EXCLUDED.paid_lesson_count,"
                            "subscriptions=EXCLUDED.subscriptions,refreshed_at=EXCLUDED.refreshed_at,"
                            "lessons=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons ELSE '[]'::jsonb END,"
                            "lessons_refreshed_at=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons_refreshed_at END,"
                            "lessons_full_refreshed_at=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons_full_refreshed_at END,"
                            "lessons_history_through=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons_history_through END,"
                            "settings_version=EXCLUDED.settings_version WHERE alfa_family_details.settings_version<>:v "
                            "OR alfa_family_details.refreshed_at<=:observed"),
                            params | {"balance": data["balance"], "paid": data["paid_lesson_count"], "subs": json.dumps(data["subscriptions"])})
                        from center_messenger.crm_api import DirectoryReader
                        snapshot = DirectoryReader._published(db, workspace)
                        if snapshot and snapshot["connection_id"] == settings["connection_id"] and snapshot["version"] == settings["version"]:
                            enqueue(db, workspace, snapshot, contact=job["contact"] if own.get("manual") else None,
                                    force=bool(job.get("force_at")), topics=(LESSONS, HISTORY),
                                    source_contact=own.get("source_contact"), conversation=own.get("conversation"),
                                    manual_context=own)
                    else:
                        cached = db.execute(text("SELECT lessons,lessons_refreshed_at,lessons_full_refreshed_at FROM alfa_family_details "
                            "WHERE workspace_id=:w AND connection_id=:conn AND branch_id=:b AND customer_id=:id "
                            "AND settings_version=:v FOR UPDATE"), params).mappings().one_or_none()
                        clock = "lessons_full_refreshed_at" if job["topic"] == HISTORY else "lessons_refreshed_at"
                        if cached is not None and (cached[clock] is None or cached[clock] <= started_at):
                            history_only = job["topic"] == HISTORY and (checkpoint.get("history_only", False)
                                or bool(cached["lessons_refreshed_at"] and cached["lessons_refreshed_at"] > started_at))
                            incoming = [dict(item, observed_at=started_at.isoformat()) for item in data]
                            lessons = merge_family_lessons(cached["lessons"], incoming, today=today,
                                full=job["topic"] == HISTORY, history_only=history_only)
                            db.execute(text("UPDATE alfa_family_details SET lessons=CAST(:data AS jsonb),"
                                "lessons_refreshed_at=CASE WHEN :history_only THEN lessons_refreshed_at ELSE :observed END,"
                                "lessons_full_refreshed_at=CASE WHEN :full THEN :observed ELSE lessons_full_refreshed_at END,"
                                "lessons_history_through=CASE WHEN :full THEN CAST(:through AS date) ELSE lessons_history_through END "
                                "WHERE workspace_id=:w AND connection_id=:conn AND branch_id=:b AND customer_id=:id AND settings_version=:v"),
                                params | {"data": json.dumps(lessons), "full": job["topic"] == HISTORY,
                                    "history_only": history_only, "through": today + timedelta(days=-31 if history_only else FUTURE_DAYS)})
                    db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:secret WHERE workspace_id=:w AND version=:v"),
                        {"w": workspace, "v": settings["version"], "secret": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})
                    card_jobs.clear_verified_denial(db, workspace, job)
                    card_preference.refresh_for_contact(db, workspace, snapshot, job['contact'])
                    self.finish(db, workspace, job)
        except card_queue.Deferred as error:
            self.retry(workspace, job, error=error, paged=paged)
        except card_jobs.PageYield:
            self.yield_job(workspace, job)
        except card_jobs.LostCardLease:
            self.yield_job(workspace, job)
        except AuthError as error:
            self.retry(workspace, job, error=error, busy=error.status == 409, paged=paged)
        except AlfaError as error:
            self.retry(workspace, job, error=error, paged=paged)
        except Exception as error:
            # Never log source/customer details or credentials.
            logging.getLogger(__name__).warning("CRM card refresh will resume exception=%s", type(error).__name__)
            self.retry(workspace, job, error=error, paged=paged)

    def finish(self, db, workspace, job):
        # A force refresh requested during IO must survive this worker's finish.
        now = datetime.now(UTC)
        started = _parse_time(job.get("started_at")) or now
        finished = db.execute(text("UPDATE outbox_events SET state=CASE WHEN (payload->>'force_at') IS DISTINCT FROM CAST(:force AS text) "
            "THEN 'pending' ELSE 'published' END,payload=(payload-'lease_owner'-'lease_until'-'error'-'checkpoint' "
            "-'paused'-'cause'-'http_status'-'failed_at'-'stage'-'page')||jsonb_build_object('available_at',now(),'attempts',0) "
            "||jsonb_build_object('finished_at',CAST(:finished AS text),'duration_ms',CAST(:duration AS integer)) "
            "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:owner "
            "AND (payload->>'lease_until')::timestamptz>now()"),
            {"w": workspace, "id": job["id"], "owner": job["lease_owner"], "force": job.get("force_at"),
             "finished": now.isoformat(), "duration": max(0, int((now - started).total_seconds() * 1000))})

        if finished.rowcount:
            self.publish_snapshots(db, workspace, job)

    def publish_snapshots(self, db, workspace, job):
        if db.execute(text("SELECT EXISTS(SELECT 1 FROM outbox_events WHERE workspace_id=:w "
            "AND state='pending' AND topic=ANY(CAST(:topics AS text[])) "
            "AND payload->>'contact'=:contact AND payload->>'connection'=:conn AND payload->>'version'=:version)"),
            {'w':workspace, 'topics':list(TOPICS), 'contact':job['contact'], 'conn':job['connection'],
             'version':str(job['version'])}).scalar_one():
            return
        from center_messenger.crm_api import DirectoryReader
        reader = DirectoryReader(self.connections)
        # Plain directory cards and verified multi-contact chat families each
        # have their own exact Customer ID/student/branch scope.
        targets = [(UUID(job['contact']), None)]
        rows = db.execute(text("SELECT c.contact_id,c.id FROM conversations c JOIN connections cn "
            "ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id WHERE c.workspace_id=:w "
            "AND c.kind='direct' AND cn.kind IN ('alfa_max_browser','max_bot') AND (c.contact_id=CAST(:contact AS uuid) "
            "OR c.contact_id IN (SELECT (payload->>'source')::uuid FROM outbox_events WHERE workspace_id=:w "
            "AND topic='crm.card.preference' AND state='published' AND payload->'contacts' ? CAST(:contact AS text))) LIMIT 100"),
            {'w': workspace, 'contact': job['contact']}).all()
        targets.extend((row[0], row[1]) for row in rows)
        for contact, conversation in targets:
            try:
                with db.begin_nested():
                    reader._detail(db, workspace, contact, conversation=conversation, publish_snapshot=True)
            except AuthError as error:
                if error.status != 404:
                    raise

    def retry(self, workspace, job, *, busy=False, error=None, paged=None):
        with self.accounts.transaction(workspace_id=workspace) as db:
            policy = card_loader_settings.get(db, workspace)
            patch, scope = card_jobs.failure(error, job.get("attempts", 0), busy=busy, policy=policy)
            code = patch["error"]
            if busy or code in {"quota_wait", "send_pending"}:
                patch.pop("error")
                patch["cause"] = "busy" if busy else code
            patch.update(stage=paged.stage if paged else "prepare", page=paged.page if paged else None)
            patch["last_progress_at"] = datetime.now(UTC).isoformat()
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            settings = self.connections._row(db, workspace)
            if not settings or settings["version"] != job["version"] or str(settings["connection_id"]) != job["connection"]:
                owned = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id "
                    "AND state='pending' AND payload->>'lease_owner'=:owner FOR UPDATE"),
                    {"w": workspace, "id": job["id"], "owner": job["lease_owner"]}).scalar_one_or_none()
                if owned:
                    self.requeue_current(db, workspace, job, owned)
                return
            written = db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until'-'error' "
                + ("-'checkpoint' " if code in {"not_found", "invalid_response"} else "") + ")||CAST(:p AS jsonb) "
                "WHERE workspace_id=:w AND id=:id AND state='pending' AND payload->>'lease_owner'=:owner "
                "AND (payload->>'lease_until')::timestamptz>now() RETURNING id"),
                {"w": workspace, "id": job["id"], "owner": job["lease_owner"], "p": json.dumps(patch)}).scalar_one_or_none()
            if written:
                card_jobs.save_block(db, workspace, job, patch, scope)

    def run_next(self):
        """One workspace/job; called by the shared scheduler, without a loop."""
        with self._schedule_lock:
            pending = getattr(self, "_pending_scopes", [])
            if not pending:
                with self.accounts.transaction() as db:
                    pending = list(db.execute(text("SELECT workspace_id FROM max_routes UNION "
                        "SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars())
                self._pending_scopes = pending
            if not pending:
                return False
            workspace = pending.pop(0)
            scheduled = getattr(self, "_scheduled_scopes", {})
            if time.monotonic() >= scheduled.get(workspace, 0):
                self.schedule(workspace)
                scheduled[workspace] = time.monotonic() + 10
                self._scheduled_scopes = scheduled
            job = self.claim(workspace)
        if job:
            self.process(workspace, job)
        return bool(job)

    def run(self):
        scheduled = 0
        while not self.stopping.is_set():
            try:
                with self.accounts.transaction() as db:
                    workspaces = list(db.execute(text("SELECT workspace_id FROM max_routes UNION "
                        "SELECT workspace_id FROM alfa_max_sync_queue")).scalars())
                if time.monotonic() >= scheduled:
                    for workspace in workspaces:
                        self.schedule(workspace)
                    scheduled = time.monotonic() + 10
                for workspace in workspaces:
                    if self.stopping.is_set():
                        break
                    job = self.claim(workspace)
                    if job:
                        self.process(workspace, job)
            except Exception:
                logging.getLogger(__name__).warning("CRM card cache worker will resume")
            # Yield the shared CRM host budget to new messages and sync workers.
            self.stopping.wait(2)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=25)
