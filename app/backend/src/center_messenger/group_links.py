"""Light six-hour CGI refreshes for active Customer IDs, separate from finance."""

import json
from datetime import UTC, datetime

from sqlalchemy import text

from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_family import pages
from center_messenger.crm_directory import positive_id, source_date, source_name
from center_messenger.auth_security import AuthError
from center_messenger import card_jobs

TOPIC = "crm.customer.groups"


def progress(db, workspace, snapshot):
    """One active Customer ID, with all its available branch observations."""
    return dict(db.execute(text("""
      WITH clients AS (
        SELECT c.external_id,
          bool_and(coalesce(e.payload->>'checked_run'=CAST(:r AS text)
            AND (e.payload->>'refreshed_at')::timestamptz>now()-interval '6 hours',false)) AS ready,
          bool_or(coalesce(e.state='pending',false)) AS queued,
          bool_or(coalesce(e.state='pending' AND e.payload ? 'error',false)) AS error,
          bool_or(c.contact_id IS NULL OR c.student_id IS NULL) AS unlinked
        FROM alfa_customers c JOIN alfa_settings s ON s.workspace_id=c.workspace_id
          AND s.connection_id=:conn AND s.secret_ciphertext IS NOT NULL
        JOIN alfa_branches b ON b.workspace_id=c.workspace_id
          AND b.connection_id=:conn AND b.external_id=c.branch_id AND b.available
        LEFT JOIN outbox_events e ON e.workspace_id=c.workspace_id AND e.topic=:topic
          AND e.event_key=concat(:conn,':',c.branch_id,':',c.external_id)
          AND e.payload->>'version'=s.version::text AND e.payload->>'contact'=c.contact_id::text
          AND e.payload->>'student'=c.student_id::text
        WHERE c.workspace_id=:w AND c.run_id=:r AND c.status='client' GROUP BY c.external_id
      ) SELECT count(*) AS total,count(*) FILTER(WHERE ready) AS ready,
        count(*) FILTER(WHERE queued) AS queued,count(*) FILTER(WHERE error) AS errors,
        count(*) FILTER(WHERE unlinked) AS unlinked FROM clients
    """), {"w": workspace, "conn": snapshot["connection_id"], "r": snapshot["run"], "topic": TOPIC}).mappings().one())


def enqueue(db, workspace, snapshot, *, contact=None, force=False):
    return db.execute(text("""
      INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state)
      SELECT :w,gen_random_uuid(),:topic,concat(:conn,':',c.branch_id,':',c.external_id),
        jsonb_build_object('connection',CAST(:conn AS text),'branch',c.branch_id::text,
          'customer',c.external_id::text,'contact',c.contact_id::text,'student',c.student_id::text,
          'version',:v,'manual',:manual,'due_at',now(),'available_at',now(),'attempts',0,
          'force_at',CASE WHEN :force THEN CAST(clock_timestamp() AS text) ELSE NULL END),'pending'
      FROM alfa_customers c JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND b.connection_id=:conn
        AND b.external_id=c.branch_id AND b.available
      LEFT JOIN outbox_events e ON e.workspace_id=c.workspace_id AND e.topic=:topic
        AND e.event_key=concat(:conn,':',c.branch_id,':',c.external_id)
      WHERE c.workspace_id=:w AND c.run_id=:r AND c.contact_id IS NOT NULL AND c.student_id IS NOT NULL
        AND (CAST(:contact AS uuid) IS NULL OR c.contact_id=CAST(:contact AS uuid))
        AND (:manual OR c.status='client')
        AND (:force OR e.id IS NULL OR e.payload->>'version'<>CAST(:v AS text)
          OR e.payload->>'contact'<>c.contact_id::text OR e.payload->>'student'<>c.student_id::text
          OR e.payload->>'checked_run' IS DISTINCT FROM CAST(:r AS text)
          OR (e.payload->>'refreshed_at')::timestamptz<=now()-interval '6 hours')
      ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET state='pending',payload=CASE
        WHEN outbox_events.state='pending' AND outbox_events.payload->>'version'=EXCLUDED.payload->>'version'
          AND outbox_events.payload->>'contact'=EXCLUDED.payload->>'contact'
          AND outbox_events.payload->>'student'=EXCLUDED.payload->>'student'
        THEN outbox_events.payload||jsonb_build_object(
          'manual',coalesce((outbox_events.payload->>'manual')::boolean,false) OR :manual,
          'force_at',CASE WHEN :force THEN EXCLUDED.payload->>'force_at' ELSE outbox_events.payload->>'force_at' END)
        ELSE EXCLUDED.payload||CASE WHEN outbox_events.payload->>'version'=EXCLUDED.payload->>'version'
          AND outbox_events.payload->>'contact'=EXCLUDED.payload->>'contact'
          AND outbox_events.payload->>'student'=EXCLUDED.payload->>'student'
          THEN jsonb_build_object('refreshed_at',outbox_events.payload->>'refreshed_at',
            'checked_run',outbox_events.payload->>'checked_run','groups',outbox_events.payload->'groups') ELSE '{}'::jsonb END END
      WHERE outbox_events.state<>'pending' OR outbox_events.payload->>'version'<>EXCLUDED.payload->>'version'
        OR outbox_events.payload->>'contact'<>EXCLUDED.payload->>'contact'
        OR outbox_events.payload->>'student'<>EXCLUDED.payload->>'student'
        OR (:manual AND outbox_events.payload->>'manual' IS DISTINCT FROM 'true') OR :force
    """), {"w": workspace, "conn": snapshot["connection_id"], "r": snapshot["run"], "v": snapshot["version"],
             "topic": TOPIC, "contact": contact, "manual": contact is not None, "force": force}).rowcount


