"""Directional recovery of an archived, emptied CRM card from exact MAX sources.

This is not a same-phone family alias. Only a completely proven old dialog is
moved to the existing active card; original Customer IDs and message bodies
remain unchanged. Failed commands are preserved, never requeued.
"""
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from center_messenger import native_relink
from center_messenger.alfa_repair import customer_directory, resolve_customer
from center_messenger.chat_store import changed, refresh_unread
from center_messenger.message_order import visible_message

TOPIC = 'alfa_max.archived_source_redirect.v1'


def phone_guard(phone):
    return hashlib.sha256(phone.encode()).hexdigest() if phone else None


def allowed(old, new):
    return bool(old.get('global_id_verified') and new.get('global_id_verified')
        and old.get('contact_id') and new.get('contact_id') and old['contact_id'] != new['contact_id']
        and old.get('status') == 'archive' and not old.get('primary_phone') and old.get('empty_archive')
        and new.get('status') == 'client' and new.get('primary_phone')
        and old.get('available') is True and new.get('available') is True)


def current_card(customer, branch, directory):
    value = resolve_customer({'customer_id': customer, 'branch_id': branch}, directory)
    rows = [r for r in directory.get(customer, []) if r.get('available') is True]
    value['available'] = any(r['branch_id'] == value['branch_id'] for r in rows)
    value['empty_archive'] = bool(rows) and all(r['status'] == 'archive' and not r.get('primary_phone') for r in rows)
    return value


def observation(payload, customer):
    value = payload.get('crm_observed_customers')
    return (isinstance(value, list) and len(value) == 1 and isinstance(value[0], list)
            and len(value[0]) == 2 and str(value[0][0]) == str(payload.get('branch_id'))
            and str(value[0][1]) == str(customer)
            and str(payload.get('customer_id')) == str(customer)
            and not payload.get('crm_recipient_stale'))


def source_identity(payload):
    return all(not isinstance(payload.get(key), bool) and
        re.fullmatch('[1-9][0-9]{0,18}', str(payload.get(key))) is not None
        and int(payload[key]) <= 2**63-1
        for key in ('branch_id', 'crm_record_id', 'customer_id', 'customer_branch_id'))


def same_source(old, new):
    a, b = old['payload'], new['payload']
    return bool(a.get('crm_record_id') is not None and a.get('branch_id') is not None
        and str(a['crm_record_id']) == str(b.get('crm_record_id'))
        and str(a['branch_id']) == str(b.get('branch_id')) and old['occurred_at'] == new['occurred_at']
        and (not a.get('max_user_id') or not b.get('max_user_id') or a['max_user_id'] == b['max_user_id']))


def counterpart(db, workspace, channel, row):
    if not source_identity(row['payload']):
        return None
    matches = list(db.execute(text("SELECT m.id,m.occurred_at,m.payload,m.conversation_id,c.contact_id,c.crm_link_manual "
        "FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
        "WHERE m.workspace_id=:w AND c.connection_id=:channel AND m.direction='in' AND m.actor_type='contact' "
        "AND m.id<>:id AND m.payload->>'branch_id'=:branch AND m.payload->>'crm_record_id'=:record "
        "AND m.occurred_at=:at AND m.payload->>'customer_id'<>:old "
        "AND coalesce(m.payload->>'crm_recipient_stale','false')<>'true' "
        "AND coalesce(m.payload->>'deleted','false')<>'true' "
        "AND EXISTS(SELECT 1 FROM source_records s WHERE s.workspace_id=m.workspace_id "
        "AND s.connection_id=:channel AND s.namespace='alfa_max' AND s.message_id=m.id "
        "AND s.external_record_id IN (:source,concat(:source,'|recipient|',m.payload->>'customer_branch_id',':',m.payload->>'customer_id'))) "
        "ORDER BY m.id LIMIT 3"),
        {'w': workspace, 'channel': channel, 'id': row['id'], 'branch': str(row['payload']['branch_id']),
         'record': str(row['payload']['crm_record_id']), 'at': row['occurred_at'],
         'old': str(row['payload']['customer_id']),
         'source': f'in:{row["payload"]["branch_id"]}:{row["payload"]["crm_record_id"]}'}).mappings())
    # Never pick arbitrarily between multiple projections or recipients.
    if len(matches) != 1 or not same_source(row, matches[0]):
        return None
    new = matches[0]
    return new if source_identity(new['payload']) and observation(new['payload'], new['payload'].get('customer_id')) else None


