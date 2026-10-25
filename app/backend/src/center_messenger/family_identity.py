"""Repair published child bindings by Customer ID without reimporting messages."""

import json
from uuid import uuid4

from sqlalchemy import text

from center_messenger.crm_directory import student_bindings

TOPIC = "crm.student.identity"
VERSION = 1


def repair(db, workspace, snapshot, *, limit=100):
    # Same lock order as publication/settings writers. All changed branches of
    # one Customer ID are committed together; cached financial facts are ID-bound.
    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
    control = db.execute(text("SELECT published_id,connection_id FROM alfa_sync_control WHERE workspace_id=:w"),
                         {"w": workspace}).mappings().one_or_none()
    if not control or control["published_id"] != snapshot["run"] or control["connection_id"] != snapshot["connection_id"]:
        return 0
    params = {"w": workspace, "r": snapshot["run"], "conn": snapshot["connection_id"], "topic": TOPIC}
    state = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:topic "
        "AND event_key=CAST(:conn AS text)"), params).scalar_one_or_none() or {}
    if state.get("run") == str(snapshot["run"]) and state.get("version") == VERSION and state.get("complete"):
        return 0
    if state.get("run") != str(snapshot["run"]) or state.get("version") != VERSION:
        state = {"run": str(snapshot["run"]), "version": VERSION, "processed": 0, "after": 0}
    rows = list(db.execute(text("SELECT external_id,student_id,contact_id,name,branch_id FROM alfa_customers "
        "WHERE workspace_id=:w AND run_id=:r AND contact_id IS NOT NULL ORDER BY external_id,branch_id"), params).mappings())
    identities, ambiguous = student_bindings(snapshot["connection_id"], (r["external_id"] for r in rows), rows)
    keys = sorted({r["external_id"] for r in rows if r["external_id"] > state["after"]
                   and (r["student_id"] != identities[r["external_id"]] or r["external_id"] in ambiguous)})
    selected = set(keys[:limit])
    old_links = set()
    for customer in sorted(selected):
        sources = [r for r in rows if r["external_id"] == customer]
        student = identities[customer]
        db.execute(text("INSERT INTO students(workspace_id,id,display_name) VALUES(:w,:id,:name) "
            "ON CONFLICT(workspace_id,id) DO UPDATE SET display_name=EXCLUDED.display_name"),
            {"w": workspace, "id": student, "name": sources[0]["name"]})
        db.execute(text("UPDATE alfa_customers SET student_id=:s,needs_review=true "
            "WHERE workspace_id=:w AND run_id=:r AND external_id=:customer"),
            params | {"s": student, "customer": customer})
        for source in sources:
            contact = source["contact_id"]
            if source["student_id"]:
                old_links.add((contact, source["student_id"]))
            db.execute(text("INSERT INTO contact_student_links(workspace_id,contact_id,student_id,managed_by_alfa) "
                "VALUES(:w,:c,:s,true) ON CONFLICT DO NOTHING"), {"w": workspace, "c": contact, "s": student})
            db.execute(text("UPDATE alfa_directory SET needs_review=true WHERE workspace_id=:w AND run_id=:r AND contact_id=:c"),
                       params | {"c": contact})
    for contact, student in old_links:
        db.execute(text("DELETE FROM contact_student_links l WHERE l.workspace_id=:w AND l.contact_id=:c AND l.student_id=:s "
            "AND l.managed_by_alfa AND NOT EXISTS(SELECT 1 FROM alfa_customers a WHERE a.workspace_id=:w AND a.run_id=:r "
            "AND a.contact_id=l.contact_id AND a.student_id=l.student_id)"), params | {"c": contact, "s": student})
    state.update(processed=state["processed"] + len(selected), complete=len(keys) <= limit,
                 after=max(selected) if selected else state["after"])
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:topic,CAST(:conn AS text),CAST(:p AS jsonb),'published') "
        "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        params | {"id": uuid4(), "p": json.dumps(state)})
    return len(selected)
