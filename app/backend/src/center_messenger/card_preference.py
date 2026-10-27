"""Persist a display card without changing CRM identities or MAX recipients.

Published outbox records are durable projections, never executable jobs. Every
read fences the projection by CRM settings, publication and the current identity
of the entire primary-phone cohort. Manual conversation links always win.
"""
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from center_messenger.alfa_family import current_lessons, _member_at
from center_messenger.crm_directory import normalized_name, phone_number

TOPIC = "crm.card.preference"
SCAN = TOPIC + ".scan"
VERSION = 1
REASONS = {5: "scheduled_lesson", 4: "active_group", 3: "active_client", 2: "lead", 1: "archive"}


def enabled(snapshot):
    return bool(snapshot and all(snapshot.get(k) is not None for k in ("run", "connection_id", "version", "today")))


def params(snapshot):
    return {"card_run": snapshot["run"], "card_connection": snapshot["connection_id"],
            "card_version": snapshot["version"]} if enabled(snapshot) else {}


def guard_piece(a="a"):
    return (f"jsonb_build_array({a}.external_id,{a}.branch_id,{a}.contact_id,{a}.student_id,"
            f"{a}.name,{a}.legal_name,{a}.legal_type,{a}.dob,{a}.phones,{a}.status)::text")


def join_sql(snapshot, *, c="c", cn="cn", am="am", alias="cp"):
    if not enabled(snapshot):
        return f" LEFT JOIN LATERAL (SELECT NULL::jsonb AS payload) {alias} ON true "
    # This guard also detects a new, conflicting owner of a formerly unique
    # phone, not just edits to the previously selected card.
    # Keep the two mutually exclusive contact/phone paths separate. A CASE in
    # the customer filter prevents indexed contact lookups and scans the entire
    # CRM catalog again for every pending dialog.
    return f""" LEFT JOIN LATERAL (
      SELECT e.payload FROM outbox_events e WHERE e.workspace_id={c}.workspace_id
        AND e.topic='{TOPIC}' AND e.event_key=concat(CAST(:card_connection AS text),':',{c}.contact_id)
        AND e.state='published' AND e.payload->>'policy'='{VERSION}'
        AND e.payload->>'run'=CAST(:card_run AS text) AND e.payload->>'version'=CAST(:card_version AS text)
        AND e.payload->>'source'={c}.contact_id::text AND NOT {c}.crm_link_manual
        AND {cn}.kind='alfa_max_browser' AND {am}.crm_connection_id=:card_connection
        AND e.payload->>'guard'=(
          SELECT md5(coalesce(string_agg(guarded.piece,'|' ORDER BY guarded.external_id,guarded.branch_id),''))
          FROM (
            SELECT a.external_id,a.branch_id,{guard_piece()} AS piece
            FROM alfa_customers a JOIN alfa_branches b ON b.workspace_id=a.workspace_id
              AND b.connection_id=:card_connection AND b.external_id=a.branch_id AND b.available
            WHERE e.payload->>'phone' IS NULL AND a.workspace_id=e.workspace_id
              AND a.run_id=:card_run AND a.contact_id={c}.contact_id
            UNION ALL
            SELECT a.external_id,a.branch_id,{guard_piece()} AS piece
            FROM alfa_customers a JOIN alfa_branches b ON b.workspace_id=a.workspace_id
              AND b.connection_id=:card_connection AND b.external_id=a.branch_id AND b.available
            WHERE e.payload->>'phone' IS NOT NULL AND a.workspace_id=e.workspace_id
              AND a.run_id=:card_run AND a.contact_id IN (
                SELECT anchor.contact_id FROM alfa_customers anchor JOIN alfa_branches ab ON ab.workspace_id=anchor.workspace_id
                  AND ab.connection_id=:card_connection AND ab.external_id=anchor.branch_id AND ab.available
                WHERE anchor.workspace_id=e.workspace_id AND anchor.run_id=:card_run
                  AND anchor.phones->>0=e.payload->>'phone')
          ) guarded)
    ) {alias} ON true """


def contact_sql(c="c", alias="cp"):
    return f"coalesce(({alias}.payload->>'contact')::uuid,{c}.contact_id)"


