"""Publish a whole verified family at once; staging sections never replace it."""
import copy
import hashlib
import json

from sqlalchemy import text


def scope(family):
    sources = sorted((str(person['id']), str(card['branch_id']), str(card['id']))
                     for person in family['students'] for card in person['cards'])
    # No phone/name matching here. A replaced student or Customer ID is a new scope.
    return hashlib.sha256(json.dumps([str(family['id']), sources]).encode()).hexdigest()


def complete(family):
    sections = family.get('cache', {}).get('sections', {})
    return (not family.get('has_more') and not family.get('cache', {}).get('mapping_pending')
            and all(sections.get(key, {}).get('status') in {'ready', 'stale'}
                    for key in ('finance', 'lessons', 'history', 'groups')))


def publish(db, workspace, snapshot, family):
    if not complete(family):
        return False
    from center_messenger.chat_card_view import compact_card
    payload = {'students': family['students'], 'updated_at': family['cache']['updated_at'],
               'compact_students': compact_card(family)['students']}
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode()) > 8 * 1024 * 1024:
        return False  # Keep the previous complete view; staging data is still durable.
    db.execute(text("INSERT INTO crm_family_snapshots(workspace_id,connection_id,scope_key,settings_version,payload) "
        "VALUES(:w,:c,:key,:v,CAST(:p AS jsonb)) ON CONFLICT(workspace_id,connection_id,scope_key) "
        "DO UPDATE SET payload=EXCLUDED.payload,settings_version=EXCLUDED.settings_version,published_at=clock_timestamp() "
        "WHERE crm_family_snapshots.payload IS DISTINCT FROM EXCLUDED.payload "
        "OR crm_family_snapshots.settings_version<>EXCLUDED.settings_version"),
        {'w': workspace, 'c': snapshot['connection_id'], 'v': snapshot['version'],
         'key': scope(family), 'p': encoded})
    return True


def load(db, workspace, snapshot, family, *, compact=False):
    # Projection happens in PostgreSQL: chat reads do not deserialize a year's
    # raw lessons just to throw them away. Legacy snapshots remain readable.
    students = "payload->'students'"
    if compact:
        students = """coalesce(payload->'compact_students',(
          SELECT jsonb_agg((person-'cards')||jsonb_build_object('cards',(
            SELECT jsonb_agg((card-'lessons'-'subscriptions')||jsonb_build_object(
              'lessons_ready',coalesce(jsonb_typeof(card->'lessons')='array',false),
              'subscriptions_ready',coalesce(jsonb_typeof(card->'subscriptions')='array',false)) ORDER BY ordinal)
            FROM jsonb_array_elements(person->'cards') WITH ORDINALITY AS cards(card,ordinal))) ORDER BY ordinal)
          FROM jsonb_array_elements(payload->'students') WITH ORDINALITY AS people(person,ordinal)))"""
    saved = db.execute(text("SELECT " + students + " AS students,payload->>'updated_at' AS updated_at,published_at "
        "FROM crm_family_snapshots WHERE workspace_id=:w AND connection_id=:c "
        "AND scope_key=:key AND settings_version=:v"),
        {'w': workspace, 'c': snapshot['connection_id'], 'v': snapshot['version'], 'key': scope(family)}).mappings().one_or_none()
    if saved is None:
        return None
    people = copy.deepcopy(saved['students'])
    current = {str(person['id']): person for person in family['students']}
    for person in people:
        live = current[str(person['id'])]
        person.update({key: live[key] for key in ('name', 'dob') if key in live})
        cards = {(str(card['branch_id']), str(card['id'])): card for card in live['cards']}
        for card in person['cards']:
            live_card = cards[(str(card['branch_id']), str(card['id']))]
            card.update({key: live_card[key] for key in ('status', 'study_status', 'legal_name', 'branch_name', 'url') if key in live_card})
    return {**family, 'students': people, 'cache': {**family.get('cache', {}), 'snapshot': {
        'published_at': saved['published_at'].isoformat(), 'updated_at': saved['updated_at'], 'complete': True}}}


def visible(db, workspace, snapshot, family):
    saved = load(db, workspace, snapshot, family)
    if saved is not None:
        return saved
    family['cache']['snapshot'] = None
    return family
