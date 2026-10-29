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
from center_messenger import group_links, card_preference, card_jobs

TOPIC = "crm.card.refresh"
LESSONS = "crm.card.lessons"
HISTORY = "crm.card.lesson-history"
GROUPS = group_links.TOPIC
TOPICS = (TOPIC, LESSONS, HISTORY, GROUPS)
INTERVAL = timedelta(minutes=30)
HISTORY_INTERVAL = timedelta(days=1)
LEASE = timedelta(seconds=90)
SUBSCRIPTIONS_STALE_SQL = """EXISTS(SELECT 1 FROM jsonb_array_elements(f.subscriptions) item
    WHERE NOT(item ? 'subject_ids') OR NOT(item ? 'lesson_type_ids'))"""
LESSONS_STALE_SQL = """EXISTS(SELECT 1 FROM jsonb_array_elements(f.lessons) item
    WHERE NOT(item ? 'branch_id') OR NOT(item ? 'subject_id') OR NOT(item ? 'lesson_type_id')
      OR NOT(item ? 'customer_id') OR NOT(item ? 'customer_verified'))"""
MAPPING_STALE_SQL = "(" + SUBSCRIPTIONS_STALE_SQL + " OR " + LESSONS_STALE_SQL + ")"


def enqueue(db, workspace, snapshot, *, contact=None, force=False, topics=TOPICS):
    """Independent queues: finances first, recent lessons, then the daily history.

    All three keep their own leases and retry clocks. A failed history request
    cannot hold the next balance refresh. Read clients require explicit opening.
    """
    count = 0
    for topic, column, interval, stale in (
        (TOPIC, "refreshed_at", "30 minutes", SUBSCRIPTIONS_STALE_SQL),
        (LESSONS, "lessons_refreshed_at", "30 minutes", "false"),
        (HISTORY, "lessons_full_refreshed_at", "1 day", LESSONS_STALE_SQL),
    ):
        if topic not in topics:
            continue
        count += _enqueue(db, workspace, snapshot, contact=contact, force=force,
                         topic=topic, column=column, interval=interval, stale=stale)
    if GROUPS in topics:
        count += group_links.enqueue(db, workspace, snapshot, contact=contact, force=force)
    return count


def _enqueue(db, workspace, snapshot, *, contact, force, topic, column, interval, stale):
    # SQL fragments below come only from the fixed policy in enqueue().
    return db.execute(text("WITH pending_families AS MATERIALIZED (" + card_preference.pending_sql(snapshot) + ") " + f"""
      INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state)
      SELECT :w,gen_random_uuid(),:topic,concat(:conn,':',c.branch_id,':',c.external_id),
        jsonb_build_object('connection',CAST(:conn AS text),'branch',c.branch_id::text,
          'customer',c.external_id::text,'contact',c.contact_id::text,'student',c.student_id::text,
          'version',:version,'manual',:manual,'due_at',coalesce(f.{column}+interval '{interval}',now()),
          'force_at',CASE WHEN :force THEN CAST(clock_timestamp() AS text) ELSE NULL END,
          'available_at',now(),'attempts',0),'pending'
      FROM alfa_customers c JOIN alfa_branches b ON b.workspace_id=c.workspace_id
        AND b.connection_id=:conn AND b.external_id=c.branch_id AND b.available
      LEFT JOIN alfa_family_details f ON f.workspace_id=c.workspace_id AND f.connection_id=:conn
        AND f.branch_id=c.branch_id AND f.customer_id=c.external_id AND f.settings_version=:version
      WHERE c.workspace_id=:w AND c.run_id=:run AND c.contact_id IS NOT NULL AND c.student_id IS NOT NULL
        AND (CAST(:contact AS uuid) IS NULL OR c.contact_id=CAST(:contact AS uuid))
        AND (:manual OR c.contact_id IN (SELECT contact_id FROM pending_families))
        AND (:finance OR f.customer_id IS NOT NULL)
        AND (:force OR f.{column} IS NULL OR f.{column}<=now()-interval '{interval}' OR {stale})
      ON CONFLICT (workspace_id,topic,event_key) DO UPDATE SET
        state='pending',payload=CASE WHEN outbox_events.state='pending'
          AND outbox_events.payload->>'version'=EXCLUDED.payload->>'version'
          AND outbox_events.payload->>'contact'=EXCLUDED.payload->>'contact'
          AND outbox_events.payload->>'student'=EXCLUDED.payload->>'student'
          THEN outbox_events.payload||jsonb_build_object(
            'manual',coalesce((outbox_events.payload->>'manual')::boolean,false) OR :manual,
            'force_at',CASE WHEN :force THEN EXCLUDED.payload->>'force_at' ELSE outbox_events.payload->>'force_at' END)
          ELSE EXCLUDED.payload END
      WHERE (outbox_events.state<>'pending' AND coalesce((outbox_events.payload->>'lease_until')::timestamptz,'epoch')<=now())
        OR outbox_events.payload->>'version'<>EXCLUDED.payload->>'version'
        OR outbox_events.payload->>'contact'<>EXCLUDED.payload->>'contact'
        OR outbox_events.payload->>'student'<>EXCLUDED.payload->>'student'
        OR (:manual AND outbox_events.payload->>'manual' IS DISTINCT FROM 'true')
        OR :force
      """), {"w": workspace, "topic": topic, "conn": snapshot["connection_id"],
               "run": snapshot["run"], "version": snapshot["version"],
               "contact": contact, "manual": contact is not None, "force": force,
               "finance": topic == TOPIC, **card_preference.params(snapshot)}).rowcount