def family_sql(c="c", alias="cp"):
    return (f"CASE WHEN {alias}.payload IS NULL THEN ARRAY[{c}.contact_id] ELSE "
            f"ARRAY(SELECT value::uuid FROM jsonb_array_elements_text({alias}.payload->'contacts')) END")


def pending_sql(snapshot):
    """One prepared family per pending chat, including a displayed active alias."""
    return ("SELECT DISTINCT unnest(" + family_sql("q") + ") AS contact_id FROM conversations q "
            "JOIN connections cn ON cn.workspace_id=q.workspace_id AND cn.id=q.connection_id "
            "LEFT JOIN alfa_max_settings am ON am.workspace_id=q.workspace_id AND am.connection_id=q.connection_id "
            + join_sql(snapshot, c="q") +
            " WHERE q.workspace_id=:w AND q.kind='direct' AND q.needs_reply AND q.contact_id IS NOT NULL")


def is_pending(db, workspace, snapshot, contacts):
    contacts = tuple(contacts)
    if not contacts:
        return False
    # Start with the requested family and persisted alias candidates. Validate
    # only those candidates with the full identity guard, rather than computing
    # and unnesting every pending family's projection for one cached card read.
    # The candidate payload is a hint, never authority: stale/manual/changed
    # choices still go through join_sql and the final verified family overlap.
    source = ("CASE WHEN e.payload->>'source' ~ "
        "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' "
        "THEN (e.payload->>'source')::uuid END")
    aliases = (" UNION SELECT " + source + " FROM outbox_events e "
        "WHERE e.workspace_id=:w AND e.topic='" + TOPIC + "' AND e.state='published' "
        "AND e.payload->>'policy'='" + str(VERSION) + "' "
        "AND e.payload->>'run'=CAST(:card_run AS text) "
        "AND e.payload->>'version'=CAST(:card_version AS text) "
        "AND e.payload->'contacts' ?| CAST(:card_contact_text AS text[])") if enabled(snapshot) else ""
    return db.execute(text(
        "WITH candidates AS MATERIALIZED (SELECT unnest(CAST(:card_contacts AS uuid[])) AS contact_id" + aliases + ") "
        "SELECT EXISTS(SELECT 1 FROM candidates candidate JOIN conversations q ON q.contact_id=candidate.contact_id "
        "AND q.workspace_id=:w JOIN connections cn ON cn.workspace_id=q.workspace_id AND cn.id=q.connection_id "
        "LEFT JOIN alfa_max_settings am ON am.workspace_id=q.workspace_id AND am.connection_id=q.connection_id "
        + join_sql(snapshot, c="q") + " WHERE q.kind='direct' AND q.needs_reply AND "
        + family_sql("q") + " && CAST(:card_contacts AS uuid[]))"),
        {"w": workspace, **params(snapshot), "card_contacts": list(contacts),
         "card_contact_text": [str(contact) for contact in contacts]}).scalar_one()


def selected(db, workspace, snapshot, conversation, source):
    if not enabled(snapshot) or not conversation:
        return None
    return db.execute(text("SELECT cp.payload FROM conversations c JOIN connections cn ON "
        "cn.workspace_id=c.workspace_id AND cn.id=c.connection_id LEFT JOIN alfa_max_settings am ON "
        "am.workspace_id=c.workspace_id AND am.connection_id=c.connection_id " + join_sql(snapshot) +
        " WHERE c.workspace_id=:w AND c.id=:c AND c.contact_id=:source AND c.kind='direct'"),
        {"w": workspace, "c": conversation, "source": source, **params(snapshot)}).scalar_one_or_none()


def public(selection, *, manual=False):
    if not selection:
        return {"mode": "manual"} if manual else None
    return {key: selection.get(key) for key in ("source", "contact", "customer", "branch", "student", "reason",
        "signature", "selected_at", "checked_at", "evidence", "ambiguous")} | {"mode": "auto"}


def person_name(value, phones):
    """A phone entered into name/legal_name is missing data, not another payer."""
    if not value or phone_number(value) in phones:
        return ""
    return normalized_name(value)


