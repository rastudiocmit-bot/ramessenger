"""Six-hour branch/status metadata, independent of customer and message archives.

Each operation keeps its own lease, pages and retry. Only a complete validated
list replaces published metadata; a forbidden branch never logs out MAX.
"""
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from center_messenger import card_jobs
from center_messenger.alfa_client import AlfaClient, AlfaError
from center_messenger.alfa_family import pages
from center_messenger.alfa_sync import AlfaSync
from center_messenger.auth_security import AuthError
from center_messenger.crm_api import DirectoryReader
from center_messenger.crm_directory import positive_id, source_name

TOPICS = ("crm.catalog.branches", "crm.catalog.study-status", "crm.catalog.lead-status")
INTERVAL = timedelta(hours=6)


def status(db, workspace, connection):
    return [dict(row) for row in db.execute(text("SELECT topic,state,payload-'checkpoint' AS progress FROM outbox_events "
        "WHERE workspace_id=:w AND topic=ANY(CAST(:topics AS text[])) AND payload->>'connection'=:c "
        "ORDER BY topic,event_key"), {"w": workspace, "c": str(connection), "topics": list(TOPICS)}).mappings()]


class CatalogReviewWorker:
    def __init__(self, connections):
        self.connections, self.accounts = connections, connections.accounts
        self.owner = str(uuid4())

    def schedule(self, db, workspace, settings, snapshot):
        for topic in TOPICS:
            db.execute(text("""
              INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state)
              SELECT :w,gen_random_uuid(),:topic,concat(:conn,':',branch),
                jsonb_build_object('connection',CAST(:conn AS text),'version',:v,
                  'run',CAST(:run AS text),'branch',branch::text,'available_at',now(),'attempts',0),'pending'
              FROM (SELECT 0 AS branch WHERE :global UNION ALL SELECT external_id FROM alfa_branches
                WHERE workspace_id=:w AND connection_id=:conn AND available AND NOT :global) branches
              ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET state='pending',payload=EXCLUDED.payload
              WHERE outbox_events.payload->>'version' IS DISTINCT FROM EXCLUDED.payload->>'version'
                OR outbox_events.payload->>'run' IS DISTINCT FROM EXCLUDED.payload->>'run'
                OR (outbox_events.state='published'
                  AND (outbox_events.payload->>'next_at')::timestamptz<=now())
            """), {"w": workspace, "conn": settings["connection_id"], "v": settings["version"],
                     "run": snapshot["run"], "topic": topic, "global": topic == TOPICS[0]})

    def current(self, db, workspace, job):
        settings = self.connections._row(db, workspace)
        snapshot = DirectoryReader._published(db, workspace)
        own = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id "
            "AND state='pending' AND payload->>'lease_owner'=:owner "
            "AND (payload->>'lease_until')::timestamptz>clock_timestamp() FOR UPDATE"),
            {"w": workspace, "id": job["id"], "owner": self.owner}).scalar_one_or_none()
        if (not settings or not settings["secret_ciphertext"] or str(settings["connection_id"]) != job["connection"]
            or settings["version"] != job["version"] or not snapshot or str(snapshot["run"]) != job["run"] or not own):
            raise card_jobs.LostCardLease()
        if job["topic"] != TOPICS[0] and not db.execute(text("SELECT 1 FROM alfa_branches WHERE workspace_id=:w "
            "AND connection_id=:c AND external_id=:b AND available"),
            {"w": workspace, "c": job["connection"], "b": int(job["branch"])}).scalar_one_or_none():
            raise card_jobs.LostCardLease()
        return dict(settings), dict(snapshot), dict(own)

    def step(self, workspace):
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            settings = self.connections._row(db, workspace)
            snapshot = DirectoryReader._published(db, workspace)
            if not settings or not settings["secret_ciphertext"] or not snapshot:
                return False
            self.schedule(db, workspace, settings, snapshot)
            row = db.execute(text("SELECT e.id,e.topic,e.payload FROM outbox_events e WHERE e.workspace_id=:w "
                "AND e.topic=ANY(CAST(:topics AS text[])) AND e.state='pending' "
                "AND e.payload->>'version'=:v AND e.payload->>'connection'=:c AND e.payload->>'run'=:run "
                "AND (e.payload->>'available_at')::timestamptz<=now() "
                "AND coalesce((e.payload->>'lease_until')::timestamptz,'epoch')<=clock_timestamp() "
                "AND (e.topic=:branches OR EXISTS(SELECT 1 FROM alfa_branches b WHERE b.workspace_id=e.workspace_id "
                "AND b.connection_id=CAST(:c AS uuid) AND b.external_id::text=e.payload->>'branch' AND b.available)) "
                "AND NOT " + card_jobs.block_sql() + " ORDER BY (e.topic=:branches) DESC,e.created_at,e.id "
                "LIMIT 1 FOR UPDATE OF e SKIP LOCKED"),
                {"w": workspace, "topics": list(TOPICS), "branches": TOPICS[0], "c": str(settings["connection_id"]),
                 "v": str(settings["version"]), "run": str(snapshot["run"])}).mappings().one_or_none()
            if not row:
                return False
            job = dict(row["payload"])
            job.update(lease_owner=self.owner, lease_until=(datetime.now(UTC)+timedelta(seconds=90)).isoformat())
            db.execute(text("UPDATE outbox_events SET payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                       {"w": workspace, "id": row["id"], "p": json.dumps(job)})
            job.update(id=row["id"], topic=row["topic"])
            settings = dict(settings)
        paged = None
        try:
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
                checkpoint = job.get("checkpoint") or {"responses": {}, "started_at": datetime.now(UTC).isoformat()}
                def persist(value):
                    with self.accounts.transaction(workspace_id=workspace) as db:
                        db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                        self.current(db, workspace, job)
                        db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('checkpoint',CAST(:p AS jsonb),"
                            "'stage',CAST(:stage AS text),'page',CAST(:page AS integer)) WHERE workspace_id=:w AND id=:id"),
                            {"w": workspace, "id": job["id"], "p": json.dumps(value), "stage": value["stage"], "page": value["page"]})
                        self.save_credentials(db, workspace, settings, credentials)
                paged = card_jobs.CheckpointClient(client, checkpoint, persist)
                kind = job["topic"].removeprefix("crm.catalog.")
                if kind == "branches":
                    values = AlfaClient.branches(paged, settings["seed_branch_id"])
                    if not values:
                        raise AlfaError("forbidden", 403)
                else:
                    values = [{"id": positive_id(item["id"]), "name": source_name(item.get("name"))}
                        for item in pages(paged, f"/v2api/{job['branch']}/{kind}/index", {}, maximum=10000)]
                with self.accounts.transaction(workspace_id=workspace) as db:
                    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                    _, snapshot, _ = self.current(db, workspace, job)
                    if kind == "branches":
                        AlfaSync._branches(db, workspace, settings["connection_id"], values)
                    else:
                        db.execute(text("DELETE FROM alfa_statuses WHERE workspace_id=:w AND run_id=:r AND branch_id=:b AND kind=:kind"),
                            {"w": workspace, "r": snapshot["run"], "b": int(job["branch"]), "kind": kind})
                        for value in values:
                            db.execute(text("INSERT INTO alfa_statuses(workspace_id,run_id,branch_id,kind,external_id,name) "
                                "VALUES(:w,:r,:b,:kind,:id,:name)"),
                                {"w": workspace, "r": snapshot["run"], "b": int(job["branch"]), "kind": kind, **value})
                    at = datetime.now(UTC)
                    result = {k: job[k] for k in ("connection", "version", "run", "branch")}
                    result.update(refreshed_at=checkpoint["started_at"], completed_at=at.isoformat(),
                                  next_at=(at+INTERVAL).isoformat(), count=len(values))
                    db.execute(text("UPDATE outbox_events SET state='published',payload=CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                               {"w": workspace, "id": job["id"], "p": json.dumps(result)})
                    self.save_credentials(db, workspace, settings, credentials)
            return True
        except card_jobs.LostCardLease:
            return False
        except card_jobs.PageYield:
            failure = {"available_at": datetime.now(UTC).isoformat()}
            scope = None
        except (AlfaError, AuthError) as error:
            failure, scope = card_jobs.failure(error, job.get("attempts", 0), busy=isinstance(error, AuthError) and error.status == 409)
            if failure["error"] == "access_denied":
                # Permissions can change without a new key. Recheck this small
                # operation on the next six-hour cycle, never in a tight loop.
                failure.update(paused=False, available_at=(datetime.now(UTC)+INTERVAL).isoformat())
        except Exception as error:
            logging.getLogger(__name__).warning("CRM catalog review will resume exception=%s", type(error).__name__)
            failure, scope = card_jobs.failure(error, job.get("attempts", 0))
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            try:
                self.current(db, workspace, job)
            except card_jobs.LostCardLease:
                return False
            if paged and failure.get("error"):
                failure.update(stage=paged.stage, page=paged.page)
            db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until') "
                "||CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                {"w": workspace, "id": job["id"], "p": json.dumps(failure)})
            if failure.get("error") == "invalid_response":
                db.execute(text("UPDATE outbox_events SET payload=payload-'checkpoint' WHERE workspace_id=:w AND id=:id"),
                           {"w": workspace, "id": job["id"]})
            card_jobs.save_block(db, workspace, job, failure, scope)
        return True

    def save_credentials(self, db, workspace, settings, credentials):
        db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:s WHERE workspace_id=:w AND version=:v"),
            {"w": workspace, "v": settings["version"],
             "s": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})

    def run_next(self):
        pending = getattr(self, "_scopes", [])
        if not pending:
            with self.accounts.transaction() as db:
                pending = list(db.execute(text("SELECT s.workspace_id FROM alfa_settings s JOIN alfa_sync_control c "
                    "ON c.workspace_id=s.workspace_id AND c.connection_id=s.connection_id "
                    "WHERE s.secret_ciphertext IS NOT NULL AND c.published_id IS NOT NULL ORDER BY s.workspace_id")).scalars())
            self._scopes = pending
        return self.step(pending.pop(0)) if pending else False
