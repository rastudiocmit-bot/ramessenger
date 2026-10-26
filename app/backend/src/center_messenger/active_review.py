"""Six-hour, page-at-a-time review of active Customer IDs; never reads MAX history."""
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5, UUID

from sqlalchemy import text
from center_messenger.alfa_client import AlfaError
from center_messenger.auth_security import AuthError
from center_messenger.crm_api import DirectoryReader
from center_messenger.crm_directory import customer_row, page_rows, normalized_name

TOPIC = "crm.active.review"
IDENTITY = "crm.active.identity"
INTERVAL = timedelta(hours=6)


def status(db, workspace, connection):
    return db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND event_key=:c"), {"w": workspace, "t": TOPIC, "c": str(connection)}).scalar_one_or_none() or {"state": "waiting", "processed": 0}


def store(db, workspace, connection, payload):
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:t,:c,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
        "DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        {"w": workspace, "id": uuid4(), "t": TOPIC, "c": str(connection), "p": json.dumps(payload)})


def request(db, workspace, connection):
    value = dict(status(db, workspace, connection))
    if value.get("lease_until") and datetime.fromisoformat(value["lease_until"]) > datetime.now(UTC):
        return {"queued": True, "already_running": True}
    value.update(next_at=datetime.now(UTC).isoformat(), state="waiting", run=None, attempts=0)
    store(db, workspace, connection, value)
    return {"queued": True, "already_running": False}