def _catalog(db, workspace, snapshot, sources=None):
    # A card opening needs only its own contacts and all direct primary-phone
    # candidates, including every branch/card of those candidates. Keep the
    # same ambiguity guards; do not read the entire directory for one family.
    prefix, restriction = "", ""
    if sources is not None:
        prefix = """WITH source_phones AS MATERIALIZED (
          SELECT a.phones->>0 AS phone FROM alfa_customers a JOIN alfa_branches b
            ON b.workspace_id=a.workspace_id AND b.connection_id=:conn AND b.external_id=a.branch_id AND b.available
          WHERE a.workspace_id=:w AND a.run_id=:r AND a.contact_id=ANY(CAST(:sources AS uuid[]))
        ), candidates AS MATERIALIZED (
          SELECT unnest(CAST(:sources AS uuid[])) AS contact_id UNION
          SELECT a.contact_id FROM alfa_customers a JOIN alfa_branches b
            ON b.workspace_id=a.workspace_id AND b.connection_id=:conn AND b.external_id=a.branch_id AND b.available
          WHERE a.workspace_id=:w AND a.run_id=:r AND a.phones->>0 IN (SELECT phone FROM source_phones)
        ) """
        restriction = " AND a.contact_id IN (SELECT contact_id FROM candidates) "
    rows = list(db.execute(text(prefix + "SELECT a.external_id,a.branch_id,a.contact_id,a.student_id,a.name,a.legal_name,a.legal_type,"
        "a.dob,a.status,a.phones,d.display_name," + guard_piece() + " AS guard_piece FROM alfa_customers a "
        "JOIN alfa_branches b ON b.workspace_id=a.workspace_id AND b.connection_id=:conn "
        "AND b.external_id=a.branch_id AND b.available LEFT JOIN alfa_directory d ON d.workspace_id=a.workspace_id "
        "AND d.run_id=a.run_id AND d.contact_id=a.contact_id WHERE a.workspace_id=:w AND a.run_id=:r "
        "AND a.contact_id IS NOT NULL " + restriction + "ORDER BY a.external_id,a.branch_id"),
        {"w": workspace, "r": snapshot["run"], "conn": snapshot["connection_id"],
         "sources": list(map(str, sources or []))}).mappings())
    by_contact, by_phone = defaultdict(list), defaultdict(list)
    for raw in rows:
        row = dict(raw); row["contact_id"] = str(row["contact_id"])
        by_contact[row["contact_id"]].append(row)
        if row["phones"]:
            by_phone[row["phones"][0]].append(row)
    for phone, values in list(by_phone.items()):
        by_phone[phone] = [row for contact in sorted({r["contact_id"] for r in values}) for row in by_contact[contact]]
    return by_contact, by_phone


def _cohort(source, by_contact, by_phone):
    own = by_contact.get(source, [])
    phones = {r["phones"][0] for r in own if r["phones"]}
    phone = next(iter(phones)) if len(phones) == 1 else None
    rows = by_phone.get(phone, own) if phone else own
    if len({r["contact_id"] for r in rows}) > 100:
        return None, own, own, "too_many_candidates"
    all_phones = {p for r in rows for p in r["phones"]}
    parents = {person_name(r["legal_name"], all_phones) for r in rows} - {""}
    own_parents = {person_name(r["legal_name"], all_phones) for r in own} - {""}
    real_owners = {r["contact_id"] for r in rows if person_name(r["name"], all_phones)}
    conflict = "conflicting_parents" if len(parents) > 1 else None
    if not conflict and len({r["contact_id"] for r in rows}) > 1 and any(r["legal_type"] == 2 for r in rows):
        conflict = "shared_business_phone"
    if not conflict and len(real_owners) > 1 and not parents:
        conflict = "family_not_verified"
    if not conflict and not own_parents and any(person_name(r["name"], all_phones) for r in own) and len(real_owners - {source}):
        conflict = "family_not_verified"
    # Different primary numbers on one candidate family may not create a chain.
    for contact in {r["contact_id"] for r in rows}:
        numbers = {r["phones"][0] for r in by_contact[contact] if r["phones"]}
        if phone and numbers != {phone}:
            conflict = "phone_chain"
    return phone, rows, own if conflict else rows, conflict


