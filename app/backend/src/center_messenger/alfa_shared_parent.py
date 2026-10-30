"""Verified aliases for one incoming MAX sender linked to several CRM children.

The shared phone is a consistency check, never evidence on its own. Only one
incoming CRM journal row explicitly naming all the children can establish an
alias. A verified re-association of that exact incoming source row may also
establish one. A sent bulk notification must never establish one.
"""

import json
from uuid import UUID, uuid4

from sqlalchemy import text

TOPIC = "alfa_max.shared_parent.v1"
REPAIR_VERSION = 1


def _key(channel, contact):
    return f"{channel}:{contact}"


def _current(db, workspace, snapshot, contacts, phone):
    """Fence a saved alias against the published CRM snapshot and phone changes."""
    if not snapshot or not phone or not contacts:
        return False
    rows = db.execute(text(
        "SELECT a.contact_id,array_agg(DISTINCT a.phones->>0) AS phones, "
        "bool_and(a.legal_type=1) AS personal "
        "FROM alfa_customers a JOIN alfa_branches b ON b.workspace_id=a.workspace_id "
        "AND b.connection_id=:crm AND b.external_id=a.branch_id AND b.available "
        "WHERE a.workspace_id=:w AND a.run_id=:run AND a.contact_id=ANY(CAST(:contacts AS uuid[])) "
        "GROUP BY a.contact_id"
    ), {"w": workspace, "run": snapshot["run"], "crm": snapshot["connection_id"],
        "contacts": list(contacts)}).all()
    return len(rows) == len(contacts) and all(row[1] == [phone] and row[2] for row in rows)


def members(db, workspace, channel, snapshot, contact, phone):
    if not contact or not phone:
        return [contact] if contact else []
    payload = db.execute(text(
        "SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:topic "
        "AND event_key=:key AND state='published'"
    ), {"w": workspace, "topic": TOPIC, "key": _key(channel, contact)}).scalar_one_or_none()
    if not payload or payload.get("phone") != phone:
        return [contact]
    contacts = [UUID(value) for value in payload.get("contacts", [])]
    if contact not in contacts or not _current(db, workspace, snapshot, contacts, phone):
        return [contact]
    return contacts


def canonical_alias(db, workspace, channel, snapshot, contact):
    """Resolve an emptied child dialog after a proven family consolidation."""
    if not snapshot or not contact:
        return None
    phones = db.execute(text(
        "SELECT array_agg(DISTINCT a.phones->>0) FROM alfa_customers a "
        "JOIN alfa_branches b ON b.workspace_id=a.workspace_id AND b.connection_id=:crm "
        "AND b.external_id=a.branch_id AND b.available WHERE a.workspace_id=:w "
        "AND a.run_id=:run AND a.contact_id=:contact"
    ), {"w": workspace, "run": snapshot["run"], "crm": snapshot["connection_id"],
        "contact": contact}).scalar_one()
    if not phones or len(phones) != 1 or not phones[0]:
        from center_messenger.native_archive_relink import canonical_alias as archived_alias
        return archived_alias(db, workspace, channel, snapshot, contact)
    family = members(db, workspace, channel, snapshot, contact, phones[0])
    if len(family) < 2:
        return None
    return db.execute(text(
        "SELECT id FROM conversations WHERE workspace_id=:w AND connection_id=:channel "
        "AND external_chat_id=:external AND external_topic_id='' AND NOT crm_link_manual"
    ), {"w": workspace, "channel": channel,
        "external": f"alfa-contact:{family[0]}"}).scalar_one_or_none()


def prove(db, workspace, channel, snapshot, customers, *, source, reply_card=None):
    """Save a peer only when the same incoming row names verified CRM cards."""
    contacts = {c["contact_id"] for c in customers if c and c.get("contact_id")}
    phones = {c.get("primary_phone") for c in customers if c and c.get("contact_id")}
    if (not contacts or (len(contacts) < 2 and reply_card is None) or len(phones) != 1 or None in phones
            or any(not c.get("global_id_verified") for c in customers if c and c.get("contact_id"))):
        return None
    phone = next(iter(phones))
    if not _current(db, workspace, snapshot, contacts, phone):
        return None
    current = set(contacts)
    for contact in contacts:
        current.update(members(db, workspace, channel, snapshot, contact, phone))
    if len(current) > 20 or not _current(db, workspace, snapshot, current, phone):
        return None
    # A manually linked chat might intentionally belong to another recipient.
    manual = db.execute(text(
        "SELECT 1 FROM conversations WHERE workspace_id=:w AND connection_id=:channel "
        "AND contact_id=ANY(CAST(:contacts AS uuid[])) AND crm_link_manual LIMIT 1"
    ), {"w": workspace, "channel": channel, "contacts": list(current)}).scalar_one_or_none()
    if manual:
        return None
    canonical = min(current)
    expected = [str(c) for c in sorted(current)]
    keys = [_key(channel, contact) for contact in current]
    stored = dict(db.execute(text(
        "SELECT event_key,payload FROM outbox_events WHERE workspace_id=:w AND topic=:topic "
        "AND event_key=ANY(CAST(:keys AS text[])) AND state='published'"
    ), {"w": workspace, "topic": TOPIC, "keys": keys}).all())
    old_cards = [value['reply_card'] for value in stored.values() if value.get('reply_card')]
    if reply_card is not None:
        if (not source.startswith('relink:in:') or not isinstance(reply_card.get('customer'), int)
                or isinstance(reply_card['customer'], bool) or reply_card['customer'] <= 0
                or not isinstance(reply_card.get('branch'), int) or isinstance(reply_card['branch'], bool)
                or reply_card['branch'] <= 0 or not reply_card.get('from')
                or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                       for value in reply_card['from'])):
            return None
        origins = set(reply_card['from'])
        for card in old_cards:
            if (card.get('crm_connection') != reply_card.get('crm_connection')
                    or card.get('settings_version') != reply_card.get('settings_version')):
                continue
            origins.update(card.get('from', []))
            origins.add(card['customer'])
        origins.discard(reply_card['customer'])
        reply_card = dict(reply_card, **{'from': sorted(origins), 'source': source})
    elif old_cards and len({json.dumps(card, sort_keys=True) for card in old_cards}) == 1:
        reply_card = old_cards[0]
    if len(stored) == len(current) and all(
        value.get("phone") == phone and value.get("contacts") == expected
        and value.get("canonical") == str(canonical) and value.get('reply_card') == reply_card
        for value in stored.values()
    ):
        return sorted(current) if reply_card is not None else None
    record = {"policy": REPAIR_VERSION, "phone": phone, "contacts": expected,
              "canonical": str(canonical), "source": source}
    if reply_card is not None:
        record['reply_card'] = reply_card
    payload = json.dumps(record, ensure_ascii=False)
    for contact in current:
        db.execute(text(
            "INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
            "VALUES(:w,:id,:topic,:key,CAST(:payload AS jsonb),'published') "
            "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET "
            "payload=EXCLUDED.payload,state='published'"
        ), {"w": workspace, "id": uuid4(), "topic": TOPIC,
            "key": _key(channel, contact), "payload": payload})
    return sorted(current)


