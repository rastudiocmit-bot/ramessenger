"""Family care owned by the telephony process; tenant-scoped, versioned records."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import text
from center_messenger.accounts import context, audit
from center_messenger.auth_security import AuthError
from center_messenger.crm_api import DirectoryReader
from center_messenger.crm_directory import phone_number
from center_messenger.tasks import Tasks
from center_messenger import phone_care_model as model

CASE = 'phone-care-case-v1'
SOURCE = 'phone-care-source-v1'
SETTINGS = 'phone-care-settings-v1'
CAMPAIGN = 'phone-care-campaign-v1'
TRAINING = 'phone-care-training-v1'
DIGEST = 'phone-care-digest-v1'
MAX_REPORT = 5000


def load(db, workspace, topic, key):
    return db.execute(text('SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND event_key=:k'),
                      {'w': workspace, 't': topic, 'k': str(key)}).scalar_one_or_none()


def store(db, workspace, topic, key, payload, *, pending=False):
    db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                    "VALUES(:w,:id,:t,:k,CAST(:p AS jsonb),:state) ON CONFLICT(workspace_id,topic,event_key) "
                    "DO UPDATE SET payload=EXCLUDED.payload,state=EXCLUDED.state"),
               {'w': workspace, 'id': uuid4(), 't': topic, 'k': str(key), 'p': json.dumps(payload, ensure_ascii=False),
                'state': 'pending' if pending else 'published'})


def enqueue(db, workspace, call, *, priority=1):
    store(db, workspace, SOURCE, call, {'call_id': str(call), 'priority': priority}, pending=True)


def lock(db, workspace, key):
    value = int.from_bytes(hashlib.sha256(f'phone-care:{workspace}:{key}'.encode()).digest()[:8], 'big', signed=True)
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': value})


def fresh_case(db, workspace, case_id):
    value = load(db, workspace, CASE, case_id)
    if not value or value.get('id') != str(case_id): raise AuthError(404, 'Обращение не найдено.')
    state = db.execute(text('SELECT account_id,connected FROM telephony_state WHERE workspace_id=:w'), {'w': workspace}).mappings().one_or_none()
    if not state or state['account_id'] != value['account_id']: raise AuthError(404, 'Обращение относится к другому подключению.')
    return deepcopy(value)


def save_case(db, workspace, value):
    value['summary'] = model.case_summary(value)
    store(db, workspace, CASE, value['id'], value)


class FamilyCare:
    def __init__(self, accounts, *, clock=model.now_utc):
        self.accounts, self.clock = accounts, clock
        # Reuse the application task service, in the same database transaction.
        self.tasks = Tasks(type('Services', (), {'accounts': accounts})())

    @contextmanager
    def enter(self, actor, workspace, *, write=False, admin=False, audit_read=False):
        with self.accounts.transaction(actor, snapshot=not (write or audit_read)) as db:
            member = self.accounts._workspace_chat(db, actor, workspace)
            manager = member['role'] in ('owner', 'admin')
            if (admin and not manager) or (write and member['role'] == 'observer'):
                raise AuthError(403, 'Для этого действия недостаточно прав.')
            yield db, member

    @staticmethod
    def version(value, expected):
        if value['version'] != expected: raise AuthError(409, 'Обращение изменилось. Обновите его перед сохранением.')

    @staticmethod
    def visible(value, actor, member):
        # Administrators assign work; operators see their own and unassigned follow-ups.
        return member['role'] in ('owner', 'admin') or value.get('assignee_id') in (None, str(actor.user_id))

    def case(self, db, actor, member, workspace, case_id):
        value = fresh_case(db, workspace, case_id)
        if not self.visible(value, actor, member) and not db.execute(text(
                "SELECT 1 FROM tasks WHERE workspace_id=:w AND (assignee_id=:me OR created_by=:me) "
                "AND sources @> CAST(:source AS jsonb) LIMIT 1"),
                {'w':workspace,'me':actor.user_id,'source':json.dumps([{'case_id':str(case_id)}])}).scalar_one_or_none():
            raise AuthError(404, 'Обращение недоступно.')
        return value

    def _members(self, db, workspace):
        return [dict(row) for row in db.execute(text(
            "SELECT m.user_id AS id,u.display_name AS name,u.email,m.role,m.sections FROM workspace_memberships m JOIN auth_users u ON u.id=m.user_id "
            "WHERE m.workspace_id=:w AND m.active AND u.active AND m.role<>'observer' "
            "AND (m.role IN ('owner','admin') OR 'messages'=ANY(coalesce(m.sections,ARRAY[]::text[]))) ORDER BY u.display_name,m.user_id"),
            {'w': workspace}).mappings()]

    def board(self, actor, workspace, data):
        with self.enter(actor, workspace) as (db, member):
            state = db.execute(text('SELECT account_id,connected FROM telephony_state WHERE workspace_id=:w'), {'w': workspace}).mappings().one_or_none()
            account = state['account_id'] if state else None
            # Project small summaries in PostgreSQL; do not load thousands of transcripts/evidence arrays.
            manager = member['role'] in ('owner', 'admin')
            linked = [r['case_id'] for r in db.execute(text("SELECT DISTINCT source->>'case_id' AS case_id FROM tasks CROSS JOIN LATERAL jsonb_array_elements(sources) source WHERE workspace_id=:w AND (assignee_id=:me OR created_by=:me) AND source->>'type' IN ('call','phone_case')"), {'w':workspace,'me':actor.user_id}).mappings()] if not manager else []
            rows = list(db.execute(text("SELECT payload->'summary' AS item FROM outbox_events WHERE workspace_id=:w AND topic=:t "
                "AND payload->>'account_id'=:account AND (:manager OR payload->>'assignee_id' IS NULL OR payload->>'assignee_id'=:me OR event_key=ANY(CAST(:linked AS text[]))) ORDER BY event_key LIMIT :limit"),
                {'w': workspace, 't': CASE, 'account': account, 'limit': MAX_REPORT+1, 'manager':manager,'me':str(actor.user_id),'linked':linked}).mappings())
            values = [row['item'] for row in rows[:MAX_REPORT] if row['item']]
            visible_campaigns = {campaign for value in values for campaign in value.get('campaigns', [])}
            for key in ('status', 'campaign', 'interest', 'branch', 'source', 'reason'):
                selected = str(data[key]) if data.get(key) is not None else None
                if selected:
                    if key=='status' and selected=='active': values=[x for x in values if x['status'] in ('open','working','waiting')]
                    elif key in ('campaign','interest','reason'): values=[x for x in values if selected in (x.get({'campaign':'campaigns','interest':'interests','reason':'reasons'}[key]) or [])]
                    else: values=[x for x in values if x.get(key)==selected]
            def activity(x):return max(model.moment(x.get('last_call_at') or x['created_at']),model.moment(x.get('work_at') or x.get('last_call_at') or x['created_at']))
            if data.get('since'): values = [x for x in values if activity(x) >= model.moment(data['since'])]
            if data.get('until'): values = [x for x in values if activity(x) < model.moment(data['until'])]
            values.sort(key=lambda x: (x.get('status') not in ('open','working','waiting'), x.get('due_at') or '9999', x.get('last_call_at') or ''), reverse=False)
            settings = load(db, workspace, SETTINGS, 'workspace') or {'version': 0, 'enabled': True}
            control = load(db, workspace, SETTINGS, 'bootstrap') or {}
            campaigns = [row['payload'] for row in db.execute(text('SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t ORDER BY created_at DESC LIMIT 100'), {'w': workspace, 't': CAMPAIGN}).mappings()]
            if not manager: campaigns = [c for c in campaigns if c['id'] in visible_campaigns]
            offset = data.get('offset',0)
            scope = hashlib.sha256(json.dumps([(v['id'],v['version']) for v in values]).encode()).hexdigest()
            return {'cases': values[offset:offset+100], 'has_more': len(values) > offset+100, 'next_offset':offset+100 if len(values)>offset+100 else None,
                    'scope':scope,'report_limited': len(rows) > MAX_REPORT,
                    'metrics': model.metrics(values, now=self.clock()), 'settings': settings, 'progress': control,
                    'campaigns': campaigns, 'members': self._members(db, workspace) if manager else [],
                    'manager': manager, 'read_only': member['role'] == 'observer', 'connected': bool(state and state['connected']),
                    'branches': DirectoryReader._branches(db,workspace),
                    'filter_sources': sorted({x['item']['source'] for x in rows[:MAX_REPORT] if x['item'] and x['item'].get('source')}),
                    'labels': {'kinds': model.KINDS, 'stages': model.STAGES, 'statuses': model.STATUSES, 'campaigns': model.CAMPAIGNS,
                               'courses': {k:v[0] for k,v in model.COURSES.items()}},
                    'live_audio_ready': False, 'voice_assistant_ready': False,
                    'coverage_note': 'Показатели относятся к телефонным обращениям и этапам, подтверждённым сотрудником. Переписка и продажи без подтверждения сюда не включаются.'}

    def crm_context(self, db, workspace, value):
        snapshot = DirectoryReader._published(db, workspace)
        if not snapshot or not value.get('phone'): return {'contacts': [], 'groups': [], 'refreshed_at': None}
        contacts = [dict(row) for row in db.execute(text(
            'SELECT contact_id,display_name FROM alfa_directory WHERE workspace_id=:w AND run_id=:r AND phones ? :phone LIMIT 20'),
            {'w': workspace, 'r': snapshot['run'], 'phone': value['phone']}).mappings()]
        interests = [model.COURSES[k][0].split()[0] for k in value.get('interests', []) if k in model.COURSES]
        groups = []
        for interest in interests[:5]:
            groups.extend(dict(row) for row in db.execute(text(
                'SELECT branch_id,external_id,name FROM alfa_groups WHERE workspace_id=:w AND run_id=:r AND name ILIKE :q ORDER BY name LIMIT 10'),
                {'w': workspace, 'r': snapshot['run'], 'q': '%'+interest[:5]+'%'}).mappings())
        unique = {(str(x['branch_id']), str(x['external_id'])): x for x in groups}
        return {'contacts': contacts, 'groups': list(unique.values())[:20], 'refreshed_at': model.iso(snapshot['finished_at']),
                'availability_verified': False, 'note': 'Группы подобраны по названию из опубликованного снимка CRM. Свободные места, цены и запись нужно проверить отдельно.'}

    def detail(self, actor, workspace, data):
        with self.enter(actor, workspace) as (db, member):
            value = self.case(db, actor, member, workspace, data['case_id'])
            crm = self.crm_context(db, workspace, value)
            if crm['contacts']: value['display_name'] = ' / '.join(dict.fromkeys(x['display_name'] for x in crm['contacts']))
            else: value['display_name'] = value.get('label') or value.get('phone') or 'Скрытый номер'
            value['crm'] = crm
            value['can_edit'] = member['role'] in ('owner','admin') or (member['role']!='observer' and value.get('assignee_id') in (None,str(actor.user_id)))
            value['can_create_task'] = value['can_edit'] and (member['role'] in ('owner','admin') or 'tasks' in (member.get('sections') or []))
            task_ids = [UUID(x['task_id']) for x in value['signals'] if x.get('task_id')]
            task_member = self.tasks._member(db, actor, workspace); studio = self.tasks._studio(db, workspace)
            visibility = self.tasks._visible(task_member, studio)
            value['linked_tasks'] = [dict(row) for row in db.execute(text('SELECT t.id,t.title,t.status,t.due_at,t.assignee_name FROM tasks t WHERE workspace_id=:w AND id=ANY(CAST(:ids AS uuid[])) AND '+visibility), {'w': workspace, 'ids': task_ids,'me':actor.user_id}).mappings()] if task_ids else []
            return value

    def _assignee(self, db, actor, workspace, member, user, *, task=False):
        if user is None: return None
        user = str(UUID(str(user)))
        if member['role'] not in ('owner', 'admin') and user != str(actor.user_id): raise AuthError(403, 'Назначать другому сотруднику может администратор.')
        target = next((x for x in self._members(db, workspace) if str(x['id'])==user),None)
        if not target: raise AuthError(422, 'Выберите действующего сотрудника с доступом к чатам.')
        if task and target['role'] not in ('owner','admin') and 'tasks' not in (target.get('sections') or []):
            raise AuthError(422, 'У выбранного сотрудника нет доступа к задачам.')
        return user

    def patch(self, actor, workspace, data):
        with self.enter(actor, workspace, write=True) as (db, member):
            lock(db, workspace, data['case_id']); value = self.case(db, actor, member, workspace, data['case_id']); self.version(value, data['expected_version'])
            self.editable(actor, member, value)
            now = self.clock(); was_closed = value['status'] in ('resolved','dismissed')
            for key in ('status','stage','journey','label','branch','source'):
                if key in data: value[key] = data[key]
            if data.get('branch') and data['branch'] not in {x['id'] for x in DirectoryReader._branches(db,workspace)}:
                raise AuthError(422, 'Выберите филиал из текущего справочника CRM.')
            if 'assignee_id' in data: value['assignee_id'] = self._assignee(db, actor, workspace, member, data['assignee_id'])
            if 'due_at' in data: value['due_at'] = model.iso(data['due_at'])
            if was_closed and value['status'] in ('open','working','waiting'):
                value['resolved_at'] = None
                for item in value['signals']:
                    if item['state'] == 'reviewed': item['state'] = 'proposed'
            if data.get('dismiss_signal'):
                item = next((x for x in value['signals'] if x['id'] == data['dismiss_signal'] and x['state'] == 'proposed'), None)
                if not item: raise AuthError(409, 'Предложение уже изменилось.')
                item['state'] = 'dismissed'
            if data.get('reactivate_signal'):
                item=next((x for x in value['signals'] if x['id']==data['reactivate_signal'] and x['state'] in ('historical','reviewed','dismissed')),None)
                if not item or not data.get('note','').strip(): raise AuthError(422,'Выберите архивный сигнал и подтвердите, почему он актуален.')
                item['state']='proposed';value['status']='open';value['resolved_at']=None
            if value['status'] in ('resolved','dismissed'):
                if not data.get('note','').strip(): raise AuthError(422, 'Укажите результат обращения.')
                value['resolved_at'] = model.iso(now)
                for item in value['signals']:
                    if item['state'] == 'proposed': item['state'] = 'reviewed'
            if data.get('stage'):
                if not data.get('note','').strip(): raise AuthError(422, 'Подтвердите этап коротким пояснением.')
                value['reached_stages'] = sorted(set(value.get('reached_stages', ['contact'])) | {data['stage']})
            value['events'] = (value['events'] + [{'at': model.iso(now), 'actor_id': str(actor.user_id),
                'note': data.get('note','').strip(), 'status': value['status'], 'stage': value['stage']}])[-50:]
            value['version'] += 1; value['updated_at'] = model.iso(now); value['work_at']=model.iso(now)
            save_case(db, workspace, value); audit(db, 'phone_care_updated', user_id=actor.user_id, workspace_id=workspace, subject_id=value['id'])
            return {'id': value['id'], 'version': value['version']}

    def task(self, actor, workspace, data):
        with self.enter(actor, workspace, write=True) as (db, member):
            if member['role'] not in ('owner','admin') and 'tasks' not in (member.get('sections') or []): raise AuthError(403,'Для создания задачи нужен доступ к разделу «Задачи».')
            lock(db, workspace, data['case_id']); value = self.case(db, actor, member, workspace, data['case_id'])
            self.version(value, data['expected_version'])
            self.editable(actor, member, value)
            signal = next((x for x in value['signals'] if x['id'] == data['signal_id']), None)
            if not signal or signal['state'] not in ('proposed', 'confirmed'): raise AuthError(409, 'Это предложение уже изменилось. Обновите обращение.')
            source_call = self.verify_signal(db, workspace, value, signal, current=True)
            user = db.execute(text('SELECT display_name,email FROM auth_users WHERE id=:id AND active'), {'id':actor.user_id}).mappings().one()
            actor = replace(actor, display_name=user['display_name'], email=user['email'])
            assignee = self._assignee(db, actor, workspace, member, data.get('assignee_id') or actor.user_id, task=True)
            task_id = uuid5(UUID(str(workspace)), f'phone-care-task:{value["id"]}:{signal["id"]}')
            # A single transaction makes repeated confirmation and timeouts safe.
            source = {'id': 'call:'+signal['call_id'] if signal['call_id'] else 'phone-case:'+value['id'], 'type': 'call' if signal['call_id'] else 'phone_case', 'call_id': signal['call_id'], 'case_id': value['id'],
                      'text': signal['quote'] or signal['title'], 'at': signal['at']}
            if source_call is not None: source['direction'] = source_call['direction']
            task = self.tasks.create_in(db, actor, workspace, {'title': data['title'], 'details': data.get('details',''),
                'due_at': data['due_at'], 'assignee_id': UUID(assignee), 'client_label': value.get('label') or value.get('phone') or 'Телефонное обращение',
                'kind': 'call', 'origin': 'ai_hint'}, task_id=task_id, call_source=source)
            if signal.get('task_id') == task['id']:
                return {'task':task,'version':value['version']}
            signal.update(state='confirmed', task_id=task['id'])
            value['assignee_id'] = value['assignee_id'] or assignee; value['due_at'] = value['due_at'] or model.iso(data['due_at'])
            value['status'] = 'working'; value['version'] += 1; value['updated_at'] = model.iso(self.clock());value['work_at']=value['updated_at']
            save_case(db, workspace, value)
            audit(db, 'phone_care_task_confirmed', user_id=actor.user_id, workspace_id=workspace, subject_id=value['id'])
            return {'task': task, 'version': value['version']}

    def campaign(self, actor, workspace, data):
        with self.enter(actor, workspace, write=True, admin=True) as (db, member):
            identifier = str(data['request_id']); lock(db, workspace, 'campaign:'+identifier)
            previous = load(db, workspace, CAMPAIGN, identifier)
            if not data['name'].strip(): raise AuthError(422, 'Укажите название очереди.')
            if previous:
                if previous['name'] != data['name'].strip() or previous['kind'] != data['kind']: raise AuthError(409, 'Этот запрос уже создал другую очередь.')
                return previous
            value = {'id': identifier, 'name': data['name'].strip(), 'kind': data['kind'], 'created_at': model.iso(self.clock()), 'created_by': str(actor.user_id)}
            store(db, workspace, CAMPAIGN, identifier, value)
            audit(db, 'phone_care_campaign_created', user_id=actor.user_id, workspace_id=workspace, subject_id=identifier)
            return value

    def search(self, actor, workspace, data):
        with self.enter(actor, workspace, admin=True) as (db, member):
            snapshot = DirectoryReader._published(db, workspace)
            if not snapshot: return {'contacts': []}
            query = data['query'].strip()
            if len(query) < 3: return {'contacts': []}
            escaped = query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            rows = [dict(row) for row in db.execute(text('SELECT contact_id,display_name,phones FROM alfa_directory WHERE workspace_id=:w AND run_id=:r AND search_text ILIKE :q ORDER BY display_name LIMIT 20'),
                              {'w': workspace, 'r': snapshot['run'], 'q': '%'+escaped+'%'}).mappings()]
            account = db.execute(text('SELECT account_id FROM telephony_state WHERE workspace_id=:w'), {'w':workspace}).scalar_one_or_none()
            identifiers = {model.family_id(workspace,account,phone_number(phone)) for row in rows for phone in row['phones'] if phone_number(phone)}
            known = {r['event_key']:r['item'] for r in db.execute(text("SELECT event_key,payload->'summary' AS item FROM outbox_events WHERE workspace_id=:w AND topic=:t AND event_key=ANY(CAST(:ids AS text[]))"), {'w':workspace,'t':CASE,'ids':list(identifiers)}).mappings()} if identifiers else {}
            for row in rows: row['cases'] = {phone:known.get(model.family_id(workspace,account,phone_number(phone))) for phone in row['phones'] if phone_number(phone)}
            return {'contacts': rows}

    def add_to_campaign(self, actor, workspace, data):
        with self.enter(actor, workspace, write=True, admin=True) as (db, member):
            campaign = load(db, workspace, CAMPAIGN, data['campaign_id'])
            if not campaign: raise AuthError(404, 'Очередь обзвона не найдена.')
            snapshot = DirectoryReader._published(db, workspace)
            if not snapshot: raise AuthError(409, 'Сначала обновите справочник CRM.')
            contact = db.execute(text('SELECT display_name,phones FROM alfa_directory WHERE workspace_id=:w AND run_id=:r AND contact_id=:c'),
                {'w': workspace, 'r': snapshot['run'], 'c': data['contact_id']}).mappings().one_or_none()
            phone = phone_number(data['phone'])
            if not contact or not phone or phone not in {phone_number(x) for x in contact['phones']}: raise AuthError(422, 'Выберите номер этого контакта из CRM.')
            state = db.execute(text('SELECT account_id FROM telephony_state WHERE workspace_id=:w AND connected'), {'w': workspace}).scalar_one_or_none()
            if not state: raise AuthError(409, 'Сначала проверьте подключение Новофона.')
            case_id = model.family_id(workspace, state, phone); lock(db, workspace, case_id)
            value = load(db, workspace, CASE, case_id)
            if value: self.version(value, data.get('expected_version'))
            if value and campaign['id'] in value['campaigns']:
                return {'id':case_id,'version':value['version'],'already':True}
            if not value:
                # No fabricated call is added: this is a staff-created work item.
                now = model.iso(self.clock())
                value = {'id': case_id, 'version': 0, 'account_id': state, 'phone': phone, 'source_call_id': None,
                    'status': 'open', 'stage': 'contact', 'journey': 'unknown', 'assignee_id': None, 'due_at': None,
                    'label': contact['display_name'], 'signals': [], 'calls': [], 'interests': [], 'campaigns': [], 'events': [], 'created_at': now, 'resolved_at': None}
            if campaign['id'] not in value['campaigns']:
                if len(value['campaigns']) >= 20: raise AuthError(422, 'У обращения уже 20 очередей. Завершите часть из них.')
                value['campaigns'].append(campaign['id'])
                value['signals'].append({'id':model.evidence_id(value['id'],'campaign',campaign['id']), 'kind':'campaign',
                    'campaign_id':campaign['id'],'title':'Связаться по очереди «'+campaign['name']+'»','call_id':None,'run_id':None,
                    'at':model.iso(self.clock()),'quote':'','offset_seconds':0,'channel':None,'state':'proposed','suggested_due_at':model.iso(data['due_at'])})
            value['version'] += 1; value['status'] = 'open'; value['updated_at'] = model.iso(self.clock());value['work_at']=value['updated_at']
            value['assignee_id'] = self._assignee(db, actor, workspace, member, data.get('assignee_id')) or value['assignee_id']
            value['due_at'] = model.iso(data['due_at']); save_case(db, workspace, value)
            audit(db, 'phone_care_campaign_member_added', user_id=actor.user_id, workspace_id=workspace, subject_id=case_id)
            return {'id': case_id, 'version': value['version']}

    def training(self, actor, workspace, data):
        with self.enter(actor, workspace, write=True, admin=True) as (db, member):
            value = self.case(db, actor, member, workspace, data['case_id'])
            signal = next((x for x in value['signals'] if x['id'] == data['signal_id'] and x.get('quote') and x.get('run_id')), None)
            if not signal: raise AuthError(422, 'Выберите фрагмент расшифровки с исходной цитатой.')
            self.verify_signal(db, workspace, value, signal)
            if not data['title'].strip(): raise AuthError(422, 'Укажите название примера.')
            identifier = str(data['request_id']); lock(db, workspace, 'training:'+identifier)
            previous = load(db, workspace, TRAINING, identifier)
            if previous: return previous
            result = {'id': identifier, 'account_id':value['account_id'], 'title': data['title'].strip(), 'category': data['category'], 'good': data.get('good',''),
                'improve': data.get('improve',''), 'evidence': signal, 'case_id': value['id'], 'created_at': model.iso(self.clock()), 'created_by': str(actor.user_id)}
            store(db, workspace, TRAINING, identifier, result)
            audit(db, 'phone_care_training_saved', user_id=actor.user_id, workspace_id=workspace, subject_id=identifier)
            return result

    def library(self, actor, workspace, data):
        with self.enter(actor, workspace, admin=True) as (db, member):
            rows = [r['payload'] for r in db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND payload->>'account_id'=(SELECT account_id FROM telephony_state WHERE workspace_id=:w) ORDER BY created_at DESC,id DESC LIMIT 21 OFFSET :offset"), {'w': workspace, 't': TRAINING,'offset':data.get('offset',0)}).mappings()]
            return {'examples': rows[:20], 'next_offset':data.get('offset',0)+20 if len(rows)>20 else None}

    def call(self, actor, workspace, data):
        # Audio access is read-only for the user, but its audit record is a write.
        with self.enter(actor, workspace, audit_read=bool(data.get('recording'))) as (db, member):
            value = self.case(db, actor, member, workspace, data['case_id'])
            row = db.execute(text('SELECT c.id,c.phone,c.recording_ciphertext,s.connected,r.id AS run_id,r.phase,substring(r.transcript FROM :start FOR 12000) AS transcript,length(r.transcript) AS text_length FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id LEFT JOIN telephony_ai_links l ON l.workspace_id=c.workspace_id AND l.call_id=c.id LEFT JOIN telephony_ai_runs r ON r.workspace_id=c.workspace_id AND r.call_id=c.id AND r.id=coalesce(CAST(:run AS uuid),l.run_id) WHERE c.workspace_id=:w AND c.id=:c AND c.account_id=:a'),
                {'w': workspace, 'c': data['call_id'], 'a': value['account_id'], 'start': data.get('offset',0)+1, 'run':data.get('run_id')}).mappings().one_or_none()
            if not row or not (row['phone'] == value.get('phone') and (row['phone'] is not None or str(row['id']) == value['source_call_id'])):
                raise AuthError(404, 'Звонок недоступен в этом обращении.')
            if data.get('run_id') and str(row['run_id']) != str(data['run_id']): raise AuthError(409, 'Расшифровка изменилась. Откройте её заново.')
            if data.get('recording'):
                if not row['connected'] or not row['recording_ciphertext']: raise AuthError(404, 'Запись недоступна.')
                audit(db, 'phone_care_recording_read', user_id=actor.user_id, workspace_id=workspace, subject_id=str(row['id']))
                return {'authorized': True}
            text_value = row['transcript'] if row['phase'] == 'complete' else ''
            return {'run_id': str(row['run_id']) if row['run_id'] else None, 'text': text_value or '', 'phase': row['phase'] or 'not_requested',
                    'next_offset': data.get('offset',0)+len(text_value or '') if text_value and data.get('offset',0)+len(text_value) < row['text_length'] else None}

    def history(self, actor, workspace, data):
        with self.enter(actor, workspace) as (db, member):
            value = self.case(db, actor, member, workspace, data['case_id'])
            where = ' AND (c.started_at,c.id)<(:before_time,:before_id)' if data.get('before_time') else ''
            rows = list(db.execute(text('SELECT c.id,c.started_at,c.direction,c.missed,c.talk_seconds,c.total_seconds,c.recording_ciphertext IS NOT NULL AND s.connected AS recording_available FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND c.account_id=:a AND c.phone IS NOT DISTINCT FROM :phone AND (:phone IS NOT NULL OR c.id=CAST(:hidden AS uuid))'+where+' ORDER BY c.started_at DESC,c.id DESC LIMIT 26'),
                {'w':workspace,'a':value['account_id'],'phone':value.get('phone'),'hidden':value.get('source_call_id'),
                 'before_time':data.get('before_time'),'before_id':data.get('before_id')}).mappings())
            shown = rows[:25]
            return {'calls':[dict(x) for x in shown], 'next':{'before_time':model.iso(shown[-1]['started_at']),'before_id':str(shown[-1]['id'])} if len(rows)>25 else None}

    def digest(self, actor, workspace, data):
        with self.enter(actor, workspace, admin=True) as (db, member):
            row = db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND payload->>'account_id'=(SELECT account_id FROM telephony_state WHERE workspace_id=:w) ORDER BY event_key DESC LIMIT 1"), {'w': workspace, 't': DIGEST}).scalar_one_or_none()
            return row or {'ready': False, 'note': 'Сводка появится после фонового прохода. Она сохраняется ежедневно после 09:00 по времени центра.'}

    def settings(self, actor, workspace, data):
        with self.enter(actor, workspace, write=True, admin=True) as (db, member):
            lock(db, workspace, 'settings'); value = load(db, workspace, SETTINGS, 'workspace') or {'version':0,'enabled':True}
            self.version(value, data['expected_version']); value.update(enabled=data['enabled'],version=value['version']+1)
            store(db, workspace, SETTINGS, 'workspace', value)
            audit(db, 'phone_care_settings_updated', user_id=actor.user_id, workspace_id=workspace)
            return value

    @staticmethod
    def editable(actor, member, value):
        if member['role'] not in ('owner','admin') and value.get('assignee_id') not in (None,str(actor.user_id)):
            raise AuthError(403, 'Изменять обращение может ответственный сотрудник или администратор.')

    @staticmethod
    def verify_signal(db, workspace, value, signal, *, current=False):
        if not signal.get('call_id'):
            if signal['kind'] != 'campaign' or signal.get('campaign_id') not in value['campaigns']: raise AuthError(409, 'Источник предложения недоступен.')
            return
        row = db.execute(text('SELECT c.phone,c.direction,r.phase,l.run_id,CASE WHEN :quote=\'\' THEN true ELSE position(:quote in r.transcript)>0 END AS literal FROM telephony_calls c LEFT JOIN telephony_ai_runs r ON r.workspace_id=c.workspace_id AND r.call_id=c.id AND r.id=CAST(:run AS uuid) LEFT JOIN telephony_ai_links l ON l.workspace_id=c.workspace_id AND l.call_id=c.id WHERE c.workspace_id=:w AND c.id=:c AND c.account_id=:a'),
            {'w':workspace,'c':UUID(signal['call_id']),'a':value['account_id'],'run':signal.get('run_id'),'quote':signal.get('quote','')}).mappings().one_or_none()
        if not row or row['phone'] != value.get('phone') or (row['phone'] is None and signal['call_id'] != value.get('source_call_id')) or not row['literal'] or (signal.get('quote') and row['phase'] != 'complete'):
            raise AuthError(409, 'Исходный фрагмент изменился или недоступен. Обновите обращение.')
        if current and signal.get('run_id') and str(row['run_id']) != signal['run_id']:
            raise AuthError(409, 'Есть новая расшифровка звонка. Дождитесь обновления предложений.')
        return row