def apply_customer(db, workspace, settings, snapshot, raw, *, archive=False):
    """Update light directory facts by source ID; full finance cards remain untouched."""
    value = customer_row(raw, archive=archive)
    branch, customer = int(raw["_branch"]), value["external_id"]
    old = db.execute(text("SELECT * FROM alfa_customers WHERE workspace_id=:w AND run_id=:r "
        "AND branch_id=:b AND external_id=:id"),
        {"w": workspace, "r": snapshot["run"], "b": branch, "id": customer}).mappings().one_or_none()
    if old:
        for field in ("dob", "study_status_id", "lead_status_ids"):
            if field not in raw:
                value[field] = old[field]
    # No name/phone matching. Existing verified bindings survive name changes.
    contact = old["contact_id"] if old else None
    student = old["student_id"] if old else None
    if not contact:
        matches = list(db.execute(text("SELECT contact_id,student_id,branch_id FROM alfa_customers "
            "WHERE workspace_id=:w AND run_id=:r AND external_id=:id AND contact_id IS NOT NULL"),
            {"w": workspace, "r": snapshot["run"], "id": customer}).mappings())
        # Customer IDs are global within this CRM connection. A second branch
        # is another observation, including APIs omitting optional branch_ids.
        if len({(m["contact_id"], m["student_id"]) for m in matches}) == 1:
            contact, student = matches[0]["contact_id"], matches[0]["student_id"]
    contact = contact or uuid5(UUID(str(settings["connection_id"])), f"active-contact:{branch}:{customer}")
    student = student or uuid5(UUID(str(settings["connection_id"])), f"student:{customer}")
    parent = value["legal_name"] or value["name"]
    review = bool(old and normalized_name(old["legal_name"]) != normalized_name(value["legal_name"]))
    db.execute(text("INSERT INTO contacts(workspace_id,id,display_name) VALUES(:w,:id,:name) ON CONFLICT DO NOTHING"),
               {"w": workspace, "id": contact, "name": parent})
    db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES(:w,:id,:name) "
        "ON CONFLICT(workspace_id,id) DO UPDATE SET display_name=EXCLUDED.display_name"),
        {"w": workspace, "id": student, "name": value["name"]})
    db.execute(text("INSERT INTO contact_student_links(workspace_id,contact_id,student_id,managed_by_alfa) "
        "VALUES(:w,:c,:s,true) ON CONFLICT DO NOTHING"), {"w": workspace, "c": contact, "s": student})
    db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,legal_name,status,dob,study_status_id,lead_status_ids,"
        "contact_id,student_id,needs_review,phones,emails) VALUES(:w,:r,:b,:id,:name,:parent,:status,:dob,:study,CAST(:leads AS jsonb),:c,:s,:review,CAST(:phones AS jsonb),CAST(:emails AS jsonb)) "
        "ON CONFLICT(workspace_id,run_id,branch_id,external_id) DO UPDATE SET name=EXCLUDED.name,"
        "legal_name=EXCLUDED.legal_name,status=EXCLUDED.status,dob=EXCLUDED.dob,study_status_id=EXCLUDED.study_status_id,"
        "lead_status_ids=EXCLUDED.lead_status_ids,phones=EXCLUDED.phones,emails=EXCLUDED.emails,contact_id=EXCLUDED.contact_id,"
        "student_id=EXCLUDED.student_id,needs_review=alfa_customers.needs_review OR EXCLUDED.needs_review"),
        {"w": workspace, "r": snapshot["run"], "b": branch, "id": customer, "name": value["name"],
         "parent": value["legal_name"], "status": value["status"], "dob": value["dob"], "study": value["study_status_id"],
         "leads": json.dumps(value["lead_status_ids"]), "c": contact, "s": student, "review": review,
         "phones":json.dumps(value["phones"]),"emails":json.dumps(value["emails"])})
    # Published lightweight directory gets new ID-bound cards without a full archive import.
    db.execute(text("INSERT INTO alfa_directory(workspace_id,run_id,contact_id,display_name,phones,emails,needs_review,search_text) "
        "VALUES(:w,:r,:c,:name,CAST(:phones AS jsonb),CAST(:emails AS jsonb),:review,:search) ON CONFLICT(workspace_id,run_id,contact_id) "
        "DO UPDATE SET needs_review=alfa_directory.needs_review OR EXCLUDED.needs_review,"
        "phones=(SELECT coalesce(jsonb_agg(DISTINCT x),'[]'::jsonb) FROM alfa_customers a "
        "CROSS JOIN LATERAL jsonb_array_elements(a.phones) x WHERE a.workspace_id=:w AND a.run_id=:r AND a.contact_id=:c),"
        "emails=(SELECT coalesce(jsonb_agg(DISTINCT x),'[]'::jsonb) FROM alfa_customers a "
        "CROSS JOIN LATERAL jsonb_array_elements(a.emails) x WHERE a.workspace_id=:w AND a.run_id=:r AND a.contact_id=:c),"
        "search_text=concat_ws(' ',alfa_directory.display_name,alfa_directory.phones::text,alfa_directory.emails::text,"
        "(SELECT string_agg(a.name||' '||a.external_id::text,' ') FROM alfa_customers a "
        "WHERE a.workspace_id=:w AND a.run_id=:r AND a.contact_id=:c))"),
        {"w": workspace, "r": snapshot["run"], "c": contact, "name": parent, "review": review,
         "search": normalized_name(parent + " " + value["name"] + " " + str(customer)), "phones":json.dumps(value["phones"]),"emails":json.dumps(value["emails"])})
    if not old or old["contact_id"] != contact or old["student_id"] != student:
        # Repair only the newly linked ID, never all historic families.
        db.execute(text("UPDATE conversations c SET contact_id=:contact WHERE c.workspace_id=:w "
            "AND c.external_chat_id=:external AND NOT c.crm_link_manual AND EXISTS(SELECT 1 FROM alfa_max_settings m "
            "WHERE m.workspace_id=c.workspace_id AND m.connection_id=c.connection_id AND m.crm_connection_id=:crm)"),
            {"w": workspace, "contact": contact, "external": f"alfa-customer:{branch}:{customer}", "crm": settings["connection_id"]})
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,'crm.identity.repair',:key,CAST(:p AS jsonb),'pending') "
            "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload,state='pending'"),
            {"w": workspace, "id": uuid4(), "key": f"{settings['connection_id']}:{contact}",
             "p": json.dumps({"connection": str(settings["connection_id"]), "contact": str(contact)})})
    return {"customer_id": str(customer), "branch_id": str(branch), "name": value["name"],
            "contact_id": str(contact), "student_id": str(student), "active": value["status"] == "client"}


