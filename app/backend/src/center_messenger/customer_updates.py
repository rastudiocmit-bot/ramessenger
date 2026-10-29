"""ID-bound light facts, observation fences and coherent local contact labels.

No CRM/browser calls or message writes. Callers own the workspace write lock and
validate the published run/settings after IO. Observation clocks are captured
before IO and remain unchanged across checkpoint resumes.
"""
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from center_messenger.crm_directory import customer_row, normalized_name, phone_number

TOPIC = 'crm.customer.observation.v1'
LABEL_REPAIR = 'crm.customer.labels.v1'
LIGHT_FIELDS = ('name', 'legal_name', 'legal_type', 'dob', 'phones', 'emails',
                'status', 'study_status_id', 'lead_status_ids')
SOURCE_FIELDS = {'name': 'name', 'legal_name': 'legal_name', 'legal_type': 'legal_type',
                 'dob': 'dob', 'phones': 'phone', 'emails': 'email',
                 'study_status_id': 'study_status_id', 'lead_status_ids': 'lead_status_ids'}


def merge_fields(raw, old, *, archive=False):
    """Absent fields preserve facts; explicit empties follow customer_row's contract."""
    source = dict(raw)
    if old:
        source.setdefault('name', old['name'])
        source.setdefault('is_study', int(old['status'] == 'client'))
    value = customer_row(source, archive=archive)
    if old:
        for field, source_field in SOURCE_FIELDS.items():
            if source_field not in raw:
                value[field] = old[field]
        if 'is_study' not in raw and not archive:
            value['status'] = old['status']
    return value


def key(connection, run, branch, customer):
    return f'{connection}:{run}:{branch}:{customer}'


def observation(db, workspace, snapshot, branch, customer):
    return db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w "
        "AND topic=:topic AND event_key=:key AND state='published'"),
        {'w': workspace, 'topic': TOPIC, 'key': key(snapshot['connection_id'], snapshot['run'], branch, customer)}).scalar_one_or_none() or {}


def is_older(saved, observed_at):
    return bool(observed_at and saved.get('observed_at')
                and datetime.fromisoformat(saved['observed_at']) >= observed_at)


def observed_fields(raw, archive=False):
    return [f for f, raw_field in SOURCE_FIELDS.items() if raw_field in raw] + (
        ['status'] if archive or 'is_study' in raw else [])


def field_clocks(saved):
    return saved.get('fields', {f: saved['observed_at'] for f in LIGHT_FIELDS} if saved.get('observed_at') else {})


def record(db, workspace, snapshot, branch, customer, observed_at, *, revision=1, source='point',
           fields=None, saved=None, field_times=None):
    saved = observation(db, workspace, snapshot, branch, customer) if saved is None else saved
    clocks = dict(field_clocks(saved))
    clocks.update(field_times if field_times is not None else
                  {f: observed_at.isoformat() for f in (LIGHT_FIELDS if fields is None else fields)})
    if saved.get('observed_at'):
        observed_at = max(observed_at, datetime.fromisoformat(saved['observed_at']))
    payload = {'observed_at': observed_at.isoformat(), 'revision': revision, 'fields': clocks,
               'source': source, 'settings': snapshot.get('version')}
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:topic,:key,CAST(:p AS jsonb),'published') "
        "ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
        {'w': workspace, 'id': uuid4(), 'topic': TOPIC,
         'key': key(snapshot['connection_id'], snapshot['run'], branch, customer), 'p': json.dumps(payload)})


