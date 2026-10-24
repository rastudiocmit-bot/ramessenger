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
from center_messenger.alfa_family import fetch_family_card
from center_messenger.auth_security import AuthError

TOPIC = "crm.card.refresh"
INTERVAL = timedelta(minutes=30)
LEASE = timedelta(seconds=90)


def enqueue(db, workspace, snapshot, *, contact=None, force=False):
    """Coalesce all family sources, retaining leases, retry deadlines and good facts."""
    return db.execute(text("""
      INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state)
      SELECT :w,gen_random_uuid(),:topic,concat(:conn,':',c.branch_id,':',c.external_id),
        jsonb_build_object('connection',CAST(:conn AS text),'branch',c.branch_id::text,
          'customer',c.external_id::text,'contact',c.contact_id::text,'student',c.student_id::text,
          'version',:version,'manual',:manual,'due_at',coalesce(f.refreshed_at+interval '30 minutes',now()),
          'available_at',now(),'attempts',0),'pending'
      FROM alfa_customers c JOIN alfa_branches b ON b.workspace_id=c.workspace_id
        AND b.connection_id=:conn AND b.external_id=c.branch_id AND b.available
      LEFT JOIN alfa_family_details f ON f.workspace_id=c.workspace_id AND f.connection_id=:conn
        AND f.branch_id=c.branch_id AND f.customer_id=c.external_id AND f.settings_version=:version
      WHERE c.workspace_id=:w AND c.run_id=:run AND c.contact_id IS NOT NULL AND c.student_id IS NOT NULL
        AND (CAST(:contact AS uuid) IS NULL OR c.contact_id=CAST(:contact AS uuid))
        AND (:manual OR EXISTS(SELECT 1 FROM conversations q WHERE q.workspace_id=c.workspace_id
          AND q.contact_id=c.contact_id AND q.kind='direct' AND q.needs_reply))
        AND (:force OR f.refreshed_at IS NULL OR f.refreshed_at<=now()-interval '30 minutes')
      ON CONFLICT (workspace_id,topic,event_key) DO UPDATE SET
        state='pending',payload=CASE WHEN outbox_events.state='pending'
          AND outbox_events.payload->>'version'=EXCLUDED.payload->>'version'
          AND outbox_events.payload->>'contact'=EXCLUDED.payload->>'contact'
          AND outbox_events.payload->>'student'=EXCLUDED.payload->>'student'
          THEN jsonb_set(outbox_events.payload,'{manual}','true') ELSE EXCLUDED.payload END
      WHERE (outbox_events.state<>'pending' AND coalesce((outbox_events.payload->>'lease_until')::timestamptz,'epoch')<=now())
        OR outbox_events.payload->>'version'<>EXCLUDED.payload->>'version'
        OR outbox_events.payload->>'contact'<>EXCLUDED.payload->>'contact'
        OR outbox_events.payload->>'student'<>EXCLUDED.payload->>'student'
        OR (:manual AND outbox_events.payload->>'manual' IS DISTINCT FROM 'true')
      """), {"w": workspace, "topic": TOPIC, "conn": snapshot["connection_id"],
               "run": snapshot["run"], "version": snapshot["version"],
               "contact": contact, "manual": contact is not None, "force": force}).rowcount