def fetch(client, branch, customer):
    """Use documented CGI IDs/periods and verify group metadata in this branch."""
    branch, customer = positive_id(branch), positive_id(customer)
    memberships = []
    for row in pages(client, f"/v2api/{branch}/cgi/customer?customer_id={customer}", {"customer_id": customer}, maximum=500):
        if positive_id(row.get("customer_id")) != customer:
            raise AlfaError("invalid_response")
        begin, end = source_date(row.get("b_date")), source_date(row.get("e_date"))
        if begin and end and begin > end:
            raise AlfaError("invalid_response")
        memberships.append({"id": positive_id(row["id"]), "group_id": positive_id(row.get("group_id")),
                            "begin": begin, "end": end})
    groups = {}
    for identifier in sorted({m["group_id"] for m in memberships}):
        rows = pages(client, f"/v2api/{branch}/group/index", {"id": identifier, "removed": 0}, maximum=1)
        if not rows:
            continue  # An archived/deleted group cannot remain active in the card.
        row = rows[0]
        if positive_id(row["id"]) != identifier:
            raise AlfaError("invalid_response")
        if row.get("branch_id") not in (None, "", 0, "0") and positive_id(row["branch_id"]) != branch:
            continue
        if row.get("branch_ids") not in (None, []):
            if not isinstance(row["branch_ids"], list):
                raise AlfaError("invalid_response")
            if branch not in [positive_id(value) for value in row["branch_ids"]]:
                continue
        groups[identifier] = source_name(row.get("name"))
    return groups, [m for m in memberships if m["group_id"] in groups]