class ActiveReviewWorker:
    def __init__(self, connections):
        self.connections, self.accounts = connections, connections.accounts
        self.owner = str(uuid4()); self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="crm-active-review")

    def step(self, workspace):
        now = datetime.now(UTC)
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            settings = self.connections._row(db, workspace)
            snapshot = DirectoryReader._published(db, workspace)
            if not settings or not settings["secret_ciphertext"] or not snapshot:
                return False
            settings, snapshot = dict(settings), dict(snapshot)
            job = dict(status(db, workspace, settings["connection_id"]))
            if job.get("lease_until") and datetime.fromisoformat(job["lease_until"]) > now:
                return False
            if job.get("version") == settings["version"] and job.get("next_at") and datetime.fromisoformat(job["next_at"]) > now:
                return False
            if not job.get("run") or job.get("version") != settings["version"] or job.get("state") == "complete":
                branches = list(db.execute(text("SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                    "AND connection_id=:c AND available ORDER BY external_id"), {"w": workspace, "c": settings["connection_id"]}).scalars())
                if not branches:
                    return False
                job.update(run=str(uuid4()), version=settings["version"], branches=branches, bi=0, page=0,
                           processed=0, unresolved=0, received=0, phase="active", last_id=0, started_at=now.isoformat())
            job.update(state="running", owner=self.owner, lease_until=(now + timedelta(seconds=90)).isoformat(), updated_at=now.isoformat())
            branch = job["branches"][job["bi"]]
            missing = None
            if job["phase"] == "transitions":
                missing = db.execute(text("SELECT external_id FROM alfa_customers a WHERE a.workspace_id=:w AND a.run_id=:r "
                    "AND a.branch_id=:b AND a.status='client' AND a.external_id>:last AND NOT EXISTS(SELECT 1 FROM outbox_events e "
                    "WHERE e.workspace_id=a.workspace_id AND e.topic=:t AND e.event_key=concat(CAST(:c AS text),':',a.branch_id,':',a.external_id) "
                    "AND (e.payload->>'run'=:run OR e.payload->>'active'='false')) ORDER BY external_id LIMIT 1"),
                    {"w": workspace, "r": snapshot["run"], "b": branch, "last": job["last_id"], "t": IDENTITY,
                     "c": str(settings["connection_id"]), "run": job["run"]}).scalar_one_or_none()
            store(db, workspace, settings["connection_id"], job)
        try:
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
                archive = False
                if job["phase"] == "active":
                    data = client.call(f"/v2api/{branch}/customer/index", {"is_study": 1, "removed": 0, "page": job["page"], "pageSize": 100})
                    rows, total = page_rows(data, job["page"])
                    if len({str(r.get('id')) for r in rows}) != len(rows) or any(str(r.get("is_study")) not in {"1", "True"} for r in rows):
                        raise AlfaError("invalid_response")
                elif missing:
                    # Only a previously active ID which disappeared from the active list.
                    data = client.call(f"/v2api/{branch}/customer/index", {"id": missing, "is_study": 2, "removed": 0, "page": 0})
                    rows, total = page_rows(data, 0)
                    if not rows:
                        data = client.call(f"/v2api/{branch}/customer/index", {"id": missing, "is_study": 2, "removed": 2, "page": 0})
                        rows, total = page_rows(data, 0); archive = True
                    if len(rows) > 1 or any(str(r.get("id")) != str(missing) for r in rows):
                        raise AlfaError("invalid_response")
                else:
                    rows, total = [], 0
                with self.accounts.transaction(workspace_id=workspace) as db:
                    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
                    current = self.connections._row(db, workspace)
                    owned = status(db, workspace, settings["connection_id"])
                    latest = DirectoryReader._published(db, workspace)
                    if (not current or current["version"] != settings["version"] or not latest or latest["run"] != snapshot["run"]
                        or owned.get("owner") != self.owner or owned.get("run") != job["run"] or not owned.get("lease_until") or datetime.fromisoformat(owned["lease_until"]) <= datetime.now(UTC)):
                        return False
                    if job["phase"] == "active" and rows:
                        keys = [f"{settings['connection_id']}:{branch}:{raw['id']}" for raw in rows]
                        duplicate = db.execute(text("SELECT 1 FROM outbox_events WHERE workspace_id=:w AND topic=:t "
                            "AND event_key=ANY(CAST(:keys AS text[])) AND payload->>'run'=:run LIMIT 1"),
                            {"w":workspace,"t":IDENTITY,"keys":keys,"run":job["run"]}).scalar_one_or_none()
                        if duplicate:
                            raise AlfaError("invalid_response")
                    for raw in rows:
                        fact = apply_customer(db, workspace, settings, snapshot, dict(raw, _branch=branch), archive=archive)
                        fact.update(run=job["run"], checked_at=datetime.now(UTC).isoformat())
                        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                            "VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
                            "DO UPDATE SET payload=EXCLUDED.payload"), {"w": workspace, "id": uuid4(), "t": IDENTITY,
                            "key": f"{settings['connection_id']}:{branch}:{fact['customer_id']}", "p": json.dumps(fact)})
                        job["last_client"] = fact
                    if missing and not rows:
                        fact = {"run":job["run"],"active":False,"state":"not_found","customer_id":str(missing),"branch_id":str(branch)}
                        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                            "VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload"),
                            {"w":workspace,"id":uuid4(),"t":IDENTITY,"key":f"{settings['connection_id']}:{branch}:{missing}","p":json.dumps(fact)})
                        db.execute(text("UPDATE alfa_customers SET needs_review=true WHERE workspace_id=:w AND run_id=:r AND branch_id=:b AND external_id=:id"),
                            {"w":workspace,"r":snapshot["run"],"b":branch,"id":missing})
                        job["unresolved"] = job.get("unresolved",0)+1
                    if job["phase"] == "active":
                        job["processed"] += len(rows)
                        job["page"] += 1
                        job["received"] = job.get("received", 0) + len(rows)
                        if job["received"] > total or (not rows and job["received"] < total):
                            raise AlfaError("invalid_response")
                        if job["received"] == total:
                            job.update(phase="transitions", last_id=0)
                        if job["page"] > 1000:
                            raise AlfaError("import_limit")
                    elif missing:
                        job["last_id"] = missing
                    else:
                        job["bi"] += 1; job.update(page=0, received=0, phase="active", last_id=0)
                    now = datetime.now(UTC)
                    job.update(owner=None, lease_until=None, next_at=now.isoformat(), updated_at=now.isoformat(), attempts=0, error=None)
                    if job["bi"] == len(job["branches"]):
                        job["unresolved"] = db.execute(text("SELECT count(*) FROM outbox_events WHERE workspace_id=:w "
                            "AND topic=:t AND event_key LIKE :prefix AND payload->>'state'='not_found'"),
                            {"w":workspace,"t":IDENTITY,"prefix":str(settings["connection_id"])+":%"}).scalar_one()
                        job.update(state="complete", last_success_at=now.isoformat(), next_at=(now + INTERVAL).isoformat())
                    store(db, workspace, settings["connection_id"], job)
                    db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:secret WHERE workspace_id=:w AND version=:v"),
                        {"w": workspace, "v": settings["version"], "secret": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})
            return True
        except (AlfaError, AuthError) as error:
            code = getattr(error, "code", "busy" if getattr(error, "status", None) == 409 else "unavailable")
        except Exception:
            code = "internal"
            logging.getLogger(__name__).warning("Active CRM review will resume")
        with self.accounts.transaction(workspace_id=workspace) as db:
            owned = dict(status(db, workspace, settings["connection_id"]))
            if owned.get("owner") == self.owner and owned.get("run") == job["run"]:
                attempt = owned.get("attempts", 0) + (0 if code == "busy" else 1)
                owned.update(state="error", owner=None, lease_until=None, error=code, attempts=attempt,
                    next_at=(datetime.now(UTC) + timedelta(seconds=5 if code == "busy" else min(1800, 30 * 2 ** min(attempt, 5)))).isoformat())
                if attempt >= 3:
                    owned["run"] = None
                store(db, workspace, settings["connection_id"], owned)
        return False

    def run_next(self):
        """One resumable page in one workspace; no provider-owned polling loop."""
        pending = getattr(self, "_pending_scopes", [])
        if not pending:
            with self.accounts.transaction() as db:
                pending = list(db.execute(text("SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars())
            self._pending_scopes = pending
        if not pending:
            return False
        return bool(self.step(pending.pop(0)))

    def run(self):
        while not self.stopping.is_set():
            try:
                with self.accounts.transaction() as db:
                    scopes = list(db.execute(text("SELECT workspace_id FROM alfa_max_sync_queue")).scalars())
                for scope in scopes:
                    if not self.stopping.is_set():
                        self.step(scope)
            except Exception:
                logging.getLogger(__name__).warning("Active review scheduler will resume")
            self.stopping.wait(3)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set(); self.thread.join(timeout=25)
