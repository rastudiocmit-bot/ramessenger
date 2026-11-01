"""Bounded local processing in telephony-worker, separate durable source topic."""
from datetime import timedelta
import logging
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from center_messenger.accounts import context
from center_messenger.crm_api import DirectoryReader
from center_messenger import phone_care as care, phone_care_model as model

LOG = logging.getLogger(__name__)


class CareWorker:
    def __init__(self, accounts, *, clock=model.now_utc):
        self.accounts, self.clock, self.after = accounts, clock, UUID(int=0)

    def run_next(self):
        # Routing IDs only are read outside a tenant scope, like the existing phone loader.
        with self.accounts.engine.begin() as db:
            workspace = db.execute(text('SELECT workspace_id FROM telephony_routes WHERE workspace_id>:after ORDER BY workspace_id LIMIT 1'), {'after': self.after}).scalar_one_or_none()
            if workspace is None:
                self.after = UUID(int=0)
                workspace = db.execute(text('SELECT workspace_id FROM telephony_routes ORDER BY workspace_id LIMIT 1')).scalar_one_or_none()
        if workspace is None:
            self.after = UUID(int=0); return False
        self.after = workspace
        try: return self.process(workspace)
        except Exception as error:
            # Never log a transcript, phone, query parameters or an exception's message.
            LOG.warning('Family care interrupted exception=%s', type(error).__name__)
            return False

    def bootstrap(self, db, workspace, account):
        care.lock(db, workspace, 'bootstrap')
        control = care.load(db, workspace, care.SETTINGS, 'bootstrap') or {'after': str(UUID(int=0)), 'imported': 0}
        if control.get('account_id') != account:
            control = {'after': str(UUID(int=0)), 'imported': 0, 'account_id': account}
        if not control.get('complete'):
            rows = list(db.execute(text('SELECT id FROM telephony_calls WHERE workspace_id=:w AND account_id=:a AND id>:after ORDER BY id LIMIT 16'),
                {'w': workspace, 'a': account, 'after': UUID(control['after'])}).mappings())
            for row in rows:
                # Do not reset a completed source on a second bootstrap pass.
                db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) VALUES(:w,:id,:t,:k,jsonb_build_object('call_id',CAST(:k AS text)),'pending') ON CONFLICT(workspace_id,topic,event_key) DO UPDATE SET state='pending',payload=EXCLUDED.payload WHERE outbox_events.state='published' AND outbox_events.payload->>'processed_account' IS DISTINCT FROM :account"),
                    {'w': workspace, 'id': uuid4(), 't': care.SOURCE, 'k': str(row['id']),'account':account})
            if rows: control.update(after=str(rows[-1]['id']), imported=control['imported']+len(rows))
            control['complete'] = len(rows) < 16
        control.update(last_scan_at=model.iso(self.clock()))
        care.store(db, workspace, care.SETTINGS, 'bootstrap', control)
        return control

    def process(self, workspace):
        with self.accounts.engine.begin() as db:
            context(db, workspace_id=workspace, membership_admin='true')
            settings = care.load(db, workspace, care.SETTINGS, 'workspace') or {'enabled': True}
            if not settings['enabled']: return False
            state = db.execute(text('SELECT account_id,connected,timezone FROM telephony_state WHERE workspace_id=:w'), {'w': workspace}).mappings().one_or_none()
            if not state or not state['connected'] or not state['account_id']: return False
            progress = self.bootstrap(db, workspace, state['account_id'])
            source = db.execute(text("SELECT id,payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND state='pending' ORDER BY coalesce((payload->>'priority')::int,0) DESC,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"),
                {'w': workspace, 't': care.SOURCE}).mappings().one_or_none()
            if not source:
                self.digest(db, workspace, state, progress)
                return False
            try: call_id = UUID(source['payload']['call_id'])
            except (KeyError,ValueError,TypeError,AttributeError):
                db.execute(text("UPDATE outbox_events SET state='failed',payload=jsonb_build_object('error','invalid_source') WHERE workspace_id=:w AND id=:id"), {'w':workspace,'id':source['id']})
                return True
            call = db.execute(text('SELECT c.*,r.id AS run_id,CASE WHEN r.phase=\'complete\' THEN left(r.transcript,200000) ELSE NULL END AS transcript,coalesce(length(r.transcript)>200000,false) AS truncated FROM telephony_calls c LEFT JOIN telephony_ai_links l ON l.workspace_id=c.workspace_id AND l.call_id=c.id LEFT JOIN telephony_ai_runs r ON r.workspace_id=l.workspace_id AND r.id=l.run_id WHERE c.workspace_id=:w AND c.id=:id AND c.account_id=:a'),
                {'w': workspace, 'id': call_id, 'a': state['account_id']}).mappings().one_or_none()
            if call:
                case_id = model.family_id(workspace, call['account_id'], call['phone'], call['id']); care.lock(db, workspace, case_id)
                previous = care.load(db, workspace, care.CASE, case_id)
                analysis = model.analyze(call, call['transcript'] or '', run=call['run_id'], truncated=call['truncated'], zone=state['timezone'] or 'Europe/Moscow')
                value = model.merge_case(previous, call, analysis, workspace=workspace, now=self.clock())
                # A deterministic current CRM match labels a family, without changing MAX identity.
                snapshot = DirectoryReader._published(db, workspace)
                if snapshot and call['phone']:
                    names = db.execute(text('SELECT display_name FROM alfa_directory WHERE workspace_id=:w AND run_id=:r AND phones ? :phone ORDER BY display_name LIMIT 5'), {'w': workspace, 'r': snapshot['run'], 'phone': call['phone']}).scalars().all()
                    if names and not value.get('label'): value['label'] = ' / '.join(dict.fromkeys(names))[:200]
                care.save_case(db, workspace, value)
            db.execute(text("UPDATE outbox_events SET state='published',payload=payload||jsonb_build_object('processed_account',CAST(:account AS text)) WHERE workspace_id=:w AND id=:id"), {'w': workspace, 'id': source['id'],'account':state['account_id'] if call else None})
            self.digest(db, workspace, state, progress)
            return True

    def digest(self, db, workspace, state, progress):
        try: local = self.clock().astimezone(ZoneInfo(state['timezone'] or 'Europe/Moscow'))
        except (ValueError, ZoneInfoNotFoundError): local = self.clock().astimezone(ZoneInfo('Europe/Moscow'))
        if local.hour < 9 or not progress.get('complete'): return
        if db.execute(text("SELECT 1 FROM outbox_events WHERE workspace_id=:w AND topic=:t AND state='pending' LIMIT 1"), {'w':workspace,'t':care.SOURCE}).scalar_one_or_none(): return
        key = local.date().isoformat()
        if care.load(db, workspace, care.DIGEST, key): return
        rows = list(db.execute(text("SELECT payload->'summary' AS item FROM outbox_events WHERE workspace_id=:w AND topic=:t AND payload->>'account_id'=:a LIMIT :limit"), {'w': workspace, 't': care.CASE, 'a': state['account_id'], 'limit': care.MAX_REPORT+1}).mappings())
        cases = [x['item'] for x in rows[:care.MAX_REPORT] if x['item']]
        urgent = sorted((x for x in cases if x['status'] in ('open','working','waiting')), key=lambda x: (x.get('due_at') or '9999', x['id']))[:20]
        care.store(db, workspace, care.DIGEST, key, {'ready': True, 'date': key, 'account_id': state['account_id'],
            'generated_at': model.iso(self.clock()), 'metrics': model.metrics(cases, now=self.clock()), 'items': urgent,
            'coverage_complete': progress.get('complete', False), 'report_limited': len(rows)>care.MAX_REPORT,
            'note': 'Сводка по известным телефонным обращениям. Указанные этапы подтверждены сотрудниками; это не полный отчёт продаж центра.'})


def install_routes(app):
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import ValidationError
    from center_messenger.auth_security import AuthError, Identity
    from center_messenger.phone_care_protocol import WorkerInput, INPUTS

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse({'detail':'Проверьте заполнение полей.'}, status_code=422)

    @app.post('/care')
    def call(model:WorkerInput, request:Request):
        try:
            parsed = INPUTS[model.action].model_validate(model.data)
        except ValidationError:
            raise AuthError(422, 'Проверьте заполнение полей.') from None
        actor = Identity(user_id=model.actor.user_id, session_id=model.actor.session_id, auth_epoch=model.actor.auth_epoch,
                         email='', display_name='', email_verified=False, raw_session='')
        data = parsed.model_dump(exclude_unset=model.action=='patch')
        return getattr(request.app.state.family_care, model.action)(actor, model.workspace, data) | {'contract':'phone-family-care-v1'}
