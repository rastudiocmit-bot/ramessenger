"""Read-only evidence for a stale CRM recipient; run inside the MAX worker.

No browser, messages, aliases, sessions or queues are changed. The requested
new Customer ID is a diagnostic candidate, never an instruction to relink it.
"""
import argparse
import hashlib
import json

from sqlalchemy import text

from center_messenger import alfa_shared_parent, native_relink
from center_messenger.alfa_repair import customer_directory, resolve_customer
from center_messenger.attachment_audit import load_source
from center_messenger.config import Settings
from center_messenger.crm_api import DirectoryReader
from center_messenger.database import make_engine


def phone_tag(value):
    return hashlib.sha256(('phone:' + value).encode()).hexdigest()[:12] if value else None


def safe_message(row):
    payload = row['payload']
    return {key: str(row[key]) if row.get(key) is not None else None
            for key in ('id', 'conversation_id', 'contact_id', 'occurred_at')} | {
        key: payload.get(key) for key in ('branch_id', 'crm_record_id', 'customer_id',
            'customer_branch_id', 'crm_customer_ids', 'crm_observed_customers',
            'crm_recipient_stale', 'crm_mirror', 'superseded_by', 'deleted')
    } | {'has_explicit_peer': bool(payload.get('max_user_id')), 'manual_link': row['crm_link_manual']}


def safe_alias(value, snapshot):
    card = value.get('reply_card') or {}
    return {'source': value.get('source'), 'contacts': value.get('contacts'),
            'canonical': value.get('canonical'), 'phone_tag': phone_tag(value.get('phone')),
            'reply_card': {k: card.get(k) for k in ('customer', 'branch', 'from', 'source')},
            'settings_match': card.get('settings_version') == snapshot.get('version'),
            'connection_match': card.get('crm_connection') == str(snapshot.get('connection_id'))}


def counterpart(old, new, new_customer):
    """Reconstruct only the complete saved observation, not a guessed family."""
    a, b = old['payload'], new['payload']
    if (str(a.get('branch_id')) != str(b.get('branch_id'))
            or str(a.get('crm_record_id')) != str(b.get('crm_record_id'))
            or str(b.get('customer_id')) != str(new_customer)):
        return None
    observed = b.get('crm_observed_customers')
    if not observed or any(not isinstance(p, list) or len(p) != 2 for p in observed):
        return None
    return {'direction': 'in', 'actor_type': 'contact', 'record_id': b['crm_record_id'],
            'occurred_at': new['occurred_at'], 'source_metadata_refresh': True,
            'max_user_id': b.get('max_user_id'),
            'customers': [{'branch_id': int(p[0]), 'customer_id': int(p[1])} for p in observed]}