def carry_forward(db, workspace, connection, run, previous_run, rows, previous, started_at, version):
    """A slow explicit import must not roll back newer exact-ID point observations."""
    if not previous:
        return rows
    markers = [key(connection, generation, r['branch_id'], r['external_id'])
               for generation, sources in ((run, rows), (previous_run, previous)) for r in sources]
    saved = dict(db.execute(text("SELECT event_key,payload FROM outbox_events WHERE workspace_id=:w "
        "AND topic=:t AND event_key=ANY(CAST(:keys AS text[])) AND state='published'"),
        {'w': workspace, 't': TOPIC, 'keys': markers}).all())
    available = set(db.execute(text("SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
        "AND connection_id=:c AND available"), {'w': workspace, 'c': connection}).scalars())
    current = {(r['branch_id'], r['external_id']): dict(r) for r in rows}
    for old in previous:
        identity = old['branch_id'], old['external_id']
        marker = saved.get(key(connection, previous_run, *identity), {})
        if not marker.get('observed_at') or old['branch_id'] not in available:
            continue
        staged = saved.get(key(connection, run, *identity), {})
        at = datetime.fromisoformat(marker['observed_at'])
        if identity not in current and at <= started_at:
            continue
        old_clocks, new_clocks = field_clocks(marker), field_clocks(staged)
        copied = {f: stamp for f, stamp in old_clocks.items() if f in LIGHT_FIELDS and (
            (f not in new_clocks and ('fields' in staged or datetime.fromisoformat(stamp) > started_at))
            or (f in new_clocks and datetime.fromisoformat(stamp) > datetime.fromisoformat(new_clocks[f])))}
        if not copied:
            continue
        value = dict(current.get(identity, old))
        value.update({field: old[field] for field in copied}, workspace_id=workspace, run_id=run)
        fields = ('workspace_id', 'run_id', 'branch_id', 'external_id', *LIGHT_FIELDS)
        json_fields = {'phones', 'emails', 'lead_status_ids'}
        bindings = {field: json.dumps(value[field]) if field in json_fields else value[field] for field in fields}
        placeholders = ','.join(f'CAST(:{f} AS jsonb)' if f in json_fields else ':' + f for f in fields)
        updates = ','.join(f'{f}=EXCLUDED.{f}' for f in LIGHT_FIELDS)
        db.execute(text(f"INSERT INTO alfa_customers({','.join(fields)}) VALUES({placeholders}) "
            f"ON CONFLICT(workspace_id,run_id,branch_id,external_id) DO UPDATE SET {updates}"), bindings)
        record(db, workspace, {'connection_id': connection, 'run': run, 'version': version}, *identity,
               at, revision=max(marker.get('revision', 0), staged.get('revision', 0)) + 1,
               source='publication_carry', saved=staged, field_times=copied)
        current[identity] = value
    return [current[identity] for identity in sorted(current)]


def cleanup(db, workspace, connection, keep):
    """Observation retention follows the two catalog snapshots, not message history."""
    db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w AND topic IN (:observations,:labels) "
        "AND split_part(event_key,':',1)=:c AND NOT(split_part(event_key,':',2)=ANY(CAST(:keep AS text[])))"),
        {'w': workspace, 'c': str(connection), 'keep': list(map(str, keep)),
         'observations': TOPIC, 'labels': LABEL_REPAIR})


def label(cards, previous=None):
    phones = sorted({p for row in cards for p in row['phones']})
    names = {row['legal_name'] for row in cards if row['legal_name'] and not phone_number(row['legal_name'])}
    normalized = {normalized_name(name) for name in names}
    if previous and normalized_name(previous) in normalized and len(normalized) > 1:
        return previous
    if names:
        return min(names, key=lambda value: (normalized_name(value), value))
    return next(iter(phones), None) or next((p for row in cards for p in row['emails']), None) or cards[0]['name']


