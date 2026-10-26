"""Current CRM identity findings; historical repairs are not unresolved errors."""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from center_messenger.crm_directory import normalized_name, phone_number

TOPIC = "crm.card.review.scan"
VERSION = 1
LIMIT = 2000
FIELDS = ("a.external_id,a.branch_id,a.contact_id,a.student_id,a.name,a.legal_name,"
          "a.legal_type,a.dob,a.phones,a.emails")
SOURCE = (" FROM alfa_customers a JOIN alfa_branches b ON b.workspace_id=a.workspace_id "
          "AND b.connection_id=:conn AND b.external_id=a.branch_id AND b.available "
          "WHERE a.workspace_id=:w AND a.run_id=:r ")
LABELS = {
    "customer_name_conflict": "У одного ID AlfaCRM различаются ФИО ребёнка в филиалах.",
    "customer_birthdate_conflict": "У одного ID AlfaCRM различаются даты рождения ребёнка.",
    "customer_binding_conflict": "Один ID AlfaCRM связан с разными внутренними карточками ребёнка.",
    "shared_student_binding": "Разные ID AlfaCRM связаны с одной внутренней карточкой ребёнка.",
    "conflicting_parents": "В карточках с общим контактом указаны разные представители.",
    "shared_business_phone": "Общий телефон организации используется разными контактами.",
    "family_not_verified": "Принадлежность карточек с общим телефоном одной семье не подтверждена.",
    "phone_chain": "Основные телефоны связанных карточек противоречат друг другу.",
    "too_many_candidates": "Слишком много карточек с общим телефоном для однозначного выбора.",
    "conflicting_customer": "У связанной карточки расходятся сведения о ребёнке между филиалами.",
    "review_incomplete": "Недостаточно доступных данных для полной проверки связей карточки.",
}


def person_name(value, phones):
    return normalized_name(value) if value and phone_number(value) not in phones else ""


class ReviewIndex:
    def __init__(self, rows, *, identities=None, contacts=None, binding_rows=()):
        self.customers, self.contacts, self.phones = defaultdict(list), defaultdict(list), defaultdict(list)
        self.codes, self.contact_codes = defaultdict(set), {}
        owners = defaultdict(set)
        for raw in rows:
            row = dict(raw)
            customer = int(row["external_id"])
            row.update(name=row.get("name") or "", legal_name=row.get("legal_name") or "",
                       legal_type=row.get("legal_type", 1), phones=row.get("phones") or [],
                       emails=row.get("emails") or [], dob=row.get("dob"))
            for field in ("name", "legal_name"):
                if phone_number(row[field]):
                    row[field] = ""
            contact = (contacts or {}).get(customer, row.get("contact_id"))
            row["contact_id"] = str(contact) if contact else "customer:" + str(customer)
            student = identities.get(customer) if identities is not None else row.get("student_id")
            row["student_id"] = str(student) if student else None
            self.customers[customer].append(row)
            self.contacts[row["contact_id"]].append(row)
            if row["phones"]:
                self.phones[row["phones"][0]].append(row)
            if student:
                owners[str(student)].add(customer)
        for row in binding_rows:
            if row["student_id"]:
                owners[str(row["student_id"])].add(int(row["external_id"]))
        for phone, values in list(self.phones.items()):
            self.phones[phone] = [row for contact in sorted({r["contact_id"] for r in values})
                                  for row in self.contacts[contact]]
        for customer, values in self.customers.items():
            phones = {p for row in values for p in row["phones"]}
            if len({name for row in values if (name := person_name(row["name"], phones))}) > 1:
                self.codes[customer].add("customer_name_conflict")
            if len({str(row["dob"]) for row in values if row["dob"]}) > 1:
                self.codes[customer].add("customer_birthdate_conflict")
            if len({row["student_id"] for row in values if row["student_id"]}) > 1:
                self.codes[customer].add("customer_binding_conflict")
            if any(len(owners[row["student_id"]]) > 1 for row in values if row["student_id"]):
                self.codes[customer].add("shared_student_binding")
        # Email is a primary identity address only when a phone is absent.
        email_names, email_customers = defaultdict(set), defaultdict(set)
        for customer, values in self.customers.items():
            for row in values:
                if not row["phones"] and row["emails"]:
                    address = row["emails"][0]
                    email_customers[address].add(customer)
                    name = person_name(row["legal_name"], set())
                    if name:
                        email_names[address].add(name)
        for email, names in email_names.items():
            if len(names) > 1:
                for customer in email_customers[email]:
                    self.codes[customer].add("conflicting_parents")

    def reasons(self, customers):
        from center_messenger.card_preference import _cohort

        findings = defaultdict(set)
        for customer in set(map(int, customers)):
            codes = set(self.codes[customer])
            for contact in {row["contact_id"] for row in self.customers[customer]}:
                if contact not in self.contact_codes:
                    self.contact_codes[contact] = _cohort(contact, self.contacts, self.phones)[3]
                if self.contact_codes[contact]:
                    codes.add(self.contact_codes[contact])
            for code in codes:
                findings[code].add(str(customer))
        return [{"code": code, "text": LABELS[code], "customer_ids": sorted(ids, key=int),
                 "branch_ids": sorted({str(row["branch_id"]) for customer in ids
                                       for row in self.customers[int(customer)] if row.get("branch_id")}, key=int)}
                for code, ids in sorted(findings.items())]

    def contact_reasons(self, contact):
        return self.reasons(row["external_id"] for row in self.contacts[str(contact)])