def recover(db, service, workspace, channel, snapshot, conversation):
    conversations = list(db.execute(text("SELECT id,contact_id,crm_link_manual FROM conversations "
        "WHERE workspace_id=:w AND connection_id=:channel AND id=:c FOR UPDATE"),
        {'w': workspace, 'channel': channel, 'c': conversation}).mappings())
    if not conversations or conversations[0]['crm_link_manual']:
        return False
    previous = db.execute(text("SELECT m.id,m.occurred_at,m.payload,m.conversation_id,c.contact_id,c.crm_link_manual "
        "FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
        "WHERE m.workspace_id=:w AND m.conversation_id=:c AND m.direction='in' AND m.actor_type='contact' "
        "AND " + visible_message('m') + " AND coalesce(m.payload->>'crm_mirror','false')<>'true' "
        "ORDER BY m.occurred_at DESC,m.id DESC LIMIT 1"), {'w': workspace, 'c': conversation}).mappings().one_or_none()
    if not previous or not source_identity(previous['payload']) or not previous['payload'].get('crm_recipient_stale'):
        return False
    current = counterpart(db, workspace, channel, previous)
    if not current or current['crm_link_manual'] or current['conversation_id'] == conversation:
        return False
    a, b = previous['payload'], current['payload']
    directory = customer_directory(db, workspace, snapshot, [int(a['customer_id']), int(b['customer_id'])])
    old = current_card(int(a['customer_id']), int(a['customer_branch_id']), directory)
    new = current_card(int(b['customer_id']), int(b['customer_branch_id']), directory)
    if (not allowed(old, new) or old['contact_id'] != previous['contact_id']
            or new['contact_id'] != current['contact_id']):
        return False
    item = {'max_user_id': b.get('max_user_id'), 'record_id': b['crm_record_id'], 'occurred_at': current['occurred_at']}
    if not native_relink.history_proven(db, workspace, channel, previous, item, [old['contact_id'], new['contact_id']]):
        return False
    canonical = db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND connection_id=:channel "
        "AND id=:id AND contact_id=:contact AND external_chat_id=:external AND NOT crm_link_manual FOR UPDATE"),
        {'w': workspace, 'channel': channel, 'id': current['conversation_id'], 'contact': new['contact_id'],
         'external': 'alfa-contact:' + str(new['contact_id'])}).scalar_one_or_none()
    if not canonical:
        return False
    busy = db.execute(text("SELECT 1 FROM send_commands WHERE workspace_id=:w AND conversation_id=ANY(CAST(:ids AS uuid[])) "
        "AND state IN ('queued','sending','unknown') LIMIT 1"), {'w': workspace, 'ids': [conversation, canonical]}).scalar_one_or_none()
    if busy:
        return False
    # Retained hidden branch mirrors must also have exact replacement sources;
    # otherwise normal reconciliation could restore an unrelated old mirror.
    copies = list(db.execute(text("SELECT m.id,m.occurred_at,m.payload FROM messages m WHERE m.workspace_id=:w "
        "AND m.conversation_id=:c AND m.direction='in' AND m.actor_type='contact' "
        "AND coalesce(m.payload->>'deleted','false')<>'true' "
        "AND (m.payload->>'superseded_by' IS NULL OR m.payload->>'crm_mirror'='true') "
        "ORDER BY m.id LIMIT 21"), {'w': workspace, 'c': conversation}).mappings())
    if len(copies) > 20:
        return False
    replacements = []
    for row in copies:
        replacement = counterpart(db, workspace, channel, row)
        if (not replacement or replacement['conversation_id'] != canonical
                or replacement['crm_link_manual'] or replacement['contact_id'] != new['contact_id']
                or str(replacement['payload'].get('customer_id')) != str(new['customer_id'])):
            return False
        replacements.append((row['id'], replacement['id']))
    if not replacements:
        return False
    proof = {'version': 1, 'crm_connection': str(snapshot['connection_id']), 'settings_version': snapshot['version'],
        'old_customer': old['customer_id'], 'old_contact': str(old['contact_id']),
        'new_customer': new['customer_id'], 'new_branch': new['branch_id'], 'new_contact': str(new['contact_id']),
        'phone_guard': phone_guard(new['primary_phone']), 'old_conversation': str(conversation),
        'canonical': str(canonical), 'old_message': str(previous['id']), 'new_message': str(current['id']),
        'branch': str(a['branch_id']), 'record': str(a['crm_record_id']), 'at': previous['occurred_at'].isoformat()}
    for old_id, new_id in replacements:
        service._supersede(db, workspace, old_id, new_id)
        changed(db, workspace, conversation, old_id)
    service._merge_contact_conversations(db, workspace, channel, new['contact_id'], canonical,
        family_contacts=[old['contact_id'], new['contact_id']], only_conversations=[conversation])
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:topic,:key,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
        "DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        {'w': workspace, 'id': uuid4(), 'topic': TOPIC, 'key': f'{channel}:{old["contact_id"]}', 'p': json.dumps(proof)})
    from center_messenger.alfa_repair import reconcile_messages
    reconcile_messages(service, db, workspace, channel, canonical)
    refresh_unread(db, workspace, conversation)
    return True