def _facts(db, workspace, snapshot, customers):
    if not customers:
        return {}, defaultdict(list)
    args = {"w": workspace, "r": snapshot["run"], "conn": snapshot["connection_id"],
            "v": snapshot["version"], "ids": sorted(customers)}
    facts = {(r["customer_id"], r["branch_id"]): dict(r) for r in db.execute(text(
        "SELECT * FROM alfa_family_details WHERE workspace_id=:w AND connection_id=:conn "
        "AND settings_version=:v AND customer_id=ANY(CAST(:ids AS bigint[]))"), args).mappings()}
    members = defaultdict(list)
    for r in db.execute(text("SELECT m.* FROM alfa_memberships m JOIN alfa_groups g ON g.workspace_id=m.workspace_id "
        "AND g.run_id=m.run_id AND g.branch_id=m.branch_id AND g.external_id=m.group_id "
        "JOIN alfa_branches b ON b.workspace_id=m.workspace_id AND b.connection_id=:conn "
        "AND b.external_id=m.branch_id AND b.available WHERE m.workspace_id=:w AND m.run_id=:r "
        "AND m.customer_id=ANY(CAST(:ids AS bigint[]))"), args).mappings():
        members[(r["customer_id"], r["branch_id"])].append(dict(r))
    return facts, members


def _rank(rows, facts, members, snapshot):
    customer = rows[0]["external_id"]
    status = max((r["status"] for r in rows), key=lambda s: {"archive": 1, "lead": 2, "client": 3}[s])
    rank = {"archive": 1, "lead": 2, "client": 3}[status]
    evidence, cards = {}, []
    today = snapshot["today"]
    zone = ZoneInfo(snapshot.get("timezone", "UTC"))
    for row in rows:
        key = (customer, row["branch_id"])
        for member in members[key]:
            if row["status"] == "client" and (member["b_date"] is None or member["b_date"] <= today) and (member["e_date"] is None or member["e_date"] >= today):
                rank = max(rank, 4)
                evidence = {"group_id": str(member["group_id"]), "branch_id": str(row["branch_id"])}
        saved = facts.get(key)
        card = {"id": str(customer), "branch_id": str(row["branch_id"]), "lessons": []}
        if saved:
            card["lessons"] = saved["lessons"]
            for source, dest in (("lessons_refreshed_at", "lessons_refreshed_at"), ("lessons_full_refreshed_at", "lessons_full_refreshed_at")):
                at = saved.get(source)
                card[dest] = at.isoformat() if at else None
            for source, dest in (("lessons_refreshed_at", "lessons_recent_day"), ("lessons_full_refreshed_at", "lessons_full_day")):
                at = saved.get(source); card[dest] = at.astimezone(zone).date().isoformat() if at else None
        cards.append(card)
    if status == "client":
        upcoming = []
        for lesson in current_lessons(cards):
            if lesson["status"] != 1 or lesson["date"] < today.isoformat():
                continue
            key = (customer, int(lesson["branch_id"]))
            if not any(r["branch_id"] == key[1] and r["status"] == "client" for r in rows):
                continue
            now = datetime.now(zone)
            if lesson["date"] == now.date().isoformat() and lesson.get("time") and lesson["time"] < now.strftime("%H:%M"):
                continue
            saved = facts.get(key)
            if not saved or not saved.get("lessons_refreshed_at") or saved["lessons_refreshed_at"] < datetime.now(UTC) - timedelta(days=1):
                continue
            member = any(str(m["group_id"]) in {str(g) for g in lesson.get("group_ids", [])}
                and _member_at({"memberships": [{"customer_id": str(customer),
                    "begin": m["b_date"].isoformat() if m["b_date"] else None,
                    "end": m["e_date"].isoformat() if m["e_date"] else None}]}, str(customer), lesson["date"])
                for m in members[key])
            if lesson.get("customer_verified") or member:
                upcoming.append(lesson)
        if upcoming:
            lesson = min(upcoming, key=lambda x: (x["date"], x.get("time") or "", str(x["id"])))
            rank = 5
            evidence = {"lesson_id": str(lesson["id"]), "branch_id": str(lesson["branch_id"]),
                        "date": lesson["date"], "time": lesson.get("time")}
    branch = int(evidence.get("branch_id") or min(rows, key=lambda r: (r["status"] != status, r["branch_id"]))["branch_id"])
    chosen = next(r for r in rows if r["branch_id"] == branch)
    return rank, evidence, chosen


