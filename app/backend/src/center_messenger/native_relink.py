"""Reconcile changed CRM associations of the exact same incoming MAX row.

The namespace, connection, source branch, record ID and timestamp are evidence.
Matching primary phones are only a current-directory consistency check. Bodies,
timestamps and CRM Customer identities are not rewritten to manufacture a match.
"""
from uuid import UUID
from sqlalchemy import text

from center_messenger import alfa_shared_parent
from center_messenger.alfa_repair import resolve_customer
from center_messenger.chat_store import changed
from center_messenger.history_receipts import customer_ids
from center_messenger.message_order import visible_message


def history_compatible(rows, previous, item):
    """One reassigned row is not proof for unrelated old incoming messages."""
    peer = item.get('max_user_id') or previous['payload'].get('max_user_id')
    peers = {r['peer'] for r in rows if r['peer']}
    if len(peers) > 1 or (peer and peers - {peer}):
        return False
    for row in rows:
        if row['conversation_id'] != previous['conversation_id']:
            continue
        exact = str(row['record']) == str(item['record_id']) and row['occurred_at'] == item['occurred_at']
        if not exact and (not peer or row['peer'] != peer):
            return False
    return True


def history_proven(db, workspace, channel, previous, item, contacts):
    rows = db.execute(text("SELECT m.payload->>'max_user_id' AS peer,m.payload->>'crm_record_id' AS record,"
        "m.occurred_at,m.conversation_id FROM messages m JOIN conversations c "
        "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id WHERE c.workspace_id=:w "
        "AND c.connection_id=:channel AND (c.contact_id=ANY(CAST(:contacts AS uuid[])) OR c.id=:previous) "
        "AND m.direction='in' AND m.actor_type='contact' AND " + visible_message('m') +
        " AND coalesce(m.payload->>'crm_mirror','false')<>'true'"),
        {'w': workspace, 'channel': channel, 'contacts': contacts, 'previous': previous['conversation_id']}).mappings().all()
    return history_compatible(rows, previous, item)


def previous_rows(db, workspace, channel, branch, records):
    keys = [f"in:{branch}:{r['record_id']}" for r in records
            if r['direction'] == 'in' and r['actor_type'] == 'contact']
    if not keys:
        return {}
    rows = db.execute(text("SELECT m.id,m.payload,m.occurred_at,m.conversation_id,"
        "c.contact_id,c.crm_link_manual FROM source_records s JOIN messages m "
        "ON m.workspace_id=s.workspace_id AND m.id=s.message_id JOIN conversations c "
        "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
        "WHERE s.workspace_id=:w AND s.connection_id=:channel AND s.namespace='alfa_max' "
        "AND s.external_record_id=ANY(CAST(:keys AS text[])) AND c.connection_id=:channel "
        "AND m.direction='in' AND m.actor_type='contact'"),
        {'w': workspace, 'channel': channel, 'keys': keys}).mappings()
    return {str(row['payload'].get('crm_record_id')): dict(row) for row in rows
            if str(row['payload'].get('branch_id')) == str(branch)}


def association_pair(previous, item, branch):
    if (not previous or not item.get('source_metadata_refresh')
            or item['direction'] != 'in' or item['actor_type'] != 'contact'):
        return None
    old = previous['payload']
    if str(old.get('branch_id')) != str(branch) or str(old.get('crm_record_id')) != str(item['record_id']):
        return None
    selected = int(old['customer_id']) if old.get('customer_id') else None
    old_ids = {int(value) for value in (old.get('crm_customer_ids') or
               ([selected] if selected else []))}
    new_ids = customer_ids(item)
    if not selected or len(old_ids) != 1 or len(new_ids) != 1 or selected == new_ids[0]:
        return None
    # The chosen card is the stale address, even if a previous partial repair
    # already changed the source's customer list. Unrelated IDs and old bulk
    # associations are ambiguous and must not manufacture a family alias.
    observed_ids = {int(pair[1]) for pair in old.get('crm_observed_customers') or []}
    if not old_ids <= {selected, new_ids[0]} or (observed_ids and (
            len(observed_ids) != 1 or not observed_ids <= {selected, new_ids[0]})):
        return None
    return selected, new_ids[0]


def relink_pair(previous, item, branch):
    pair = association_pair(previous, item, branch)
    if not pair or previous['occurred_at'] != item['occurred_at']:
        return None
    old = previous['payload']
    if old.get('max_user_id') and item.get('max_user_id') and old['max_user_id'] != item['max_user_id']:
        return None
    return pair


