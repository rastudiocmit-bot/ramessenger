"""Recovery policy and durable page checkpoints for private CRM card sections.

Checkpoint responses are intermediate data, never a published customer card.
All writes require the current lease, settings version and directory snapshot.
"""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from center_messenger.alfa_client import AlfaError, retry_seconds
from center_messenger.crm_directory import page_rows

TOPICS = ("crm.card.refresh", "crm.card.lessons", "crm.card.lesson-history", "crm.customer.groups")
BLOCKS = "crm.card.block"
LABELS = {
    "credentials_required": "CRM не приняла ключ. Обновление приостановлено до исправления подключения.",
    "access_denied": "CRM запретила эту операцию в филиале. Остальные разделы продолжают обновляться.",
    "rate_limited": "CRM ограничила запросы. Повтор запланирован после указанной ею паузы.",
    "not_found": "CRM не вернула клиента с этим ID, включая архив. Повторная проверка через сутки.",
    "invalid_response": "CRM вернула неполные или неподдерживаемые данные. Сохранён предыдущий раздел.",
    "crm_unavailable": "CRM временно недоступна. Сохранены последние сведения; повтор запланирован.",
    "busy": "Подключение занято. Ожидаем свободный канал.",
    "quota_wait": "Достигнут лимит API загрузчика. Контрольная точка сохранена; продолжим после освобождения квоты.",
    "send_pending": "Фоновое обновление уступило отправке сообщения. Контрольная точка сохранена.",
}


def block_sql(alias="e"):
    return f"""EXISTS(SELECT 1 FROM outbox_events block WHERE block.workspace_id={alias}.workspace_id
      AND block.topic='{BLOCKS}' AND block.state='published'
      AND block.payload->>'connection'={alias}.payload->>'connection'
      AND block.payload->>'version'={alias}.payload->>'version'
      AND (block.payload->>'branch' IS NULL OR
           (block.payload->>'branch'={alias}.payload->>'branch' AND block.payload->>'operation'={alias}.topic))
      AND (block.payload->>'until' IS NULL OR (block.payload->>'until')::timestamptz>now())
      AND NOT (block.payload->>'error'='access_denied'
        AND coalesce(({alias}.payload->>'force_at')::timestamptz,'epoch')>(block.payload->>'blocked_at')::timestamptz))"""


def blocks(db, workspace, connection, version):
    return list(db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND state='published' AND payload->>'connection'=:c AND payload->>'version'=:v "
        "AND (payload->>'until' IS NULL OR (payload->>'until')::timestamptz>now())"),
        {"w": workspace, "t": BLOCKS, "c": str(connection), "v": str(version)}).scalars())


def effective_job(job, active_blocks):
    if (job.get("force_at") and job.get("failed_at") and datetime.fromisoformat(job["force_at"]) > datetime.fromisoformat(job["failed_at"])
        and job.get("error") in {"access_denied", "not_found", "invalid_response", "crm_unavailable"}):
        job = dict(job, paused=False, available_at=job["force_at"], error=None)
    for block in sorted(active_blocks, key=lambda x: x["error"] != "credentials_required"):
        if block.get("branch") and (block["branch"] != job["branch"] or block["operation"] != job["topic"]):
            continue
        if block["error"] == "access_denied" and job.get("force_at") and datetime.fromisoformat(job["force_at"]) > datetime.fromisoformat(block["blocked_at"]):
            continue
        return dict(job, error=block["error"], available_at=block.get("until"),
                    paused=block.get("until") is None, http_status=block.get("http_status"))
    return job


def blocked(db, workspace, job):
    return bool(db.execute(text("SELECT " + block_sql() + " FROM outbox_events e "
        "WHERE e.workspace_id=:w AND e.id=:id"), {"w": workspace, "id": job["id"]}).scalar_one_or_none())