def choose(source, by_contact, by_phone, facts, members, snapshot, previous=None, verified_contacts=None):
    phone, cohort, eligible, ambiguous = _cohort(source, by_contact, by_phone)
    proven = set(map(str, verified_contacts or []))
    if (len(proven) > 1 and phone and source in proven
            and proven <= {row["contact_id"] for row in cohort}):
        # A shared inbound MAX row proves this particular peer; the phone alone
        # never bypasses the conflicting-parent guard for other contacts.
        eligible = [row for row in cohort if row["contact_id"] in proven]
        ambiguous = None
    else:
        proven = set()
    if not eligible:
        return None
    customers = defaultdict(list)
    for row in eligible:
        customers[row["external_id"]].append(row)
    ranked = [_rank(rows, facts, members, snapshot) for rows in customers.values()]
    best = min(ranked, key=lambda x: (-x[0], str(x[2]["external_id"]) != (previous or {}).get("customer"),
        x[1].get("date", "9999"), x[1].get("time") or "", x[2]["external_id"], x[2]["branch_id"]))
    rank, evidence, row = best
    real_phones = {p for r in eligible for p in r["phones"]}
    # Archive phone-only records stay available as related source cards, rather
    # than appearing as an extra child in the displayed active family.
    displayed = {r["contact_id"] for r in eligible if r["status"] != "archive" and person_name(r["name"], real_phones)}
    displayed = displayed or {row["contact_id"]}
    if proven:
        displayed = proven
    if (rank < 3 and not proven) or ambiguous:
        displayed = {source}
    # A global Customer ID with contradictory branch observations is never
    # allowed to redirect a different family automatically.
    names = {person_name(r["name"], real_phones) for r in customers[row["external_id"]]} - {""}
    birthdays = {r["dob"] for r in customers[row["external_id"]] if r["dob"]}
    if (len(names) > 1 or len(birthdays) > 1) and row["contact_id"] != source:
        own = defaultdict(list)
        for r in by_contact[source]:
            own[r["external_id"]].append(r)
        rank, evidence, row = min((_rank(values, facts, members, snapshot) for values in own.values()),
                                 key=lambda x: (-x[0], x[2]["external_id"], x[2]["branch_id"]))
        displayed, ambiguous = {source}, "conflicting_customer"
    observations = sorted((r for r in eligible if r["external_id"] == row["external_id"]),
                          key=lambda r: (r["branch_id"] != row["branch_id"], r["branch_id"]))
    label = next((r["legal_name"] for r in observations if person_name(r["legal_name"], real_phones)), row["name"])
    # Most chats retain their original contact. Their indexed contact guard does
    # not scan a whole phone cohort on every list count or unchanged family read.
    phone = phone if any(contact != source for contact in displayed) or row["contact_id"] != source else None
    guarded = cohort if phone else by_contact[source]
    guard = hashlib.md5("|".join(r["guard_piece"] for r in sorted(guarded, key=lambda r: (r["external_id"], r["branch_id"]))).encode()).hexdigest()
    result = {"policy": VERSION, "connection": str(snapshot["connection_id"]), "run": str(snapshot["run"]),
        "version": snapshot["version"], "source": source, "contact": row["contact_id"], "contacts": sorted(displayed),
        "customer": str(row["external_id"]), "branch": str(row["branch_id"]),
        "student": str(row["student_id"]) if row["student_id"] else None,
        "name": label, "status": {5: "client", 4: "client", 3: "client", 2: "lead", 1: "archive"}[rank],
        "rank": rank, "reason": REASONS[rank], "evidence": evidence, "ambiguous": ambiguous,
        "phone": phone, "guard": guard,
        "hidden_customers": sorted(str(key) for key, values in customers.items() if rank >= 3 and key != row["external_id"]
            and all(not person_name(r["name"], real_phones) for r in values)),
        "related_cards": [{"customer": str(r["external_id"]), "branch": str(r["branch_id"]), "status": r["status"]}
                          for r in cohort if r["contact_id"] not in displayed or
                            (rank >= 3 and r["external_id"] != row["external_id"] and not person_name(r["name"], real_phones))]}
    result["signature"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def refresh(db, workspace, snapshot, sources):
    if not enabled(snapshot) or not sources:
        return 0
    db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
    valid = db.execute(text("SELECT 1 FROM alfa_sync_control sc JOIN alfa_settings s ON s.workspace_id=sc.workspace_id "
        "AND s.connection_id=sc.connection_id WHERE sc.workspace_id=:w AND sc.connection_id=:conn "
        "AND sc.published_id=:r AND s.version=:v AND s.secret_ciphertext IS NOT NULL"),
        {"w": workspace, "conn": snapshot["connection_id"], "r": snapshot["run"], "v": snapshot["version"]}).scalar_one_or_none()
    if not valid:
        return 0
    by_contact, by_phone = _catalog(db, workspace, snapshot, sources)
    cohorts = [_cohort(str(source), by_contact, by_phone)[1] for source in sources]
    facts, members = _facts(db, workspace, snapshot, {r["external_id"] for cohort in cohorts for r in cohort})
    count, at = 0, datetime.now(UTC).isoformat()
    for source in map(str, sources):
        key = f"{snapshot['connection_id']}:{source}"
        previous = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND event_key=:key"),
            {"w": workspace, "t": TOPIC, "key": key}).scalar_one_or_none()
        from center_messenger.alfa_shared_parent import members as proven_members
        own = by_contact.get(source, [])
        phones = {r["phones"][0] for r in own if r["phones"]}
        verified = (proven_members(db, workspace, snapshot["connection_id"], snapshot,
                                   UUID(source), next(iter(phones))) if len(phones) == 1 else [])
        result = choose(source, by_contact, by_phone, facts, members, snapshot, previous,
                        verified_contacts=verified)
        if not result:
            continue
        same = previous and previous.get("signature") == result["signature"]
        result.update(checked_at=at, selected_at=previous.get("selected_at", at) if same else at)
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
            "DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
            {"w": workspace, "id": uuid4(), "t": TOPIC, "key": key, "p": json.dumps(result, ensure_ascii=False)})
        if not same:
            db.execute(text("UPDATE conversations c SET revision=revision+1 WHERE workspace_id=:w AND contact_id=CAST(:source AS uuid) "
                "AND NOT crm_link_manual AND EXISTS(SELECT 1 FROM alfa_max_settings m WHERE m.workspace_id=c.workspace_id "
                "AND m.connection_id=c.connection_id AND m.crm_connection_id=:conn)"),
                {"w": workspace, "source": source, "conn": snapshot["connection_id"]})
            count += 1
    return count