def cache_status(db, workspace, connection, cards, contact, version):
    sources = [card for student in cards for card in student["cards"]]
    times = [datetime.fromisoformat(card["refreshed_at"]) for card in sources if card.get("refreshed_at")]
    jobs = list(db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:topic "
        "AND state='pending' AND payload->>'connection'=:conn AND payload->>'version'=:v AND payload->>'contact'=:contact"),
        {"w": workspace, "topic": TOPIC, "conn": str(connection), "v": str(version), "contact": str(contact)}).scalars())
    keys = {(card["branch_id"], card["id"]) for card in sources}
    jobs = [job for job in jobs if (job["branch"], job["customer"]) in keys]
    ready = len(times) == len(sources) and bool(sources)
    updated = min(times) if ready else None
    automatic = db.execute(text("SELECT EXISTS(SELECT 1 FROM conversations WHERE workspace_id=:w "
        "AND contact_id=:c AND needs_reply AND kind='direct')"), {"w": workspace, "c": contact}).scalar_one()
    return {"auto_refresh": bool(automatic), "ready": ready, "refreshing": bool(jobs), "updated_at": updated.isoformat() if updated else None,
            "next_refresh_at": (updated + INTERVAL).isoformat() if updated else None,
            "interval_minutes": 30, "sources": len(sources), "cached_sources": len(times),
            "overdue": not ready or updated + INTERVAL <= datetime.now(UTC),
            "retry_at": min((job["available_at"] for job in jobs), default=None),
            "error": "Не удалось обновить CRM. Сохранены последние полученные сведения."
                     if any(job.get("error") for job in jobs) else None}


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
            row = db.execute(text("SELECT id,payload FROM outbox_events WHERE workspace_id=:w AND topic=:topic "
                "AND state='pending' AND (payload->>'available_at')::timestamptz<=now() "
                "AND coalesce((payload->>'lease_until')::timestamptz,'epoch')<=now() "
                "ORDER BY (payload->>'manual')::boolean DESC,(payload->>'due_at')::timestamptz,id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"), {"w": workspace, "topic": TOPIC}).mappings().one_or_none()
            if not row:
                return None
            job = dict(row["payload"])
            job.update(lease_owner=self.owner, lease_until=(datetime.now(UTC) + LEASE).isoformat())
            db.execute(text("UPDATE outbox_events SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                       {"w": workspace, "id": row["id"], "p": json.dumps(job)})
            return {**job, "id": row["id"]}

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
                job = {**job, "manual": owned.get("manual", False)}
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
                today = db.execute(text("SELECT CAST(now() AT TIME ZONE timezone AS date) FROM workspaces WHERE id=:w"),
                                   {"w": workspace}).scalar_one()
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
                client = _TariffCache(client, self.tariffs, (str(workspace), job["connection"], job["version"]))
                source["branch_id"] = int(job["branch"])
                data = fetch_family_card(client, int(job["branch"]), int(job["customer"]), source_customer=source, today=today)
                with self.accounts.transaction(workspace_id=workspace) as db:
                    # Same lock order as existing settings/message writers.
                    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                    current = self.connections._row(db, workspace)
                    own = db.execute(text("SELECT 1 FROM outbox_events WHERE workspace_id=:w AND id=:id AND "
                        "state='pending' AND payload->>'lease_owner'=:owner AND (payload->>'lease_until')::timestamptz>now() "
                        "FOR UPDATE"), {"w": workspace, "id": job["id"], "owner": self.owner}).scalar_one_or_none()
                    if not own:
                        return
                    if (not current or current["version"] != settings["version"] or
                        str(current["connection_id"]) != job["connection"] or not self._source(db, workspace, job)):
                        self.finish(db, workspace, job)
                        return
                    db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,"
                        "settings_version,balance,paid_lesson_count,subscriptions,lessons) VALUES "
                        "(:w,:conn,:b,:id,:v,:balance,:paid,CAST(:subs AS jsonb),CAST(:lessons AS jsonb)) "
                        "ON CONFLICT (workspace_id,connection_id,branch_id,customer_id) DO UPDATE SET "
                        "settings_version=EXCLUDED.settings_version,balance=EXCLUDED.balance,paid_lesson_count=EXCLUDED.paid_lesson_count,"
                        "subscriptions=EXCLUDED.subscriptions,lessons=EXCLUDED.lessons,refreshed_at=now()"),
                        {"w": workspace, "conn": job["connection"], "b": int(job["branch"]), "id": int(job["customer"]),
                         "v": job["version"], "balance": data["balance"], "paid": data["paid_lesson_count"],
                         "subs": json.dumps(data["subscriptions"]), "lessons": json.dumps(data["lessons"])})
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
        db.execute(text("UPDATE outbox_events SET state='published',payload=payload-'lease_owner'-'lease_until'-'error' "
            "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:owner"),
            {"w": workspace, "id": job["id"], "owner": self.owner})

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