def repair_batch(service, db, workspace, channel, snapshot, cursor, *, limit=20):
    """Discover proven co-recipients in saved history without rereading CRM."""
    previous = cursor.get("shared_parent_repair") or {}
    if previous.get("version") == REPAIR_VERSION and previous.get("complete"):
        return False
    after = previous.get("after", "") if previous.get("version") == REPAIR_VERSION else ""
    rows = db.execute(text(
        "SELECT concat(m.payload->>'branch_id',':',m.payload->>'crm_record_id') AS source, "
        "array_agg(DISTINCT c.contact_id) AS contacts "
        "FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
        "WHERE m.workspace_id=:w AND c.connection_id=:channel AND m.direction='in' "
        "AND m.actor_type='contact' AND c.contact_id IS NOT NULL "
        "AND m.payload->>'source' IN ('alfa_max','center_messenger_alfa_max') "
        "AND m.payload->>'branch_id' IS NOT NULL AND m.payload->>'crm_record_id' IS NOT NULL "
        "AND concat(m.payload->>'branch_id',':',m.payload->>'crm_record_id')>:after "
        "GROUP BY source HAVING count(DISTINCT c.contact_id)>1 ORDER BY source LIMIT :limit"
    ), {"w": workspace, "channel": channel, "after": after, "limit": limit + 1}).mappings().all()
    for row in rows[:limit]:
        contacts = row["contacts"]
        cards = db.execute(text(
            "SELECT DISTINCT a.contact_id,a.phones->>0 AS primary_phone FROM alfa_customers a "
            "JOIN alfa_branches b ON b.workspace_id=a.workspace_id AND b.connection_id=:crm "
            "AND b.external_id=a.branch_id AND b.available WHERE a.workspace_id=:w AND a.run_id=:run "
            "AND a.contact_id=ANY(CAST(:contacts AS uuid[]))"
        ), {"w": workspace, "run": snapshot["run"], "crm": snapshot["connection_id"],
            "contacts": contacts}).mappings().all()
        proof = [{"contact_id": c["contact_id"], "primary_phone": c["primary_phone"],
                  "global_id_verified": True} for c in cards]
        if {c["contact_id"] for c in proof} != set(contacts):
            continue
        changed = prove(db, workspace, channel, snapshot, proof, source=row["source"])
        family = members(db, workspace, channel, snapshot, contacts[0], proof[0]["primary_phone"]) if proof else []
        if family and set(contacts) <= set(family) and len(family) > 1:
            canonical = family[0]
            external = f"alfa-contact:{canonical}"
            from uuid import uuid5
            db.execute(text(
                "INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id) "
                "VALUES(:w,:id,:channel,:contact,:external) ON CONFLICT "
                "(workspace_id,connection_id,external_chat_id,external_topic_id) DO NOTHING"
            ), {"w": workspace, "id": uuid5(UUID(str(channel)), external), "channel": channel,
                "contact": canonical, "external": external})
            identifier = db.execute(text(
                "SELECT id FROM conversations WHERE workspace_id=:w AND connection_id=:channel "
                "AND external_chat_id=:external AND external_topic_id=''"
            ), {"w": workspace, "channel": channel, "external": external}).scalar_one()
            service._merge_contact_conversations(db, workspace, channel, canonical, identifier,
                                                 family_contacts=family)
            from center_messenger.alfa_repair import reconcile_messages
            reconcile_messages(service, db, workspace, channel, identifier)
            if changed and snapshot.get("version") is not None:
                from center_messenger import card_preference
                card_preference.refresh(db, workspace, snapshot, [canonical])
    cursor["shared_parent_repair"] = {"version": REPAIR_VERSION,
        "after": rows[min(len(rows), limit) - 1]["source"] if rows else after,
        "complete": len(rows) <= limit}
    return True