def canonical_alias(db, workspace, channel, snapshot, contact):
    if not snapshot or snapshot.get('version') is None:
        return None
    proof = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND event_key=:key AND state='published'"),
        {'w': workspace, 't': TOPIC, 'key': f'{channel}:{contact}'}).scalar_one_or_none()
    if (not isinstance(proof, dict) or proof.get('version') != 1 or proof.get('old_contact') != str(contact)
            or proof.get('crm_connection') != str(snapshot['connection_id'])
            or proof.get('settings_version') != snapshot['version']):
        return None
    try:
        old_id, new_id = proof['old_customer'], proof['new_customer']
        if any(not isinstance(i, int) or isinstance(i, bool) or i <= 0 for i in (old_id, new_id, proof['new_branch'])):
            return None
        old_conversation, canonical = UUID(proof['old_conversation']), UUID(proof['canonical'])
        old_message, new_message = UUID(proof['old_message']), UUID(proof['new_message'])
        branch = int(proof['branch'])
        at = datetime.fromisoformat(proof['at'])
        record = str(proof['record'])
        if (old_id == new_id or old_conversation == canonical or old_message == new_message
                or branch <= 0 or at.tzinfo is None or not re.fullmatch('[0-9]{1,19}', record)):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    directory = customer_directory(db, workspace, snapshot, [old_id, new_id])
    old = current_card(old_id, proof['new_branch'], directory)
    new = current_card(new_id, proof['new_branch'], directory)
    if (not allowed(old, new) or str(old['contact_id']) != str(contact)
            or str(new['contact_id']) != proof.get('new_contact')
            or phone_guard(new['primary_phone']) != proof.get('phone_guard')):
        return None
    # Revalidate two exact retained sources, and never hide newly arrived data
    # or an explicit operator link in the old conversation.
    return db.execute(text("SELECT c.id FROM conversations c JOIN conversations o "
        "ON o.workspace_id=c.workspace_id AND o.connection_id=c.connection_id "
        "JOIN messages a ON a.workspace_id=c.workspace_id AND a.id=:old_message "
        "JOIN messages b ON b.workspace_id=c.workspace_id AND b.id=:new_message "
        "WHERE c.workspace_id=:w AND c.connection_id=:channel AND c.id=:canonical AND o.id=:old "
        "AND c.contact_id=:new_contact AND o.contact_id=:old_contact "
        "AND NOT c.crm_link_manual AND NOT o.crm_link_manual "
        "AND a.conversation_id=c.id AND b.conversation_id=c.id "
        "AND a.direction='in' AND b.direction='in' AND a.actor_type='contact' AND b.actor_type='contact' "
        "AND a.payload->>'branch_id'=:branch AND b.payload->>'branch_id'=:branch "
        "AND a.payload->>'crm_record_id'=:record AND b.payload->>'crm_record_id'=:record "
        "AND a.occurred_at=b.occurred_at AND a.occurred_at=CAST(:at AS timestamptz) "
        "AND a.payload->>'customer_id'=:old_customer AND b.payload->>'customer_id'=:new_customer "
        "AND (a.payload->>'max_user_id' IS NULL OR b.payload->>'max_user_id' IS NULL "
        "OR a.payload->>'max_user_id'=b.payload->>'max_user_id') "
        "AND b.payload->'crm_observed_customers'=CAST(:observed AS jsonb) "
        "AND coalesce(b.payload->>'crm_recipient_stale','false')<>'true' "
        "AND coalesce(b.payload->>'deleted','false')<>'true' "
        "AND EXISTS(SELECT 1 FROM source_records s WHERE s.workspace_id=:w AND s.connection_id=:channel "
        "AND s.namespace='alfa_max' AND s.message_id=b.id "
        "AND s.external_record_id IN (:source,:recipient_source)) "
        "AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.workspace_id=:w AND m.conversation_id=o.id)"),
        {'w': workspace, 'channel': channel, 'canonical': canonical, 'old': old_conversation,
         'old_message': old_message, 'new_message': new_message, 'new_contact': new['contact_id'],
         'old_contact': old['contact_id'], 'branch': str(branch), 'record': record, 'at': at,
         'old_customer': str(old_id), 'new_customer': str(new_id),
         'observed': json.dumps([[branch, new_id]]), 'source': f'in:{branch}:{record}',
         'recipient_source': f'in:{branch}:{record}|recipient|{new["branch_id"]}:{new_id}'}).scalar_one_or_none()