def prove(db, workspace, channel, snapshot, branch, item, previous, resolved, directory):
    pair = association_pair(previous, item, branch)
    if not pair:
        return None
    old_id, new_id = pair
    # Block stale address evidence even if the stronger family proof fails.
    rows = db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('crm_recipient_stale',true),"
        "version=version+1 WHERE workspace_id=:w AND conversation_id=:conv AND direction='in' "
        "AND payload->>'crm_record_id'=:record AND payload->>'customer_id'=:old AND occurred_at=:at "
        "AND coalesce(payload->>'crm_recipient_stale','false')<>'true' RETURNING id"),
        {'w': workspace, 'conv': previous['conversation_id'], 'record': str(item['record_id']),
         'old': str(old_id), 'at': previous['occurred_at']}).scalars()
    for identifier in rows:
        changed(db, workspace, previous['conversation_id'], identifier)
    if previous['crm_link_manual'] or not relink_pair(previous, item, branch):
        return None
    old = resolve_customer({'customer_id': old_id,
        'branch_id': int(previous['payload']['customer_branch_id'])}, directory)
    current = [r for r in resolved if r.get('customer_id') == new_id]
    if len(current) != 1:
        return None
    new = current[0]
    if (not snapshot or snapshot.get('version') is None or snapshot.get('connection_id') is None
            or not old.get('global_id_verified') or not new.get('global_id_verified')
            or not old.get('primary_phone') or old['primary_phone'] != new.get('primary_phone')
            or not old.get('contact_id') or not new.get('contact_id')):
        return None
    family = alfa_shared_parent.members(db, workspace, channel, snapshot,
                                       old['contact_id'], old['primary_phone'])
    if previous['contact_id'] not in family:
        return None
    if not history_proven(db, workspace, channel, previous, item, list(set(family) | {new['contact_id']})):
        return None
    return alfa_shared_parent.prove(db, workspace, channel, snapshot, [old, new],
        source=f"relink:in:{branch}:{item['record_id']}",
        reply_card={'customer': new_id, 'branch': new['branch_id'], 'from': [old_id],
                    'crm_connection': str(snapshot['connection_id']), 'settings_version': snapshot['version']})


def reply_target(db, workspace, channel, snapshot, target, directory):
    """Apply only a persisted same-source relink, fenced by the current catalog."""
    if not snapshot:
        return target
    original = resolve_customer({'customer_id': int(target['customer_id']),
        'branch_id': int(target['customer_branch_id'])}, directory)
    if not original.get('global_id_verified') or not original.get('contact_id'):
        return target
    payload = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND event_key=:key AND state='published'"), {'w': workspace, 't': alfa_shared_parent.TOPIC,
        'key': alfa_shared_parent._key(channel, original['contact_id'])}).scalar_one_or_none()
    card = (payload or {}).get('reply_card') or {}
    if (not isinstance(card.get('from'), list) or original['customer_id'] not in card['from']
            or not isinstance(card.get('customer'), int)
            or isinstance(card['customer'], bool) or not isinstance(card.get('branch'), int)
            or isinstance(card['branch'], bool) or not str(card.get('source', '')).startswith('relink:in:')
            or card.get('crm_connection') != str(snapshot['connection_id'])
            or card.get('settings_version') != snapshot['version']):
        return target
    try:
        family = [UUID(value) for value in payload.get('contacts', [])]
    except (ValueError, TypeError):
        return target
    if (original['contact_id'] not in family or not original.get('primary_phone')
            or payload.get('phone') != original['primary_phone']
            or not alfa_shared_parent._current(db, workspace, snapshot, family, original['primary_phone'])):
        return target
    from center_messenger.alfa_repair import customer_directory
    current = resolve_customer({'customer_id': card['customer'], 'branch_id': card['branch']},
        customer_directory(db, workspace, snapshot, [card['customer']]))
    if (not current.get('global_id_verified') or current.get('contact_id') not in family
            or current.get('primary_phone') != original.get('primary_phone')
            or not original.get('primary_phone')):
        return target
    return dict(target, customer_id=current['customer_id'], customer_branch_id=current['branch_id'],
                crm_family_contacts=[str(contact) for contact in family], crm_recipient_stale=False)


def mirror_customers(db, workspace, channel, snapshot, conversation):
    """Verify the old/new customer witness before deduplicating its source copies."""
    if not snapshot or snapshot.get('version') is None:
        return set()
    row = db.execute(text("SELECT e.payload,c.contact_id FROM conversations c JOIN outbox_events e "
        "ON e.workspace_id=c.workspace_id AND e.topic=:topic "
        "AND e.event_key=concat(CAST(:channel AS text),':',c.contact_id) AND e.state='published' "
        "WHERE c.workspace_id=:w AND c.id=:conv AND c.connection_id=:channel"),
        {'w': workspace, 'channel': channel, 'conv': conversation, 'topic': alfa_shared_parent.TOPIC}).mappings().one_or_none()
    if not row:
        return set()
    payload, card = row['payload'], row['payload'].get('reply_card') or {}
    if (card.get('crm_connection') != str(snapshot['connection_id'])
            or card.get('settings_version') != snapshot['version']
            or not str(card.get('source', '')).startswith('relink:in:')
            or not isinstance(card.get('branch'), int) or isinstance(card['branch'], bool)
            or not isinstance(card.get('customer'), int) or isinstance(card['customer'], bool)
            or card['branch'] <= 0 or card['customer'] <= 0 or not isinstance(card.get('from'), list)
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in card['from'])):
        return set()
    try:
        family = [UUID(value) for value in payload.get('contacts', [])]
        ids = {card['customer']} | set(card['from'])
    except (ValueError, TypeError, KeyError):
        return set()
    phone = payload.get('phone')
    if (row['contact_id'] not in family or not phone
            or not alfa_shared_parent._current(db, workspace, snapshot, family, phone)):
        return set()
    from center_messenger.alfa_repair import customer_directory
    directory = customer_directory(db, workspace, snapshot, ids)
    for customer in ids:
        current = resolve_customer({'customer_id': customer, 'branch_id': card['branch']}, directory)
        if (not current.get('global_id_verified') or current.get('contact_id') not in family
                or current.get('primary_phone') != phone):
            return set()
    return ids


def mirror_key(payload, verified_customers):
    customers = tuple(payload.get('crm_customer_ids') or [payload.get('customer_id')])
    if (verified_customers and customers and None not in customers and payload.get('crm_record_id') is not None
            and {int(value) for value in customers} <= verified_customers):
        # Matching text/time cannot collapse different source record IDs.
        return ('relinked_max_source', str(payload['crm_record_id']))
    return customers