def cache_status(db, workspace, connection, cards, contact, version, *, contacts=None, snapshot=None):
    contacts = list(contacts or [contact])
    sources = [card for student in cards for card in student["cards"]]
    times = [datetime.fromisoformat(card["refreshed_at"]) for card in sources if card.get("refreshed_at")]
    jobs = list(db.execute(text("SELECT (payload-'checkpoint')||jsonb_build_object('topic',topic) FROM outbox_events WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history,:groups) "
        "AND state='pending' AND payload->>'connection'=:conn AND payload->>'version'=:v AND payload->>'contact'=ANY(CAST(:contacts AS text[]))"),
        {"w": workspace, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY, "groups": GROUPS,
         "conn": str(connection), "v": str(version), "contacts": list(map(str, contacts))}).scalars())
    keys = {(card["branch_id"], card["id"]) for card in sources}
    jobs = [job for job in jobs if (job["branch"], job["customer"]) in keys]
    active_blocks = card_jobs.blocks(db, workspace, connection, version) if jobs else []
    jobs = [card_jobs.effective_job(job, active_blocks) for job in jobs]
    runnable = [job for job in jobs if not job.get("paused")]
    now = datetime.now(UTC)
    refreshing = any((job.get("lease_until") and datetime.fromisoformat(job["lease_until"])>now) or
                     (job.get("available_at") and datetime.fromisoformat(job["available_at"])<=now) for job in runnable)
    retries = [job["available_at"] for job in runnable if job.get("available_at") and job.get("error")]
    failures = [{"code": job["error"], "http_status": job.get("http_status"), "cause": job.get("cause"),
                 "operation": job["topic"], "branch_id": job["branch"], "customer_id": job["customer"],
                 "stage": job.get("stage"), "page": job.get("page"), "paused": bool(job.get("paused")),
                 "retry_at": job.get("available_at"), "text": (
                     "Не удалось обновить баланс и абонементы. " if job["topic"] == TOPIC else
                     "Не удалось обновить группы. " if job["topic"] == GROUPS else
                     "Не удалось обновить занятия. Баланс и абонементы обновляются отдельно. ")
                     + card_jobs.LABELS.get(job["error"], card_jobs.LABELS["crm_unavailable"])}
                for job in jobs if job.get("error")]
    ready = len(times) == len(sources) and bool(sources)
    mapping_pending = any(card.get("needs_mapping_refresh") for card in sources)
    updated = min(times) if ready else None
    def oldest(field):
        values = [datetime.fromisoformat(card[field]) for card in sources if card.get(field)]
        return min(values) if len(values) == len(sources) and values else None
    lessons_at, history_at = oldest("lessons_refreshed_at"), oldest("lessons_full_refreshed_at")
    groups_at = oldest("groups_refreshed_at")
    automatic = card_preference.is_pending(db, workspace, snapshot, contacts) if snapshot else db.execute(text(
        "SELECT EXISTS(SELECT 1 FROM conversations WHERE workspace_id=:w AND contact_id=:c AND needs_reply AND kind='direct')"),
        {"w": workspace, "c": contact}).scalar_one()
    return {"auto_refresh": bool(automatic), "ready": ready, "refreshing": bool(refreshing), "pending": bool(jobs), "updated_at": updated.isoformat() if updated else None,
            "paused": any(job.get("paused") for job in jobs), "failures": failures,
            "next_refresh_at": (updated + INTERVAL).isoformat() if updated else None,
            "interval_minutes": 30, "sources": len(sources), "cached_sources": len(times),
            "mapping_pending": mapping_pending,
            "lessons_updated_at": lessons_at.isoformat() if lessons_at else None,
            "lessons_history_updated_at": history_at.isoformat() if history_at else None,
            "lessons_history_interval_hours": 24,
            "groups_updated_at": groups_at.isoformat() if groups_at else None,
            "groups_interval_hours": 6,
            "groups_refreshing": any(job["topic"] == GROUPS for job in runnable),
            "lessons_refreshing": any(job["topic"] == LESSONS for job in runnable),
            "lessons_history_refreshing": any(job["topic"] == HISTORY for job in runnable),
            "overdue": not ready or mapping_pending or updated + INTERVAL <= datetime.now(UTC)
                       or lessons_at is None or lessons_at + INTERVAL <= datetime.now(UTC),
            "retry_at": min(retries, default=None),
            "error": failures[0]["text"] if failures else None}