def repair_batch(service, db, workspace, channel, snapshot, cursor, *, limit=3):
    previous = cursor.get('archived_source_repair') or {}
    generation = [str(snapshot['run']), snapshot['version']]
    if previous.get('generation') == generation and previous.get('next_at'):
        try:
            if datetime.fromisoformat(previous['next_at']) > datetime.now(UTC):
                return False
        except (ValueError, TypeError):
            pass
    after = previous.get('after') if previous.get('generation') == generation else None
    # Scan only old empty-phone archives, bounded by conversation ID. A wrap
    # allows a later metadata refresh or a completed send to unblock recovery.
    rows = list(db.execute(text("SELECT c.id FROM conversations c WHERE c.workspace_id=:w AND c.connection_id=:channel "
        "AND NOT c.crm_link_manual AND c.contact_id IS NOT NULL AND c.last_inbound_at IS NOT NULL "
        "AND (CAST(:after AS uuid) IS NULL OR c.id>CAST(:after AS uuid)) "
        "AND EXISTS(SELECT 1 FROM alfa_customers a WHERE a.workspace_id=:w AND a.run_id=:run "
        "AND a.contact_id=c.contact_id AND a.status='archive' AND coalesce(a.phones->>0,'')='') "
        "AND EXISTS(SELECT 1 FROM messages m WHERE m.workspace_id=:w AND m.conversation_id=c.id "
        "AND m.direction='in' AND m.payload->>'crm_recipient_stale'='true' "
        "AND m.payload->>'superseded_by' IS NULL) ORDER BY c.id LIMIT :limit"),
        {'w': workspace, 'channel': channel, 'after': after, 'run': snapshot['run'], 'limit': limit+1}).scalars())
    for conversation in rows[:limit]:
        recover(db, service, workspace, channel, snapshot, conversation)
    value = {'generation': generation, 'after': str(rows[limit-1]) if len(rows)>limit else None,
             'next_at': (datetime.now(UTC)+timedelta(seconds=30)).isoformat()}
    cursor['archived_source_repair'] = value
    return bool(rows or value != previous)
