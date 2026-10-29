"""Personal notification choices, scoped to the authenticated member and centre.

Only preferences are stored here; no message text, credentials or notification
history. The existing published-settings store needs no schema migration.
"""
import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, StrictBool
from sqlalchemy import text

from center_messenger.auth_security import InputModel

TOPIC = "ui.notification.preferences"
TASK_TOPIC = "tasks.preferences"
DEFAULTS = dict(sound_enabled=False, volume=55, client="soft", lead="clear",
                archive="low", service_popups=False, task_sound=False)
Tone = Literal["soft", "clear", "low", "off"]


class NotificationPreferencesInput(InputModel):
    sound_enabled: StrictBool | None = None
    volume: int | None = Field(default=None, ge=0, le=100, strict=True)
    client: Tone | None = None
    lead: Tone | None = None
    archive: Tone | None = None
    service_popups: StrictBool | None = None
    task_sound: StrictBool | None = None
    task_reminders: StrictBool | None = None


def read(db, workspace, user):
    rows = dict(db.execute(text("SELECT topic,payload FROM outbox_events WHERE workspace_id=:w "
        "AND event_key=:u AND topic IN (:n,:t)"),
        {"w": workspace, "u": str(user), "n": TOPIC, "t": TASK_TOPIC}).all())
    saved = rows.get(TOPIC) or {}
    task = rows.get(TASK_TOPIC) or {}
    return {"configured": bool(saved), **DEFAULTS, **{k: saved[k] for k in DEFAULTS if k in saved},
            "task_reminders": task.get("reminders", True)}


def _patch(db, workspace, user, topic, data):
    if not data:
        return
    # Merge only the changed keys inside PostgreSQL. A sound slider must not
    # overwrite another tab's tone choice or the task editor's personal options.
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:t,:u,CAST(:p AS jsonb),'published') "
        "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE "
        "SET payload=outbox_events.payload || EXCLUDED.payload,state='published'"),
        {"w": workspace, "id": uuid4(), "t": topic, "u": str(user), "p": json.dumps(data)})


def save(db, workspace, user, data):
    _patch(db, workspace, user, TOPIC, {k: data[k] for k in DEFAULTS if k in data})
    if "task_reminders" in data:
        _patch(db, workspace, user, TASK_TOPIC, {"reminders": data["task_reminders"]})
    return read(db, workspace, user)


def status(db, workspace):
    """Small local-only status; no secret envelopes, history or CRM requests."""
    crm = db.execute(text("SELECT c.status,a.last_error_code AS error_code, "
        "a.secret_ciphertext IS NOT NULL AS configured FROM alfa_settings a JOIN connections c "
        "ON c.workspace_id=a.workspace_id AND c.id=a.connection_id WHERE a.workspace_id=:w"),
        {"w":workspace}).mappings().one_or_none()
    maximum = db.execute(text("SELECT CASE WHEN m.state!='not_configured' AND a.connection_id IS NULL "
        "THEN 'reauth_required' ELSE m.state END AS state, "
        "m.history_cursor->'session_control'->>'phase' AS phase FROM alfa_max_settings m "
        "LEFT JOIN alfa_settings a ON a.workspace_id=m.workspace_id AND a.connection_id=m.crm_connection_id "
        "AND a.secret_ciphertext IS NOT NULL WHERE m.workspace_id=:w"), {"w":workspace}).mappings().one_or_none()
    return {"crm":dict(crm) if crm else None,
            "max": {"configured":maximum['state']!='not_configured',"state":maximum['state'],
                    "recovery":{"phase":maximum['phase']}} if maximum else None}
