"""Personal, workspace-scoped display choices; no contact identity is modified."""
import json
from uuid import uuid4
from sqlalchemy import text

TOPIC = "ui.chat.preferences"


def read(db, workspace, user):
    payload = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND event_key=:u"), {"w": workspace, "t": TOPIC, "u": str(user)}).scalar_one_or_none() or {}
    return {"name_order": payload.get("name_order", "parent_first")}


def save(db, workspace, user, order):
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:t,:u,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
        "DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        {"w": workspace, "id": uuid4(), "t": TOPIC, "u": str(user), "p": json.dumps({"name_order": order})})
    return {"name_order": order}