def failure(error, attempts, *, busy=False, now=None, policy=None):
    policy = policy or {"busy_retry_seconds": 5, "retry_initial_seconds": 30,
                        "retry_max_seconds": 1800}
    now = now or datetime.now(UTC)
    original = getattr(error, "code", "unexpected_" + type(error).__name__ if error is not None else "unexpected")
    status = getattr(error, "status", None)
    attempts = int(attempts) + (not busy)
    code, delay, scope = "crm_unavailable", min(policy["retry_max_seconds"],
        policy["retry_initial_seconds"] * 2 ** min(max(attempts-1, 0), 12)), None
    if busy:
        code, delay = "busy", policy["busy_retry_seconds"]
    elif original in {"quota_wait", "send_pending"}:
        code, delay = original, error.seconds
        attempts = int(attempts) - 1
    elif status == 401 or original == "unauthorized":
        code, delay, scope = "credentials_required", None, "connection"
    elif status == 403 or original == "forbidden":
        code, delay, scope = "access_denied", None, "operation"
    elif status == 429 or original == "rate_limited":
        code, scope = "rate_limited", "connection"
        delay = max(delay, retry_seconds(getattr(error, "retry_after", None), now=now) or 0)
    elif original == "not_found":
        code, delay = "not_found", 86400
    elif original in {"invalid_response", "import_limit", "unsafe_address"}:
        code, delay = "invalid_response", policy["retry_max_seconds"]
    if delay is not None:
        delay = min(delay, (datetime.max.replace(tzinfo=UTC)-now).total_seconds()-1)
    return {"error": code, "cause": original, "http_status": status, "attempts": attempts,
            "available_at": (now + timedelta(seconds=delay)).isoformat() if delay is not None else None,
            "failed_at": now.isoformat(), "paused": delay is None}, scope


def save_block(db, workspace, job, failure, scope):
    if not scope:
        return
    value = {"connection": job["connection"], "version": job["version"],
             "error": failure["error"], "http_status": failure["http_status"],
             "blocked_at": failure["failed_at"], "until": failure["available_at"]}
    if scope == "operation":
        value.update(branch=job["branch"], operation=job["topic"])
    key = f"{job['connection']}:{job['version']}:{failure['error']}:{value.get('branch', '*')}:{value.get('operation', '*')}"
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
        "DO UPDATE SET payload=EXCLUDED.payload"),
        {"w": workspace, "id": uuid4(), "t": BLOCKS, "key": key, "p": json.dumps(value)})


def clear_verified_denial(db, workspace, job):
    if not job.get("force_at"):
        return
    db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND payload->>'connection'=:c AND payload->>'version'=:v AND payload->>'branch'=:b "
        "AND payload->>'operation'=:op AND payload->>'error'='access_denied' "
        "AND (payload->>'blocked_at')::timestamptz<CAST(:force AS timestamptz)"),
        {"w": workspace, "t": BLOCKS, "c": job["connection"], "v": str(job["version"]),
         "b": job["branch"], "op": job["topic"], "force": job["force_at"]})


def resume_settings(db, workspace, connection, version):
    """Same CRM/account, new credentials: retain facts and requeue unfinished work."""
    db.execute(text("UPDATE alfa_family_details SET settings_version=:v WHERE workspace_id=:w AND connection_id=:c"),
               {"w": workspace, "c": connection, "v": version})
    db.execute(text("UPDATE crm_family_snapshots SET settings_version=:v WHERE workspace_id=:w AND connection_id=:c"),
               {"w": workspace, "c": connection, "v": version})
    db.execute(text("UPDATE outbox_events SET payload=(payload-'checkpoint'-'lease_owner'-'lease_until' "
        "-'error'-'cause'-'http_status'-'failed_at'-'stage'-'page'-'paused')||jsonb_build_object("
        "'version',:v,'attempts',0,'available_at',now(),'force_at',clock_timestamp()) "
        "WHERE workspace_id=:w AND topic=ANY(CAST(:topics AS text[])) AND state='pending' "
        "AND payload->>'connection'=CAST(:c AS text)"),
        {"w": workspace, "c": connection, "v": version, "topics": list(TOPICS)})


class PageYield(Exception):
    """A bounded turn finished; resume from durable pages on the next turn."""


class LostCardLease(Exception):
    pass


class CheckpointClient:
    def __init__(self, client, checkpoint, persist, *, limit=3):
        self.client, self.checkpoint, self.persist = client, checkpoint, persist
        self.limit, self.calls, self.stage, self.page = limit, 0, "prepare", None

    def call(self, path, body):
        self.stage = path.split("/")[-2]  # Fixed API operation, never the URL/query or response text.
        self.page = body.get("page", 0)
        key = hashlib.sha256(json.dumps([path, body], sort_keys=True).encode()).hexdigest()
        previous = self.checkpoint["responses"].get(key)
        if previous is not None:
            return previous
        if self.calls >= self.limit:
            raise PageYield()
        self.calls += 1
        result = self.client.call(path, body)
        page_rows(result, self.page)
        self.checkpoint["responses"][key] = result
        if len(json.dumps(self.checkpoint).encode()) > 8 * 1024 * 1024:
            raise AlfaError("import_limit")
        self.checkpoint.update(stage=self.stage, page=self.page, pages=len(self.checkpoint["responses"]))
        self.persist(self.checkpoint)
        return result