def repair(db, workspace, snapshot, *, limit=100):
    if not enabled(snapshot):
        return 0
    state = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND event_key=:key"),
        {"w": workspace, "t": SCAN, "key": str(snapshot["connection_id"])}).scalar_one_or_none() or {}
    same = state.get("run") == str(snapshot["run"]) and state.get("version") == snapshot["version"]
    if same and state.get("next_at") and datetime.fromisoformat(state["next_at"]) > datetime.now(UTC):
        return 0
    after = state.get("after") if same else None
    sources = list(db.execute(text("SELECT DISTINCT c.contact_id FROM conversations c JOIN alfa_max_settings m ON "
        "m.workspace_id=c.workspace_id AND m.connection_id=c.connection_id WHERE c.workspace_id=:w "
        "AND m.crm_connection_id=:conn AND c.contact_id IS NOT NULL AND NOT c.crm_link_manual "
        "AND (CAST(:after AS uuid) IS NULL OR c.contact_id>CAST(:after AS uuid)) ORDER BY c.contact_id LIMIT :limit"),
        {"w": workspace, "conn": snapshot["connection_id"], "after": after, "limit": limit}).scalars())
    count = refresh(db, workspace, snapshot, sources)
    done = len(sources) < limit
    payload = {"run": str(snapshot["run"]), "version": snapshot["version"], "after": str(sources[-1]) if sources and not done else None,
               "next_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat() if done else None}
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') "
        "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        {"w": workspace, "id": uuid4(), "t": SCAN, "key": str(snapshot["connection_id"]), "p": json.dumps(payload)})
    return count