def current(db, workspace, connection):
    """Only a leased, current-source job can be displayed as processing now."""
    row = db.execute(text("""
      SELECT e.topic,e.payload-'checkpoint' AS payload,c.name AS student_name,coalesce(nullif(c.legal_name,''),d.display_name,c.name) AS name
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
            "manual": bool(job.get("manual")), "operation": {
                TOPIC: "reading_finance", LESSONS: "reading_recent_lessons", HISTORY: "reading_lesson_history", GROUPS: "reading_customer_groups"
            }[row["topic"]]}


def progress(db, workspace, connection, snapshot):
    """Count families needing an answer, without multiplying branch source cards."""
    at = datetime.now(UTC).isoformat()
    empty = {"configured": bool(snapshot), "total": 0, "ready": 0, "cached": 0,
             "queued": 0, "errors": 0, "next_retry_at": None, "interval_minutes": 30, "as_of": at}
    if not snapshot or snapshot["connection_id"] != connection:
        return empty
    row = db.execute(text("""
      WITH pending_families AS MATERIALIZED (""" + card_preference.pending_sql(snapshot) + """), families AS (
        SELECT c.contact_id,
          bool_and(f.customer_id IS NOT NULL) AS cached,
          bool_and(f.customer_id IS NOT NULL AND f.refreshed_at>now()-interval '30 minutes'
            AND f.lessons_refreshed_at>now()-interval '30 minutes'
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
             **card_preference.params(snapshot)}).mappings().one()
    return empty | dict(row) | {"groups": group_links.progress(db, workspace, snapshot)}


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
        self.cache[key] = (time.monotonic() + INTERVAL.total_seconds(), data)
        return data


class FamilyCacheWorker:
    def __init__(self, connections):
        self.connections, self.accounts = connections, connections.accounts
        self.stopping = threading.Event()
        self.owner, self.tariffs = str(uuid4()), {}
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

    def claim(self, workspace):
        quota = (TOPIC, LESSONS, TOPIC, HISTORY, GROUPS)
        turns = getattr(self, "_turns", {})
        preferred = quota[turns.get(workspace, 0) % len(quota)]
        with self.accounts.transaction(workspace_id=workspace) as db:
            row = db.execute(text("SELECT e.id,e.topic,e.payload FROM outbox_events e WHERE e.workspace_id=:w "
                "AND e.topic IN (:finance,:lessons,:history,:groups) AND e.state='pending' AND ("
                "(e.payload->>'available_at')::timestamptz<=now() OR (e.payload->>'error' IN "
                "('access_denied','not_found','invalid_response','crm_unavailable') AND "
                "(e.payload->>'force_at')::timestamptz>(e.payload->>'failed_at')::timestamptz)) "
                "AND coalesce((e.payload->>'lease_until')::timestamptz,'epoch')<=now() AND NOT " + card_jobs.block_sql() +
                " ORDER BY (coalesce((e.payload->>'served_at')::timestamptz,e.created_at)<now()-interval '15 minutes') DESC,"
                "coalesce((e.payload->>'manual')::boolean,false) DESC,(e.topic=:preferred) DESC,"
                "CASE e.topic WHEN :finance THEN 0 WHEN :lessons THEN 1 WHEN :history THEN 2 ELSE 3 END,"
                "coalesce((e.payload->>'served_at')::timestamptz,e.created_at),e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED"),
                {"w": workspace, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY,
                 "groups": GROUPS, "preferred": preferred}).mappings().one_or_none()
            if not row:
                return None
            turns[workspace] = turns.get(workspace, 0) + 1
            self._turns = turns
            job = dict(row["payload"])
            job.update(lease_owner=self.owner, lease_until=(datetime.now(UTC) + LEASE).isoformat(),
                       started_at=datetime.now(UTC).isoformat(), served_at=datetime.now(UTC).isoformat())
            db.execute(text("UPDATE outbox_events SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                       {"w": workspace, "id": row["id"], "p": json.dumps(job)})
            return {**job, "id": row["id"], "topic": row["topic"]}

    def checkpoint(self, job, owned, snapshot, today, *, started_at=None):
        scope = [job["connection"], job["version"], str(snapshot["run"]), job["branch"],
                 job["customer"], job["contact"], job["student"], job["topic"], job.get("force_at")]
        saved = owned.get("checkpoint") or {}
        if saved.get("scope") != scope:
            saved = {"scope": scope, "today": today.isoformat(), "started_at": (started_at or datetime.now(UTC)).isoformat(),
                     "responses": {}, "pages": 0}
        return saved

    def checkpoint_client(self, client, workspace, job, checkpoint, settings, credentials):
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
                    "'checkpoint',CAST(:p AS jsonb),'stage',CAST(:stage AS text),'page',CAST(:page AS integer)) WHERE workspace_id=:w AND id=:id "
                    "AND state='pending' AND payload->>'lease_owner'=:owner "
                    "AND (payload->>'lease_until')::timestamptz>now() RETURNING id"),
                    {"w": workspace, "id": job["id"], "owner": self.owner, "p": json.dumps(value),
                     "stage": value.get("stage", "prepare"), "page": value.get("page")}).scalar_one_or_none()
                if not written:
                    raise card_jobs.LostCardLease()
                db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:s WHERE workspace_id=:w AND version=:v"),
                    {"w": workspace, "v": job["version"],
                     "s": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})
        return card_jobs.CheckpointClient(client, checkpoint, persist)

    def yield_job(self, workspace, job):
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until'-'error'-'paused')"
                "||jsonb_build_object('available_at',now()) WHERE workspace_id=:w AND id=:id AND state='pending' "
                "AND payload->>'lease_owner'=:owner AND (payload->>'lease_until')::timestamptz>now()"),
                {"w": workspace, "id": job["id"], "owner": self.owner})

    def _source(self, db, workspace, job):
        return db.execute(text("SELECT c.contact_id,c.student_id,c.status,c.branch_id,c.external_id AS id "
            "FROM alfa_customers c JOIN alfa_sync_control s ON s.workspace_id=c.workspace_id AND s.published_id=c.run_id "
            "JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND b.connection_id=s.connection_id "
            "AND b.external_id=c.branch_id AND b.available WHERE c.workspace_id=:w AND s.connection_id=:conn "
            "AND c.branch_id=:b AND c.external_id=:customer AND c.contact_id=CAST(:contact AS uuid) "
            "AND c.student_id=CAST(:student AS uuid)"),
            {"w": workspace, "conn": job["connection"], "b": int(job["branch"]),
             "customer": int(job["customer"]), "contact": job["contact"], "student": job["student"]}).mappings().one_or_none()

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
                    {"w": workspace, "id": job["id"], "owner": self.owner}).scalar_one_or_none()
                if owned is None:
                    return
                if card_jobs.blocked(db, workspace, job):
                    db.execute(text("UPDATE outbox_events SET payload=payload-'lease_owner'-'lease_until' "
                        "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:o"),
                        {"w": workspace, "id": job["id"], "o": self.owner})
                    return
                # Explicit opening can promote a queued automatic request,
                # including after the last pending message has been answered.
                job = {**job, "manual": owned.get("manual", False), "force_at": owned.get("force_at")}
                settings = self.connections._row(db, workspace)
                source = self._source(db, workspace, job)
                from center_messenger.crm_api import DirectoryReader
                snapshot = DirectoryReader._published(db, workspace)
                pending = card_preference.is_pending(db, workspace, snapshot, [UUID(job["contact"])]) if snapshot else False
                if (not settings or not settings["secret_ciphertext"] or
                    str(settings["connection_id"]) != job["connection"] or settings["version"] != job["version"] or
                    not source or not (job.get("manual") or pending)):
                    self.finish(db, workspace, job)
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
                interval = HISTORY_INTERVAL if job["topic"] == HISTORY else INTERVAL
                metadata_stale = saved and (any("subject_ids" not in item or "lesson_type_ids" not in item
                    for item in saved["subscriptions"]) if job["topic"] == TOPIC else job["topic"] == HISTORY and
                    any(any(field not in item for field in ("branch_id", "subject_id", "lesson_type_id", "customer_id", "customer_verified"))
                        for item in saved["lessons"]))
                if saved and saved[column] and saved[column] + interval > datetime.now(UTC) and not metadata_stale and not job.get("force_at"):
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
                        and recent_at + INTERVAL > datetime.fromisoformat(checkpoint["started_at"])
                        and recent_at.astimezone(ZoneInfo(snapshot.get("timezone", "UTC"))).date() == today)
            started_at = datetime.fromisoformat(checkpoint["started_at"])
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
                client = _TariffCache(client, self.tariffs, (str(workspace), job["connection"], job["version"]))
                client = paged = self.checkpoint_client(client, workspace, job, checkpoint, settings, credentials)
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
                        "FOR UPDATE"), {"w": workspace, "id": job["id"], "owner": self.owner}).scalar_one_or_none()
                    if own is None:
                        return
                    if (not current or not current["secret_ciphertext"] or current["version"] != settings["version"] or
                        str(current["connection_id"]) != job["connection"] or not published or published["run"] != snapshot["run"]
                        or not self._source(db, workspace, job)):
                        self.finish(db, workspace, job)
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
                                    force=bool(job.get("force_at")), topics=(LESSONS, HISTORY))
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
        db.execute(text("UPDATE outbox_events SET state=CASE WHEN (payload->>'force_at') IS DISTINCT FROM CAST(:force AS text) "
            "THEN 'pending' ELSE 'published' END,payload=(payload-'lease_owner'-'lease_until'-'error'-'checkpoint' "
            "-'paused'-'cause'-'http_status'-'failed_at'-'stage'-'page')||jsonb_build_object('available_at',now(),'attempts',0) "
            "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:owner "
            "AND (payload->>'lease_until')::timestamptz>now()"),
            {"w": workspace, "id": job["id"], "owner": self.owner, "force": job.get("force_at")})

    def retry(self, workspace, job, *, busy=False, error=None, paged=None):
        patch, scope = card_jobs.failure(error, job.get("attempts", 0), busy=busy)
        code = patch["error"]
        if busy:
            patch.pop("error")
            patch["cause"] = "busy"
        patch.update(stage=paged.stage if paged else "prepare", page=paged.page if paged else None)
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            settings = self.connections._row(db, workspace)
            if not settings or settings["version"] != job["version"] or str(settings["connection_id"]) != job["connection"]:
                self.finish(db, workspace, job)
                return
            written = db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until'-'error' "
                + ("-'checkpoint' " if code in {"not_found", "invalid_response"} else "") + ")||CAST(:p AS jsonb) "
                "WHERE workspace_id=:w AND id=:id AND state='pending' AND payload->>'lease_owner'=:owner "
                "AND (payload->>'lease_until')::timestamptz>now() RETURNING id"),
                {"w": workspace, "id": job["id"], "owner": self.owner, "p": json.dumps(patch)}).scalar_one_or_none()
            if written:
                card_jobs.save_block(db, workspace, job, patch, scope)

    def run_next(self):
        """One workspace/job; called by the shared scheduler, without a loop."""
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