def cached_status(db, workspace, snapshot, cards, *, selection=None):
    """Read only related saved identities, never the whole CRM or an external API."""
    params = {"w": workspace, "r": snapshot["run"], "conn": snapshot["connection_id"]}
    incomplete = len(cards) > 1000

    def read(predicate, **values):
        nonlocal incomplete
        rows = list(db.execute(text("SELECT " + FIELDS + SOURCE + predicate + " LIMIT :limit"),
                               params | values | {"limit": LIMIT + 1}).mappings())
        incomplete |= len(rows) > LIMIT
        return rows[:LIMIT]

    customer_ids = sorted({int(row["external_id"]) for row in cards})
    if not customer_ids:
        return {"needs_review": True, "review_reasons": [
            {"code": "review_incomplete", "text": LABELS["review_incomplete"], "customer_ids": []}]}
    own = read("AND a.external_id=ANY(CAST(:ids AS bigint[]))", ids=customer_ids)
    incomplete |= not set(customer_ids).issubset({int(row["external_id"]) for row in own})
    phones = list({row["phones"][0] for row in own if row["phones"]})
    emails = list({row["emails"][0] for row in own if not row["phones"] and row["emails"]})
    related = list(own)
    if phones:
        related += read("AND a.phones->>0=ANY(CAST(:phones AS text[]))", phones=phones)
    if emails:
        related += read("AND a.phones->>0 IS NULL AND a.emails->>0=ANY(CAST(:emails AS text[]))", emails=emails)
    contacts = list({row["contact_id"] for row in related if row["contact_id"]})
    if contacts:
        related += read("AND a.contact_id=ANY(CAST(:contacts AS uuid[]))", contacts=contacts)
    unique = {(row["branch_id"], row["external_id"]): row for row in related}
    students = list({row["student_id"] for row in own if row["student_id"]})
    bindings = read("AND a.student_id=ANY(CAST(:students AS uuid[]))", students=students) if students else []
    index = ReviewIndex(unique.values(), binding_rows=bindings)
    reasons = index.reasons(customer_ids)
    if selection and selection.get("ambiguous") == "conflicting_customer":
        # A refused automatic redirect can concern another candidate, rather
        # than the displayed source card. Confirm the current contradiction;
        # neither trust the old hint nor expose another family's IDs or names.
        from center_messenger.card_preference import _cohort
        source = str(selection.get("source"))
        candidates = _cohort(source, index.contacts, index.phones)[2] if source in index.contacts else []
        conflicts = {"customer_name_conflict", "customer_birthdate_conflict"}
        if any(index.codes[int(row["external_id"])] & conflicts for row in candidates):
            reasons.append({"code": "conflicting_customer", "text": LABELS["conflicting_customer"],
                            "customer_ids": []})
    if incomplete:
        reasons.append({"code": "review_incomplete", "text": LABELS["review_incomplete"],
                        "customer_ids": list(map(str, customer_ids))})
    return {"needs_review": bool(reasons), "review_reasons": reasons}


def repair(db, workspace, snapshot, *, limit=100):
    """Resume derived-flag cleanup after child bindings have been repaired."""
    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
    params = {"w": workspace, "r": snapshot["run"], "conn": snapshot["connection_id"], "topic": TOPIC}
    valid = db.execute(text("SELECT 1 FROM alfa_sync_control c JOIN alfa_settings s ON "
        "s.workspace_id=c.workspace_id AND s.connection_id=c.connection_id WHERE c.workspace_id=:w "
        "AND c.connection_id=:conn AND c.published_id=:r AND s.version=:v"),
        params | {"v": snapshot["version"]}).scalar_one_or_none()
    if not valid:
        return 0
    identity = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w "
        "AND topic='crm.student.identity' AND event_key=CAST(:conn AS text)"), params).scalar_one_or_none()
    if identity and identity.get("run") == str(snapshot["run"]) and not identity.get("complete"):
        return 0
    state = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:topic "
        "AND event_key=CAST(:conn AS text)"), params).scalar_one_or_none() or {}
    now = datetime.now(UTC)
    same = state.get("run") == str(snapshot["run"]) and state.get("settings") == snapshot["version"] and state.get("policy") == VERSION
    if same and state.get("complete") and state.get("next_at") and datetime.fromisoformat(state["next_at"]) > now:
        return 0
    after = state.get("after") if same and not state.get("complete") else None
    rows = list(db.execute(text("SELECT " + FIELDS + SOURCE + "AND a.contact_id IS NOT NULL"), params).mappings())
    index = ReviewIndex(rows)
    contacts = sorted(contact for contact in index.contacts if after is None or contact > after)
    selected = contacts[:limit]
    for contact in selected:
        review = bool(index.contact_reasons(contact))
        values = params | {"c": UUID(contact), "review": review}
        db.execute(text("UPDATE alfa_directory SET needs_review=:review WHERE workspace_id=:w AND run_id=:r "
            "AND contact_id=:c AND needs_review IS DISTINCT FROM :review"), values)
        db.execute(text("UPDATE alfa_customers a SET needs_review=:review WHERE a.workspace_id=:w AND a.run_id=:r "
            "AND a.contact_id=:c AND a.needs_review IS DISTINCT FROM :review AND EXISTS(SELECT 1 FROM alfa_branches b "
            "WHERE b.workspace_id=a.workspace_id AND b.connection_id=:conn AND b.external_id=a.branch_id AND b.available)"), values)
    complete = len(contacts) <= limit
    progress = {"run": str(snapshot["run"]), "settings": snapshot["version"], "policy": VERSION,
                "after": selected[-1] if selected else after, "complete": complete,
                "processed": (state.get("processed", 0) if after else 0) + len(selected),
                "next_at": (now + timedelta(minutes=30)).isoformat() if complete else None}
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:topic,CAST(:conn AS text),CAST(:p AS jsonb),'published') "
        "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        params | {"id": uuid4(), "p": json.dumps(progress)})
    return len(selected)