def refresh_contact(db, workspace, snapshot, contact):
    """Rebuild labels/search/reasons from this contact's current cards, not last writer."""
    cards = list(db.execute(text("SELECT * FROM alfa_customers WHERE workspace_id=:w "
        "AND run_id=:run AND contact_id=:contact ORDER BY external_id,branch_id"),
        {'w': workspace, 'run': snapshot['run'], 'contact': contact}).mappings())
    if not cards:
        return
    previous = db.execute(text("SELECT display_name FROM contacts WHERE workspace_id=:w AND id=:c"),
                          {'w': workspace, 'c': contact}).scalar_one_or_none()
    name = label(cards, previous)
    phones = sorted({p for row in cards for p in row['phones']})
    emails = sorted({p for row in cards for p in row['emails']})
    search = normalized_name(' '.join([name, *phones, *(p.lstrip('+') for p in phones), *emails,
        *(row['name'] + ' ' + row['legal_name'] + ' ' + str(row['external_id']) for row in cards)]))
    from center_messenger.crm_review import cached_status
    review = cached_status(db, workspace, snapshot, cards)
    params = {'w': workspace, 'run': snapshot['run'], 'c': contact, 'name': name,
              'phones': json.dumps(phones), 'emails': json.dumps(emails), 'search': search,
              'review': review['needs_review']}
    db.execute(text("UPDATE contacts SET display_name=:name WHERE workspace_id=:w AND id=:c "
        "AND display_name IS DISTINCT FROM :name"), params)
    db.execute(text("INSERT INTO alfa_directory(workspace_id,run_id,contact_id,display_name,phones,emails,needs_review,search_text) "
        "VALUES(:w,:run,:c,:name,CAST(:phones AS jsonb),CAST(:emails AS jsonb),:review,:search) "
        "ON CONFLICT(workspace_id,run_id,contact_id) DO UPDATE SET display_name=EXCLUDED.display_name,"
        "phones=EXCLUDED.phones,emails=EXCLUDED.emails,needs_review=EXCLUDED.needs_review,search_text=EXCLUDED.search_text "
        "WHERE (alfa_directory.display_name,alfa_directory.phones,alfa_directory.emails,alfa_directory.needs_review,alfa_directory.search_text) "
        "IS DISTINCT FROM (EXCLUDED.display_name,EXCLUDED.phones,EXCLUDED.emails,EXCLUDED.needs_review,EXCLUDED.search_text)"), params)
    db.execute(text("UPDATE alfa_customers SET needs_review=:review WHERE workspace_id=:w "
        "AND run_id=:run AND contact_id=:c AND needs_review IS DISTINCT FROM :review"), params)


def repair_labels(db, workspace, snapshot, *, limit=100):
    """One bounded local pass per published run; no history/archive reread."""
    valid = db.execute(text("SELECT 1 FROM alfa_sync_control c JOIN alfa_settings s ON s.workspace_id=c.workspace_id "
        "AND s.connection_id=c.connection_id WHERE c.workspace_id=:w AND c.connection_id=:c "
        "AND c.published_id=:r AND s.version=:v"), {'w': workspace, 'c': snapshot['connection_id'],
        'r': snapshot['run'], 'v': snapshot['version']}).scalar_one_or_none()
    if not valid:
        return 0
    event_key = f"{snapshot['connection_id']}:{snapshot['run']}"
    state = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t "
        "AND event_key=:key"), {'w': workspace, 't': LABEL_REPAIR, 'key': event_key}).scalar_one_or_none() or {}
    if state.get('complete'):
        return 0
    contacts = list(db.execute(text("SELECT DISTINCT contact_id FROM alfa_customers WHERE workspace_id=:w "
        "AND run_id=:r AND contact_id IS NOT NULL AND CAST(contact_id AS text)>:after ORDER BY contact_id LIMIT :n"),
        {'w': workspace, 'r': snapshot['run'], 'after': state.get('after', ''), 'n': limit + 1}).scalars())
    for contact in contacts[:limit]:
        refresh_contact(db, workspace, snapshot, contact)
    state.update(complete=len(contacts) <= limit, after=str(contacts[min(limit, len(contacts))-1]) if contacts else state.get('after', ''),
                 processed=state.get('processed', 0) + min(limit, len(contacts)))
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
        "VALUES(:w,:id,:t,:key,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
        "DO UPDATE SET payload=EXCLUDED.payload"), {'w': workspace, 'id': uuid4(), 't': LABEL_REPAIR,
        'key': event_key, 'p': json.dumps(state)})
    return min(limit, len(contacts))
