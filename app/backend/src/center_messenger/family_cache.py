"""Durable, tenant-scoped CRM card refreshes; no operator session is required.

Jobs use the existing transactional outbox and facts use alfa_family_details in
PostgreSQL's persistent volume. A lease, not an open browser, owns each request.
"""
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_family import fetch_family_finance, fetch_family_lessons, merge_family_lessons
from center_messenger.auth_security import AuthError

TOPIC = "crm.card.refresh"
LESSONS = "crm.card.lessons"
HISTORY = "crm.card.lesson-history"
TOPICS = (TOPIC, LESSONS, HISTORY)
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
    return count


def _enqueue(db, workspace, snapshot, *, contact, force, topic, column, interval, stale):
    # SQL fragments below come only from the fixed policy in enqueue().
    return db.execute(text(f"""
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
        AND (:manual OR EXISTS(SELECT 1 FROM conversations q WHERE q.workspace_id=c.workspace_id
          AND q.contact_id=c.contact_id AND q.kind='direct' AND q.needs_reply))
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
               "finance": topic == TOPIC}).rowcount


def cache_status(db, workspace, connection, cards, contact, version):
    sources = [card for student in cards for card in student["cards"]]
    times = [datetime.fromisoformat(card["refreshed_at"]) for card in sources if card.get("refreshed_at")]
    jobs = list(db.execute(text("SELECT payload||jsonb_build_object('topic',topic) FROM outbox_events WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history) "
        "AND state='pending' AND payload->>'connection'=:conn AND payload->>'version'=:v AND payload->>'contact'=:contact"),
        {"w": workspace, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY,
         "conn": str(connection), "v": str(version), "contact": str(contact)}).scalars())
    keys = {(card["branch_id"], card["id"]) for card in sources}
    jobs = [job for job in jobs if (job["branch"], job["customer"]) in keys]
    ready = len(times) == len(sources) and bool(sources)
    mapping_pending = any(card.get("needs_mapping_refresh") for card in sources)
    updated = min(times) if ready else None
    def oldest(field):
        values = [datetime.fromisoformat(card[field]) for card in sources if card.get(field)]
        return min(values) if len(values) == len(sources) and values else None
    lessons_at, history_at = oldest("lessons_refreshed_at"), oldest("lessons_full_refreshed_at")
    automatic = db.execute(text("SELECT EXISTS(SELECT 1 FROM conversations WHERE workspace_id=:w "
        "AND contact_id=:c AND needs_reply AND kind='direct')"), {"w": workspace, "c": contact}).scalar_one()
    return {"auto_refresh": bool(automatic), "ready": ready, "refreshing": bool(jobs), "updated_at": updated.isoformat() if updated else None,
            "next_refresh_at": (updated + INTERVAL).isoformat() if updated else None,
            "interval_minutes": 30, "sources": len(sources), "cached_sources": len(times),
            "mapping_pending": mapping_pending,
            "lessons_updated_at": lessons_at.isoformat() if lessons_at else None,
            "lessons_history_updated_at": history_at.isoformat() if history_at else None,
            "lessons_history_interval_hours": 24,
            "lessons_refreshing": any(job["topic"] == LESSONS for job in jobs),
            "lessons_history_refreshing": any(job["topic"] == HISTORY for job in jobs),
            "overdue": not ready or mapping_pending or updated + INTERVAL <= datetime.now(UTC)
                       or lessons_at is None or lessons_at + INTERVAL <= datetime.now(UTC),
            "retry_at": min((job["available_at"] for job in jobs), default=None),
            "error": "Не удалось обновить баланс и абонементы. Сохранены последние полученные сведения."
                     if any(job.get("error") and job["topic"] == TOPIC for job in jobs) else
                     "Не удалось обновить занятия. Баланс и абонементы обновляются отдельно."
                     if any(job.get("error") for job in jobs) else None}


def current(db, workspace, connection):
    """Only a leased, current-source job can be displayed as processing now."""
    row = db.execute(text("""
      SELECT e.topic,e.payload,c.name AS student_name,coalesce(nullif(c.legal_name,''),d.display_name,c.name) AS name
      FROM outbox_events e JOIN alfa_settings s ON s.workspace_id=e.workspace_id
        AND s.connection_id=:conn AND s.secret_ciphertext IS NOT NULL
      JOIN alfa_sync_control sc ON sc.workspace_id=s.workspace_id AND sc.connection_id=s.connection_id
      JOIN alfa_customers c ON c.workspace_id=s.workspace_id AND c.run_id=sc.published_id
        AND c.branch_id::text=e.payload->>'branch' AND c.external_id::text=e.payload->>'customer'
        AND c.contact_id::text=e.payload->>'contact' AND c.student_id::text=e.payload->>'student'
      JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND b.connection_id=s.connection_id
        AND b.external_id=c.branch_id AND b.available
      LEFT JOIN alfa_directory d ON d.workspace_id=c.workspace_id AND d.run_id=c.run_id AND d.contact_id=c.contact_id
      WHERE e.workspace_id=:w AND e.topic IN (:finance,:lessons,:history) AND e.state='pending'
        AND e.payload->>'connection'=CAST(:conn AS text) AND e.payload->>'version'=s.version::text
        AND e.payload->>'lease_owner' IS NOT NULL AND (e.payload->>'lease_until')::timestamptz>now()
      ORDER BY e.created_at,e.id LIMIT 1
    """), {"w": workspace, "conn": connection, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY}).mappings().one_or_none()
    if not row:
        return None
    job = row["payload"]
    return {"name": row["name"], "student_name": row["student_name"],
            "customer_id": job["customer"], "branch_id": job["branch"],
            "started_at": job.get("started_at"), "lease_until": job["lease_until"],
            "manual": bool(job.get("manual")), "operation": {
                TOPIC: "reading_finance", LESSONS: "reading_recent_lessons", HISTORY: "reading_lesson_history"
            }[row["topic"]]}


def progress(db, workspace, connection, snapshot):
    """Count families needing an answer, without multiplying branch source cards."""
    at = datetime.now(UTC).isoformat()
    empty = {"configured": bool(snapshot), "total": 0, "ready": 0, "cached": 0,
             "queued": 0, "errors": 0, "next_retry_at": None, "interval_minutes": 30, "as_of": at}
    if not snapshot or snapshot["connection_id"] != connection:
        return empty
    row = db.execute(text("""
      WITH families AS (
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
          AND EXISTS(SELECT 1 FROM conversations q WHERE q.workspace_id=c.workspace_id
            AND q.contact_id=c.contact_id AND q.kind='direct' AND q.needs_reply)
        GROUP BY c.contact_id
      ), jobs AS (
        SELECT e.payload FROM outbox_events e JOIN alfa_settings s ON s.workspace_id=e.workspace_id
          AND s.connection_id=:conn AND s.secret_ciphertext IS NOT NULL
        WHERE e.workspace_id=:w AND e.topic IN (:finance,:lessons,:history) AND e.state='pending'
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
             "finance": TOPIC, "lessons": LESSONS, "history": HISTORY}).mappings().one()
    return empty | dict(row)


class _TariffCache:
    """Only branch tariff metadata is shared; client money is always read live."""
    def __init__(self, client, cache, scope):
        self.client, self.cache, self.scope = client, cache, scope

    def call(self, path, body):
        if not path.endswith('/tariff/index'):
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
                enqueue(db, workspace, snapshot)

    def claim(self, workspace):
        with self.accounts.transaction(workspace_id=workspace) as db:
            row = db.execute(text("SELECT id,topic,payload FROM outbox_events WHERE workspace_id=:w AND topic IN (:finance,:lessons,:history) "
                "AND state='pending' AND (payload->>'available_at')::timestamptz<=now() "
                "AND coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now() "
                "ORDER BY (payload->>'manual')::boolean DESC,CASE topic WHEN :finance THEN 0 WHEN :lessons THEN 1 ELSE 2 END,"
                "(payload->>'due_at')::timestamptz,id LIMIT 1 FOR UPDATE SKIP LOCKED"),
                {"w": workspace, "finance": TOPIC, "lessons": LESSONS, "history": HISTORY}).mappings().one_or_none()
            if not row:
                return None
            job = dict(row["payload"])
            job.update(lease_owner=self.owner, lease_until=(datetime.now(UTC) + LEASE).isoformat(),
                       started_at=datetime.now(UTC).isoformat())
            db.execute(text("UPDATE outbox_events SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                       {"w": workspace, "id": row["id"], "p": json.dumps(job)})
            return {**job, "id": row["id"], "topic": row["topic"]}

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
        # Short transactions before/after IO. Workspace/settings changes fence
        # publication, and a stale worker can never publish through a stolen lease.
        try:
            with self.accounts.transaction(workspace_id=workspace) as db:
                owned = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id "
                    "AND state='pending' AND payload->>'lease_owner'=:owner FOR UPDATE"),
                    {"w": workspace, "id": job["id"], "owner": self.owner}).scalar_one_or_none()
                if owned is None:
                    return
                # Explicit opening can promote a queued automatic request,
                # including after the last pending message has been answered.
                job = {**job, "manual": owned.get("manual", False), "force_at": owned.get("force_at")}
                settings = self.connections._row(db, workspace)
                source = self._source(db, workspace, job)
                pending = db.execute(text("SELECT EXISTS(SELECT 1 FROM conversations WHERE workspace_id=:w "
                    "AND contact_id=CAST(:c AS uuid) AND needs_reply AND kind='direct')"),
                    {"w": workspace, "c": job["contact"]}).scalar_one()
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
            started_at = datetime.now(UTC)
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
                client = _TariffCache(client, self.tariffs, (str(workspace), job["connection"], job["version"]))
                source["branch_id"] = int(job["branch"])
                if job["topic"] == TOPIC:
                    data = fetch_family_finance(client, int(job["branch"]), int(job["customer"]), source_customer=source)
                else:
                    data = fetch_family_lessons(client, int(job["branch"]), int(job["customer"]),
                                                today=today, full=job["topic"] == HISTORY)
                with self.accounts.transaction(workspace_id=workspace) as db:
                    # Same lock order as existing settings/message writers.
                    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                    current = self.connections._row(db, workspace)
                    own = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id AND "
                        "state='pending' AND payload->>'lease_owner'=:owner AND (payload->>'lease_until')::timestamptz>now() "
                        "FOR UPDATE"), {"w": workspace, "id": job["id"], "owner": self.owner}).scalar_one_or_none()
                    if own is None:
                        return
                    if (not current or current["version"] != settings["version"] or
                        str(current["connection_id"]) != job["connection"] or not self._source(db, workspace, job)):
                        self.finish(db, workspace, job)
                        return
                    params = {"w": workspace, "conn": job["connection"], "b": int(job["branch"]),
                              "id": int(job["customer"]), "v": job["version"]}
                    if job["topic"] == TOPIC:
                        db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,"
                            "settings_version,balance,paid_lesson_count,subscriptions) VALUES "
                            "(:w,:conn,:b,:id,:v,:balance,:paid,CAST(:subs AS jsonb)) "
                            "ON CONFLICT (workspace_id,connection_id,branch_id,customer_id) DO UPDATE SET "
                            "balance=EXCLUDED.balance,paid_lesson_count=EXCLUDED.paid_lesson_count,"
                            "subscriptions=EXCLUDED.subscriptions,refreshed_at=now(),"
                            "lessons=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons ELSE '[]'::jsonb END,"
                            "lessons_refreshed_at=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons_refreshed_at END,"
                            "lessons_full_refreshed_at=CASE WHEN alfa_family_details.settings_version=:v THEN alfa_family_details.lessons_full_refreshed_at END,"
                            "settings_version=EXCLUDED.settings_version"),
                            params | {"balance": data["balance"], "paid": data["paid_lesson_count"], "subs": json.dumps(data["subscriptions"])})
                        from center_messenger.crm_api import DirectoryReader
                        snapshot = DirectoryReader._published(db, workspace)
                        if snapshot and snapshot["connection_id"] == settings["connection_id"] and snapshot["version"] == settings["version"]:
                            enqueue(db, workspace, snapshot, contact=job["contact"] if own.get("manual") else None,
                                    force=bool(job.get("force_at")), topics=(LESSONS, HISTORY))
                    else:
                        cached = db.execute(text("SELECT lessons,lessons_refreshed_at FROM alfa_family_details "
                            "WHERE workspace_id=:w AND connection_id=:conn AND branch_id=:b AND customer_id=:id "
                            "AND settings_version=:v FOR UPDATE"), params).mappings().one_or_none()
                        if cached is not None and (cached["lessons_refreshed_at"] is None or cached["lessons_refreshed_at"] <= started_at):
                            incoming = [dict(item, observed_at=started_at.isoformat()) for item in data]
                            lessons = merge_family_lessons(cached["lessons"], incoming, today=today, full=job["topic"] == HISTORY)
                            db.execute(text("UPDATE alfa_family_details SET lessons=CAST(:data AS jsonb),lessons_refreshed_at=now(),"
                                "lessons_full_refreshed_at=CASE WHEN :full THEN now() ELSE lessons_full_refreshed_at END "
                                "WHERE workspace_id=:w AND connection_id=:conn AND branch_id=:b AND customer_id=:id AND settings_version=:v"),
                                params | {"data": json.dumps(lessons), "full": job["topic"] == HISTORY})
                    db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:secret WHERE workspace_id=:w AND version=:v"),
                        {"w": workspace, "v": settings["version"], "secret": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})
                    self.finish(db, workspace, job)
        except AuthError as error:
            self.retry(workspace, job, busy=error.status == 409)
        except AlfaError:
            self.retry(workspace, job)
        except Exception:
            # Never log source/customer details or credentials.
            logging.getLogger(__name__).warning("CRM card refresh will resume")
            self.retry(workspace, job)

    def finish(self, db, workspace, job):
        # A force refresh requested during IO must survive this worker's finish.
        db.execute(text("UPDATE outbox_events SET state=CASE WHEN (payload->>'force_at') IS DISTINCT FROM CAST(:force AS text) "
            "THEN 'pending' ELSE 'published' END,payload=payload-'lease_owner'-'lease_until'-'error' "
            "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:owner"),
            {"w": workspace, "id": job["id"], "owner": self.owner, "force": job.get("force_at")})

    def retry(self, workspace, job, *, busy=False):
        attempts = int(job.get("attempts", 0)) + (0 if busy else 1)
        delay = 5 if busy else min(1800, 30 * 2 ** min(attempts - 1, 6))
        patch = {"attempts": attempts, "available_at": (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()}
        if not busy:
            patch["error"] = "crm_unavailable"
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until')||CAST(:p AS jsonb) "
                "WHERE workspace_id=:w AND id=:id AND state='pending' AND payload->>'lease_owner'=:owner"),
                {"w": workspace, "id": job["id"], "owner": self.owner, "p": json.dumps(patch)})

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
