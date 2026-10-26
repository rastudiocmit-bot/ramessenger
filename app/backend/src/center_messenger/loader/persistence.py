"""Tenant-scoped, atomic page checkpoints using the existing durable outbox.

Internal API only: caller must authorize connection setup and supply trusted
Scope values. No HTTP header, provider payload or model can choose a workspace.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from .contracts import Page, Scope, identifier

STREAM = "loader.stream.v1"
EVENT = "loader.source.v1"
FOLLOWUP = "loader.followup.v1"


def key(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def encoded(value):
    result = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(result.encode()) > 1024 * 1024:
        raise ValueError("Loader metadata exceeds one MiB; store media separately")
    return result


class LostClaim(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    scope: Scope
    owner: str
    generation: int
    cursor: dict
    attempts: int


class PageStore:
    def __init__(self, accounts, *, clock=lambda: datetime.now(UTC)):
        self.accounts, self.clock = accounts, clock

    @staticmethod
    def _key(scope):
        return key(str(scope.connection), scope.provider, scope.stream)

    def _load(self, db, scope, *, lock=False):
        return db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
            "AND event_key=:key" + (" FOR UPDATE" if lock else "")),
            {"w": scope.workspace, "t": STREAM, "key": self._key(scope)}).scalar_one_or_none()

    def _write(self, db, scope, payload):
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES (:w,:id,:t,:key,CAST(:p AS jsonb),'published') "
            "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload"),
            {"w": scope.workspace, "id": uuid4(), "t": STREAM, "key": self._key(scope), "p": encoded(payload)})

    def configure(self, scope, *, generation, enabled=True):
        """Called by authorized setup; generation must increase on configuration changes."""
        if type(generation) is not int or generation < 1:
            raise ValueError("Positive settings generation required")
        with self.accounts.transaction(workspace_id=scope.workspace) as db:
            # Serialize first insertion too, and follow the existing workspace lock order.
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": scope.workspace})
            saved = self._load(db, scope, lock=True)
            if saved and generation < saved["generation"]:
                raise ValueError("Stale loader settings")
            if saved and generation == saved["generation"]:
                if enabled != saved["enabled"]:
                    raise ValueError("Configuration changes require a new generation")
                return
            payload = dict(saved or {}, generation=generation, enabled=bool(enabled), paused=False,
                           provider=scope.provider, connection=str(scope.connection), stream=scope.stream,
                           cursor=(saved or {}).get("cursor", {}), owner=None, lease_until=None,
                           attempts=0, error_code=None, due_at=self.clock().isoformat())
            self._write(db, scope, payload)

    def claim(self, scope, *, seconds=90):
        if not 1 <= seconds <= 3600:
            raise ValueError("Invalid loader lease")
        with self.accounts.transaction(workspace_id=scope.workspace) as db:
            saved = self._load(db, scope, lock=True)
            now = self.clock()
            if not saved or not saved["enabled"] or saved.get("paused"):
                return None
            if datetime.fromisoformat(saved["due_at"]) > now or (saved.get("lease_until") and datetime.fromisoformat(saved["lease_until"]) > now):
                return None
            owner = str(uuid4())
            updated = dict(saved, owner=owner, lease_until=(now + timedelta(seconds=seconds)).isoformat())
            self._write(db, scope, updated)
            return Claim(scope, owner, saved["generation"], dict(saved["cursor"]), saved.get("attempts", 0))

    def _owned(self, db, claim):
        saved = self._load(db, claim.scope, lock=True)
        if (not saved or not saved["enabled"] or saved.get("paused") or saved.get("owner") != claim.owner
            or saved["generation"] != claim.generation or not saved.get("lease_until")
            or datetime.fromisoformat(saved["lease_until"]) <= self.clock()):
            raise LostClaim()
        return saved

    def commit(self, claim, page: Page, *, project=None, poll_seconds=15):
        page.validate()
        if not 0.1 <= poll_seconds <= 86400:
            raise ValueError("Invalid source poll interval")
        if page.has_more and page.cursor == claim.cursor:
            raise ValueError("Continuation cursor did not advance")
        now, scope = self.clock(), claim.scope
        with self.accounts.transaction(workspace_id=scope.workspace) as db:
            # Projection callbacks may lock the workspace; keep the common order.
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": scope.workspace})
            saved = self._owned(db, claim)
            inserted = 0
            for event in page.events:
                payload = {"provider": scope.provider, "connection": str(scope.connection),
                    "namespace": event.namespace, "source_id": event.source_id, "kind": event.kind,
                    "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
                    "source_timezone": str(event.occurred_at.tzinfo), "received_at": now.isoformat(),
                    "crm_id": event.crm_id, "data": event.payload}
                # Stream is deliberately absent: mirrored branches observe the same event.
                event_key = key(str(scope.connection), scope.provider, event.namespace, event.source_id)
                created = db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                    "VALUES (:w,:id,:t,:key,CAST(:p AS jsonb),:state) ON CONFLICT(workspace_id,topic,event_key) "
                    "DO NOTHING RETURNING id"), {"w": scope.workspace, "id": uuid4(), "t": EVENT,
                    "key": event_key, "p": encoded(payload), "state": "published" if project else "pending"}).scalar_one_or_none()
                if created:
                    if project:
                        project(db, scope, event)
                    inserted += 1
            for job in page.followups:
                # Payload is private to this tenant; no file bytes or credentials here.
                job_payload = dict(job, provider=scope.provider, connection=str(scope.connection))
                db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                    "VALUES (:w,:id,:t,:key,CAST(:p AS jsonb),'pending') "
                    "ON CONFLICT(workspace_id,topic,event_key) DO NOTHING"),
                    {"w": scope.workspace, "id": uuid4(), "t": FOLLOWUP,
                     "key": key(str(scope.connection), scope.provider, job["kind"], job["key"]), "p": encoded(job_payload)})
            delay = .1 if page.has_more else poll_seconds
            self._write(db, scope, dict(saved, cursor=page.cursor, owner=None, lease_until=None,
                attempts=0, error_code=None, last_success_at=now.isoformat(), has_more=page.has_more,
                added=inserted, due_at=(now + timedelta(seconds=delay)).isoformat()))
        return inserted

    def fail(self, claim, code, *, retryable=True, delay=30):
        identifier(code)
        if not 0.1 <= delay <= 86400:
            raise ValueError("Invalid source retry")
        with self.accounts.transaction(workspace_id=claim.scope.workspace) as db:
            saved = self._owned(db, claim)
            self._write(db, claim.scope, dict(saved, owner=None, lease_until=None,
                attempts=saved.get("attempts", 0) + 1, error_code=code, paused=not retryable,
                due_at=(self.clock() + timedelta(seconds=delay)).isoformat()))

    def status(self, scope):
        # Internal method; HTTP callers must first authorize this exact workspace.
        with self.accounts.transaction(workspace_id=scope.workspace, snapshot=True) as db:
            saved = self._load(db, scope)
            if not saved:
                return {"configured": False}
            return {"configured": True, **{name: saved.get(name) for name in (
                "enabled", "paused", "generation", "error_code", "attempts", "due_at", "last_success_at", "has_more", "added")}}
