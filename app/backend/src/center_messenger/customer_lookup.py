"""Durable point lookups for source CRM IDs, independent of browser polling.

One job per workspace/CRM connection/Customer ID (IDs are global within that
CRM connection). Branches are observations, never a phone/name matching rule.
Only exact-ID queries are allowed, including the archived-card fallback.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from center_messenger.active_review import apply_customer
from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_repair import repair_batch
from center_messenger.auth_security import AuthError
from center_messenger.crm_api import DirectoryReader
from center_messenger.crm_directory import customer_row, page_rows, positive_id

TOPIC = "crm.customer.lookup"
LABELS = {
    "queued": "Карточка ожидает загрузки из CRM",
    "running": "Загружаем карточку по ID CRM",
    "retry": "CRM временно недоступна; повтор запланирован",
    "not_found": "CRM не вернула карточку с этим ID, включая архив",
    "unsupported_id": "CRM не подтвердила поиск по точному ID",
    "credentials_required": "Нужно проверить ключ доступа к CRM",
    "access_denied": "Нет доступа к филиалу этой карточки",
    "waiting_directory": "Ожидаем первоначальную загрузку справочника CRM",
    "resolved": "Карточка загружена",
}


def _write(db, workspace, key, payload):
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') "
        "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload"),
        {"w": workspace, "id": uuid4(), "t": TOPIC, "key": key, "p": json.dumps(payload)})
    # The existing outbox trigger only knows the older topics. Invalidate both
    # DB-backed read versions explicitly, in the same workspace transaction.
    db.execute(text("INSERT INTO chat_read_versions(workspace_id,revision,card_revision) VALUES(:w,1,1) "
        "ON CONFLICT(workspace_id) DO UPDATE SET revision=chat_read_versions.revision+1,"
        "card_revision=chat_read_versions.card_revision+1"), {"w": workspace})


def enqueue(db, workspace, crm, customer, branch):
    """Caller holds workspace lock; commit together with the source message."""
    customer, branch = positive_id(customer), positive_id(branch)
    key = f"{crm}:{customer}"
    previous = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w "
        "AND topic=:t AND event_key=:key"), {"w": workspace, "t": TOPIC, "key": key}).scalar_one_or_none()
    value = dict(previous or {})
    branches = sorted(set(value.get("branches", [])) | {branch})
    if previous and branches == value["branches"] and value["state"] != "resolved":
        return
    value.update(connection=str(crm), customer_id=customer, branches=branches)
    if not value.get("lease_until"):
        value.update(state="queued", next_at=datetime.now(UTC).isoformat(), tried=[])
    _write(db, workspace, key, value)


def public(payload, external):
    if not external.startswith("alfa-") or external.startswith("alfa-contact:"):
        return None
    if external.startswith("alfa-unlinked:"):
        return {"state": "missing_id", "text": "В сообщении CRM отсутствует ID клиента"}
    if not payload:
        return {"state": "queued", "text": LABELS["queued"]}
    return {"state": payload["state"], "text": LABELS.get(payload["state"], LABELS["retry"]),
            "customer_id": str(payload["customer_id"]), "branches": payload["branches"],
            "next_at": payload.get("next_at") if payload["state"] not in {"queued", "running", "resolved"} else None,
            "checked_at": payload.get("checked_at")}


def enrich(db, workspace, rows):
    """One bounded DB read for the visible list/history; never calls CRM."""
    keys = [f"{r['crm_connection_id']}:{r['external_chat_id'].split(':')[2]}" for r in rows
            if not r["contact_id"] and r.get("crm_connection_id")
            and r["external_chat_id"].startswith("alfa-customer:")]
    jobs = dict(db.execute(text("SELECT event_key,payload FROM outbox_events WHERE workspace_id=:w "
        "AND topic=:t AND event_key=ANY(CAST(:keys AS text[]))"),
        {"w": workspace, "t": TOPIC, "keys": keys}).all()) if keys else {}
    return [dict(r, identity_resolution=public(jobs.get(
        f"{r.get('crm_connection_id')}:{r['external_chat_id'].split(':')[-1]}"), r["external_chat_id"])
        if not r["contact_id"] else None) for r in rows]


class CustomerLookupWorker:
    def __init__(self, connections, native):
        self.connections, self.native, self.accounts = connections, native, connections.accounts

    def _seed(self, db, workspace, crm):
        # Resume unknown IDs saved before this feature was installed. At most
        # twenty lightweight aliases per turn; no CRM history/archive scan.
        rows = db.execute(text("SELECT DISTINCT c.external_chat_id FROM conversations c "
            "JOIN alfa_max_settings am ON am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id "
            "WHERE c.workspace_id=:w AND am.crm_connection_id=:crm AND c.contact_id IS NULL "
            "AND NOT c.crm_link_manual AND c.external_chat_id ~ '^alfa-customer:[1-9][0-9]*:[1-9][0-9]*$' "
            "AND NOT EXISTS(SELECT 1 FROM outbox_events e WHERE e.workspace_id=c.workspace_id AND e.topic=:t "
            "AND e.event_key=concat(CAST(:crm AS text),':',split_part(c.external_chat_id,':',3)) "
            "AND e.payload->'branches' @> jsonb_build_array(split_part(c.external_chat_id,':',2)::bigint)) "
            "ORDER BY c.external_chat_id LIMIT 20"), {"w": workspace, "crm": crm, "t": TOPIC}).scalars().all()
        for external in rows:
            _, branch, customer = external.split(":")
            enqueue(db, workspace, crm, customer, branch)

    def step(self, workspace):
        now = datetime.now(UTC)
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            settings = self.connections._row(db, workspace)
            if not settings:
                return False
            self._seed(db, workspace, settings["connection_id"])
            snapshot = DirectoryReader._published(db, workspace)
            if snapshot and snapshot["connection_id"] != settings["connection_id"]:
                snapshot = None
            job = db.execute(text("SELECT event_key,payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
                "AND payload->>'connection'=:crm AND payload->>'state'<>'resolved' "
                "AND (payload->>'lease_until' IS NULL OR (payload->>'lease_until')::timestamptz<=:now) "
                "AND ((payload->>'next_at')::timestamptz<=:now OR payload->>'version' IS DISTINCT FROM :v "
                "OR payload->>'run' IS DISTINCT FROM :run) ORDER BY created_at,event_key LIMIT 1 FOR UPDATE"),
                {"w": workspace, "t": TOPIC, "crm": str(settings["connection_id"]), "now": now,
                 "v": str(settings["version"]), "run": str(snapshot["run"]) if snapshot else None}).mappings().one_or_none()
            if not job:
                return False
            key, value = job["event_key"], dict(job["payload"])
            branches = list(db.execute(text("SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                "AND connection_id=:c AND available ORDER BY external_id"),
                {"w": workspace, "c": settings["connection_id"]}).scalars())
            version, run = settings["version"], str(snapshot["run"]) if snapshot else None
            if value.get("version") != version or value.get("run") != run:
                value.update(tried=[], attempts=0)
            value.update(version=version, run=run)
            usable = [b for b in value["branches"] if b in branches and b not in value.get("tried", [])]
            if not settings["secret_ciphertext"] or not snapshot or not usable:
                state = ("credentials_required" if not settings["secret_ciphertext"] else
                         "waiting_directory" if not snapshot else
                         "not_found" if any(b in branches for b in value["branches"]) else "access_denied")
                value.update(state=state, next_at=(now + timedelta(minutes=5 if state == "waiting_directory" else 360)).isoformat(), tried=[])
                _write(db, workspace, key, value)
                return False
            branch, owner = usable[0], str(uuid4())
            value.update(state="running", owner=owner, lease_until=(now + timedelta(seconds=60)).isoformat())
            _write(db, workspace, key, value)
            observed_at = db.execute(text('SELECT clock_timestamp()')).scalar_one()
        error_code, retry_after, rows, archive = None, None, [], False
        try:
            with self.connections._host_gate(settings["origin"]) as gate:
                credentials = self.connections.vault.open(workspace, settings["connection_id"], settings["secret_ciphertext"])
                client = self.connections.client_factory(settings["origin"], settings["api_email"], credentials, gate)
                for removed in (0, 2):
                    result = client.call(f"/v2api/{branch}/customer/index",
                        {"id": value["customer_id"], "is_study": 2, "removed": removed, "page": 0})
                    rows, total = page_rows(result, 0)
                    if total != len(rows) or len(rows) > 1 or any(str(r.get("id")) != str(value["customer_id"]) for r in rows):
                        error_code = "unsupported_id"
                        rows = []
                        break
                    if rows:
                        archive = removed == 2
                        customer_row(rows[0], archive=archive)
                        break
        except (AlfaError, AuthError) as error:
            error_code = getattr(error, "code", "busy" if getattr(error, "status", None) == 409 else "unavailable")
            retry_after = getattr(error, "retry_after", None)
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            current = self.connections._row(db, workspace)
            latest = DirectoryReader._published(db, workspace)
            saved = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
                "AND event_key=:key FOR UPDATE"), {"w": workspace, "t": TOPIC, "key": key}).scalar_one()
            now = datetime.now(UTC)
            if saved.get("owner") != owner or datetime.fromisoformat(saved["lease_until"]) <= now:
                return False
            value = dict(saved, owner=None, lease_until=None)
            allowed = db.execute(text("SELECT available FROM alfa_branches WHERE workspace_id=:w "
                "AND connection_id=:c AND external_id=:b"),
                {"w": workspace, "c": settings["connection_id"], "b": branch}).scalar_one_or_none()
            if (not current or current["connection_id"] != settings["connection_id"] or current["version"] != version
                    or not latest or str(latest["run"]) != run or latest["connection_id"] != settings["connection_id"] or not allowed):
                value.update(state="queued", next_at=now.isoformat(), tried=[])
                _write(db, workspace, key, value)
                return False
            if error_code:
                attempts = value.get("attempts", 0) + (error_code != "busy")
                delay = 5 if error_code == "busy" else min(1800, 30 * 2 ** min(attempts, 5))
                state = {"unauthorized": "credentials_required", "forbidden": "access_denied",
                         "unsupported_id": "unsupported_id"}.get(error_code, "retry")
                if state != "retry":
                    delay = 21600
                try:
                    delay = max(delay, min(86400, float(retry_after))) if retry_after is not None else delay
                except (TypeError, ValueError):
                    pass
                value.update(state=state, error_code=error_code, attempts=attempts, next_at=(now + timedelta(seconds=delay)).isoformat())
            elif rows:
                # Identity and messages become visible atomically; finance and
                # lessons stay in the regular card cache queue.
                fact = apply_customer(db, workspace, settings, snapshot, dict(rows[0], _branch=branch),
                                      archive=archive, observed_at=observed_at, source='lookup')
                channels = db.execute(text("SELECT connection_id FROM alfa_max_settings WHERE workspace_id=:w "
                    "AND crm_connection_id=:c"), {"w": workspace, "c": settings["connection_id"]}).scalars().all()
                for channel in channels:
                    repair_batch(self.native, db, workspace, channel, {}, only_contacts=[UUID(fact["contact_id"])])
                db.execute(text("UPDATE outbox_events SET state='published' WHERE workspace_id=:w "
                    "AND topic='crm.identity.repair' AND event_key=:key"),
                    {"w": workspace, "key": f"{settings['connection_id']}:{fact['contact_id']}"})
                value.update(state="resolved", contact_id=fact["contact_id"], error_code=None, attempts=0, next_at=None)
            else:
                tried = sorted(set(value.get("tried", [])) | {branch})
                more = any(b in branches and b not in tried for b in value["branches"])
                value.update(state="queued" if more else "not_found", tried=tried if more else [],
                    next_at=(now + timedelta(seconds=0 if more else 21600)).isoformat(), error_code=None)
            value["checked_at"] = now.isoformat()
            _write(db, workspace, key, value)
            if not error_code:
                db.execute(text("UPDATE alfa_settings SET secret_ciphertext=:s WHERE workspace_id=:w AND version=:v "
                    "AND secret_ciphertext=:previous"),
                    {"w": workspace, "v": version, "previous": settings["secret_ciphertext"],
                     "s": self.connections.vault.seal(workspace, settings["connection_id"], credentials)})
        return True

    def run_next(self):
        scopes = getattr(self, "_scopes", [])
        if not scopes:
            with self.accounts.transaction() as db:
                scopes = list(db.execute(text("SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id")).scalars())
            self._scopes = scopes
        return bool(scopes and self.step(scopes.pop(0)))