def inspect(db, workspace, channel, snapshot, old_customer, new_customer):
    directory = customer_directory(db, workspace, snapshot, [old_customer, new_customer])
    contacts = {row['contact_id'] for rows in directory.values() for row in rows if row.get('contact_id')}
    result = {'publication': {'run': str(snapshot['run']), 'connection': str(snapshot['connection_id']),
                             'version': snapshot['version']}, 'cards': [], 'aliases': [], 'pairs': []}
    for identifier, rows in directory.items():
        for row in rows:
            result['cards'].append({key: str(row[key]) if key == 'contact_id' and row.get(key) else row.get(key)
                for key in ('branch_id', 'contact_id', 'status', 'available')} |
                {'customer_id': identifier, 'phone_tag': phone_tag(row.get('primary_phone'))})
    for contact in sorted(contacts):
        value = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
            "AND event_key=:key AND state='published'"), {'w': workspace, 't': alfa_shared_parent.TOPIC,
            'key': alfa_shared_parent._key(channel, contact)}).scalar_one_or_none()
        if value:
            result['aliases'].append(safe_alias(value, snapshot))
    rows = list(db.execute(text("SELECT m.id,m.occurred_at,m.payload,m.conversation_id,c.contact_id,c.crm_link_manual "
        "FROM messages m JOIN conversations c ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
        "WHERE m.workspace_id=:w AND c.connection_id=:channel AND m.direction='in' AND m.actor_type='contact' "
        "AND (c.contact_id=ANY(CAST(:contacts AS uuid[])) OR m.payload->>'customer_id'=ANY(CAST(:ids AS text[]))) "
        "ORDER BY m.occurred_at DESC,m.id DESC LIMIT 101"),
        {'w': workspace, 'channel': channel, 'contacts': list(contacts),
         'ids': [str(old_customer), str(new_customer)]}).mappings())
    result['message_sample_truncated'] = len(rows) > 100
    rows = rows[:100]
    result['source_messages'] = [safe_message(row) for row in rows]
    for previous in rows:
        payload = previous['payload']
        if str(payload.get('customer_id')) != str(old_customer) or not payload.get('crm_recipient_stale'):
            continue
        old = resolve_customer({'customer_id': old_customer,
            'branch_id': int(payload['customer_branch_id'])}, directory)
        target = native_relink.reply_target(db, workspace, channel, snapshot, dict(payload), directory)
        if len(result['pairs']) >= 20:
            result['pair_sample_truncated'] = True
            break
        pair = {'old_message_id': str(previous['id']), 'source_branch': payload.get('branch_id'),
                'record_id': payload.get('crm_record_id'),
                'saved_reply_target_customer': target.get('customer_id'),
                'saved_reply_target_stale': bool(target.get('crm_recipient_stale')), 'counterparts': []}
        result['pairs'].append(pair)
        for current in rows:
            item = counterpart(previous, current, new_customer)
            if item is None:
                continue
            new = resolve_customer({'customer_id': new_customer,
                'branch_id': int(current['payload']['customer_branch_id'])}, directory)
            phone = old.get('primary_phone')
            family = alfa_shared_parent.members(db, workspace, channel, snapshot, old.get('contact_id'), phone)
            checks = {'same_source_reassociation': native_relink.association_pair(previous, item, int(payload['branch_id'])) is not None,
                'same_timestamp_and_peer': native_relink.relink_pair(previous, item, int(payload['branch_id'])) is not None,
                'not_manual': not previous['crm_link_manual'],
                'old_global_id_verified': bool(old.get('global_id_verified')),
                'new_global_id_verified': bool(new.get('global_id_verified')),
                'same_nonempty_primary_phone': bool(phone and phone == new.get('primary_phone')),
                'previous_contact_in_verified_family': previous['contact_id'] in family,
                'family_current': alfa_shared_parent._current(db, workspace, snapshot,
                    set(family) | {new['contact_id']}, phone) if new.get('contact_id') else False,
                'conversation_history_proven': native_relink.history_proven(db, workspace, channel, previous, item,
                    list(set(family) | {new['contact_id']})) if new.get('contact_id') else False}
            pair['counterparts'].append({'new_message_id': str(current['id']), 'checks': checks,
                'failed_checks': [key for key, ok in checks.items() if not ok]})
    return result


def positive(value):
    result = int(value)
    if result < 1 or result > 2**63-1:
        raise argparse.ArgumentTypeError('Customer ID must be a positive bigint')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-customer', type=positive, required=True)
    parser.add_argument('--new-customer', type=positive, required=True)
    args = parser.parse_args(argv)
    if args.old_customer == args.new_customer:
        parser.error('Choose two different Customer IDs')
    config = Settings()
    if config.runtime_role != 'max':
        raise ValueError('Run inside the MAX worker')
    engine = make_engine(config)
    try:
        workspace, source, _ = load_source(engine)
        with engine.begin() as db:
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {'w': str(workspace)})
            snapshot = DirectoryReader._published(db, workspace)
            if not snapshot or snapshot['connection_id'] != source['crm_connection_id']:
                raise ValueError('Published CRM snapshot is not available')
            result = inspect(db, workspace, source['connection_id'], snapshot, args.old_customer, args.new_customer)
        print(json.dumps({'read_only': True, 'crm_requests': 0, 'message_posts': 0,
            'old_customer': args.old_customer, 'candidate_customer': args.new_customer,
            'limits': ['Saved evidence only; source/phone coincidence does not authorize a recipient change',
                       'Latest 100 incoming rows including hidden copies; at most 20 stale source pairs'], **result},
            ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        engine.dispose()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'read_only': True, 'message_posts': 0, 'error_code': type(error).__name__}))
        raise SystemExit(1)