def process(worker, workspace, job):
    from center_messenger.crm_api import DirectoryReader
    from center_messenger.family_cache import _TariffCache
    paged = None
    try:
        with worker.accounts.transaction(workspace_id=workspace) as db:
            owned = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id AND state='pending' "
                "AND payload->>'lease_owner'=:owner FOR UPDATE"),
                {"w": workspace, "id": job["id"], "owner": worker.owner}).scalar_one_or_none()
            if owned is None:
                return
            if card_jobs.blocked(db, workspace, job):
                db.execute(text("UPDATE outbox_events SET payload=payload-'lease_owner'-'lease_until' "
                    "WHERE workspace_id=:w AND id=:id AND payload->>'lease_owner'=:o"),
                    {"w": workspace, "id": job["id"], "o": worker.owner})
                return
            job = {**job, "manual": owned.get("manual", False), "force_at": owned.get("force_at")}
            settings = worker.connections._row(db, workspace)
            snapshot = DirectoryReader._published(db, workspace)
            source = worker._source(db, workspace, job)
            if (not settings or not settings["secret_ciphertext"] or not snapshot or not source
                or str(settings["connection_id"]) != job["connection"] or settings["version"] != job["version"]
                or snapshot["connection_id"] != settings["connection_id"] or not (job["manual"] or source["status"] == "client")):
                worker.finish(db, workspace, job)
                return
            settings, snapshot = dict(settings), dict(snapshot)
            checkpoint = worker.checkpoint(job, owned, snapshot, snapshot["today"])
        with worker.connections._host_gate(settings["origin"]) as gate:
            credentials = worker.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
            client = worker.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
            if not job["force_at"]:
                client = _TariffCache(client, worker.tariffs, (str(workspace), job["connection"], job["version"]))
            client = paged = worker.checkpoint_client(client, workspace, job, checkpoint, settings, credentials)
            groups, memberships = fetch(client, int(job["branch"]), int(job["customer"]))
            with worker.accounts.transaction(workspace_id=workspace) as db:
                db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                own = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND id=:id AND state='pending' "
                    "AND payload->>'lease_owner'=:owner AND (payload->>'lease_until')::timestamptz>now() FOR UPDATE"),
                    {"w": workspace, "id": job["id"], "owner": worker.owner}).scalar_one_or_none()
                if own is None:
                    return
                current = worker.connections._row(db, workspace)
                published = DirectoryReader._published(db, workspace)
                source = worker._source(db, workspace, job)
                if (not current or not current["secret_ciphertext"] or current["version"] != settings["version"]
                    or str(current["connection_id"]) != job["connection"] or not published or published["run"] != snapshot["run"]
                    or not source or not (own.get("manual") or source["status"] == "client")):
                    worker.finish(db, workspace, job)
                    return
                params = {"w": workspace, "r": snapshot["run"], "b": int(job["branch"]), "customer": int(job["customer"])}
                for identifier, name in groups.items():
                    db.execute(text("INSERT INTO alfa_groups(workspace_id,run_id,branch_id,external_id,name) VALUES(:w,:r,:b,:id,:name) "
                        "ON CONFLICT(workspace_id,run_id,branch_id,external_id) DO UPDATE SET name=EXCLUDED.name"),
                        params | {"id": identifier, "name": name})
                for member in memberships:
                    collision = db.execute(text("SELECT 1 FROM alfa_memberships WHERE workspace_id=:w AND run_id=:r AND branch_id=:b "
                        "AND group_id=:g AND external_id=:id AND customer_id<>:customer"),
                        params | {"g": member["group_id"], "id": member["id"]}).scalar_one_or_none()
                    if collision:
                        raise AlfaError("invalid_response")
                db.execute(text("DELETE FROM alfa_memberships WHERE workspace_id=:w AND run_id=:r AND branch_id=:b AND customer_id=:customer"), params)
                for member in memberships:
                    db.execute(text("INSERT INTO alfa_memberships(workspace_id,run_id,branch_id,group_id,external_id,customer_id,b_date,e_date) "
                        "VALUES(:w,:r,:b,:g,:id,:customer,:begin,:end)"),
                        params | {"g": member["group_id"], "id": member["id"], "begin": member["begin"], "end": member["end"]})
                patch = {"refreshed_at": checkpoint["started_at"], "checked_run": str(snapshot["run"]), "groups": len(groups)}
                db.execute(text("UPDATE outbox_events SET payload=payload||CAST(:p AS jsonb) WHERE workspace_id=:w AND id=:id"),
                    {"w": workspace, "id": job["id"], "p": json.dumps(patch)})
                db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:secret WHERE workspace_id=:w AND version=:v"),
                    {"w": workspace, "v": settings["version"],
                     "secret": worker.connections.vault.seal(workspace, settings["connection_id"], credentials)})
                card_jobs.clear_verified_denial(db, workspace, job)
                from center_messenger.card_preference import refresh_for_contact
                refresh_for_contact(db, workspace, snapshot, job['contact'])
                worker.finish(db, workspace, job)
    except card_jobs.PageYield:
        worker.yield_job(workspace, job)
    except card_jobs.LostCardLease:
        worker.yield_job(workspace, job)
    except AuthError as error:
        worker.retry(workspace, job, error=error, busy=error.status == 409, paged=paged)
    except AlfaError as error:
        worker.retry(workspace, job, error=error, paged=paged)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning("CRM group links will resume")
        worker.retry(workspace, job, error=error, paged=paged)
