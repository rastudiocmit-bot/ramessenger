"""Tenant-scoped durable telephone AI work, leases and paid-operation receipts."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from center_messenger.accounts import context
from center_messenger.auth_security import AuthError
from center_messenger.telephony_agent import profile, FIXED_RULES
from center_messenger import telephony_archive as archive

TERMINAL = ('complete','error','needs_review')


def snapshot(row, workspace):
    value=profile(row,workspace)
    if not value['version']:raise AuthError(409,'Сначала сохраните телефонного агента.')
    result={key:value[key] for key in ('id','name','version','model','recognition_provider','prompt','temperature')}
    return result | {'fixed_rules':FIXED_RULES,'contract':'telephone-ai-v1','part_seconds':25}


def enqueue(db,workspace,call,request_id,user,profile_snapshot,*,historical=False,reprocess=False,expected_run=None,ack=False):
    # A call lock serializes two people/buttons, including first-time creation.
    record=db.execute(text('SELECT id FROM telephony_calls WHERE workspace_id=:w AND id=:c FOR UPDATE'),{'w':workspace,'c':call}).scalar_one_or_none()
    if record is None:raise AuthError(404,'Звонок недоступен.')
    repeated=db.execute(text('SELECT id,call_id FROM telephony_ai_runs WHERE workspace_id=:w AND request_id=:q'),{'w':workspace,'q':request_id}).mappings().one_or_none()
    if repeated:
        if repeated['call_id']!=call:raise AuthError(409,'Этот запрос относится к другому звонку.')
        return repeated['id']
    old=db.execute(text('SELECT r.id,r.phase FROM telephony_ai_links l JOIN telephony_ai_runs r ON r.workspace_id=l.workspace_id AND r.id=l.run_id WHERE l.workspace_id=:w AND l.call_id=:c'),{'w':workspace,'c':call}).mappings().one_or_none()
    if old:
        if not reprocess:return old['id']
        if old['phase'] not in TERMINAL or expected_run!=old['id'] or not ack:
            raise AuthError(409,'Для повторной обработки завершённого задания подтвердите возможный дополнительный расход.')
    run=uuid4()
    db.execute(text("INSERT INTO telephony_ai_runs(workspace_id,id,call_id,request_id,requested_by,profile,historical) VALUES(:w,:id,:c,:q,:u,CAST(:p AS jsonb),:old)"),
               {'w':workspace,'id':run,'c':call,'q':request_id,'u':user,'p':json.dumps(profile_snapshot),'old':historical})
    db.execute(text('INSERT INTO telephony_ai_links(workspace_id,call_id,run_id) VALUES(:w,:c,:r) ON CONFLICT(workspace_id,call_id) DO UPDATE SET run_id=EXCLUDED.run_id'),{'w':workspace,'c':call,'r':run})
    wake(db,workspace)
    return run


def wake(db,workspace):
    db.execute(text('INSERT INTO telephony_ai_routes(workspace_id) VALUES(:w) ON CONFLICT(workspace_id) DO UPDATE SET due_at=least(telephony_ai_routes.due_at,now())'),{'w':workspace})


class Store:
    def __init__(self,engine,clock=archive.now_utc):self.engine,self.clock=engine,clock

    @contextmanager
    def tenant(self,workspace,user=None):
        with self.engine.begin() as db:
            context(db,workspace_id=workspace,membership_admin='true',**({'user_id':user} if user else {}))
            yield db

    def owned(self,db,job):
        return db.execute(text('SELECT workspace_id FROM telephony_ai_routes WHERE workspace_id=:w AND lease_owner=:t AND lease_until>now() FOR UPDATE'),{'w':job['workspace'],'t':job['token']}).first() is not None

    def release(self,job,delay=1):
        with self.tenant(job['workspace']) as db:
            db.execute(text("UPDATE telephony_ai_routes SET lease_until=now(),lease_owner=NULL,run_id=NULL,due_at=now()+(:delay*interval '1 second') WHERE workspace_id=:w AND lease_owner=:t"),{'w':job['workspace'],'t':job['token'],'delay':max(1,min(86400,delay))})

    def claim(self):
        with self.engine.begin() as db:
            route=db.execute(text('SELECT workspace_id FROM telephony_ai_routes WHERE due_at<=now() AND lease_until<=now() ORDER BY due_at,workspace_id FOR UPDATE SKIP LOCKED LIMIT 1')).mappings().one_or_none()
            if not route:return None
            workspace,token=route['workspace_id'],uuid4()
            context(db,workspace_id=workspace,membership_admin='true')
            db.execute(text("UPDATE telephony_ai_runs r SET historical=true FROM telephony_calls c WHERE c.workspace_id=r.workspace_id AND c.id=r.call_id AND r.workspace_id=:w AND NOT r.historical AND r.phase NOT IN ('complete','error','needs_review') AND c.started_at<:cutoff"),{'w':workspace,'cutoff':archive.old_cutoff(self.clock())})
            automatic=db.execute(text('SELECT * FROM telephony_ai_settings WHERE workspace_id=:w AND auto_new'),{'w':workspace}).mappings().one_or_none()
            active_recent=db.execute(text("SELECT 1 FROM telephony_ai_runs WHERE workspace_id=:w AND NOT historical AND phase NOT IN ('complete','error','needs_review') LIMIT 1"),{'w':workspace}).first()
            if automatic and not active_recent:
                candidate=db.execute(text("SELECT c.id FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND s.connected AND c.started_at>=:after AND c.finished_at IS NOT NULL AND NOT c.missed AND c.talk_seconds>0 AND c.recording_ciphertext IS NOT NULL AND NOT EXISTS(SELECT 1 FROM telephony_ai_links l WHERE l.workspace_id=c.workspace_id AND l.call_id=c.id) ORDER BY c.started_at,c.id LIMIT 1"),{'w':workspace,'after':max(automatic['enabled_after'],archive.old_cutoff(self.clock()))}).mappings().one_or_none()
                if candidate:
                    ai=db.execute(text('SELECT * FROM ai_settings WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
                    enqueue(db,workspace,candidate['id'],uuid5(candidate['id'],'telephone-ai-initial'),automatic['configured_by'],snapshot(ai,workspace))
            control=db.execute(text('SELECT * FROM telephony_archive_control WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            historical=bool(control and control['enabled'] and archive.night(self.clock()))
            run=db.execute(text("SELECT r.* FROM telephony_ai_runs r JOIN telephony_ai_links l ON l.workspace_id=r.workspace_id AND l.run_id=r.id WHERE r.workspace_id=:w AND r.phase NOT IN ('complete','error','needs_review') AND (r.retry_at IS NULL OR r.retry_at<=now()) AND (NOT r.historical OR :historical) ORDER BY r.historical,r.created_at,r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED"),{'w':workspace,'historical':historical}).mappings().one_or_none()
            last_admission=max((control[key] for key in ('last_started_at','last_imported_at') if control[key]),default=None) if control else None
            source_ready=not control or control['retry_at'] is None or control['retry_at']<=self.clock()
            if not run and historical and source_ready and archive.due(self.clock(),last_admission)<=self.clock():
                db.execute(text("UPDATE telephony_ai_routes SET lease_owner=:t,run_id=NULL,lease_until=now()+interval '180 seconds' WHERE workspace_id=:w"),{'w':workspace,'t':token})
                return {'workspace':workspace,'token':token,'run':None,'archive':dict(control)}
            if not run:
                db.execute(text("UPDATE telephony_ai_routes SET due_at=now()+interval '15 seconds' WHERE workspace_id=:w"),{'w':workspace})
                return None
            run=dict(run)
            if run['historical'] and run['started_at'] is None:
                now=self.clock();next_at=archive.due(now,control['last_started_at'])
                if next_at>now:
                    db.execute(text('UPDATE telephony_ai_routes SET due_at=:next WHERE workspace_id=:w'),{'w':workspace,'next':next_at})
                    return None
                db.execute(text('UPDATE telephony_archive_control SET last_started_at=:now WHERE workspace_id=:w'),{'w':workspace,'now':now})
                db.execute(text('UPDATE telephony_ai_runs SET started_at=:now WHERE workspace_id=:w AND id=:id'),{'w':workspace,'id':run['id'],'now':now})
                run['started_at']=now
            # A receipt marked sent before POST is never automatically replayed.
            uncertain=db.execute(text("SELECT 1 FROM telephony_ai_operations WHERE workspace_id=:w AND run_id=:id AND status IN ('sent','unknown') LIMIT 1"),{'w':workspace,'id':run['id']}).first()
            if uncertain:
                db.execute(text("UPDATE telephony_ai_operations SET status='unknown' WHERE workspace_id=:w AND run_id=:id AND status='sent'"),{'w':workspace,'id':run['id']})
                db.execute(text("UPDATE telephony_ai_runs SET phase='needs_review',error_code='provider_result_unknown',version=version+1,updated_at=now() WHERE workspace_id=:w AND id=:id"),{'w':workspace,'id':run['id']})
                return None
            db.execute(text("UPDATE telephony_ai_routes SET lease_owner=:t,run_id=:run,lease_until=now()+interval '180 seconds' WHERE workspace_id=:w"),{'w':workspace,'t':token,'run':run['id']})
            return {'workspace':workspace,'token':token,'run':dict(run)}

    def update(self,job,**values):
        allowed={'phase','audio_hash','gateway_hash','channels','part','transcript','summary','error_code','retry_at','attempts'}
        if not values or set(values)-allowed:raise ValueError('Invalid AI state update')
        with self.tenant(job['workspace']) as db:
            if not self.owned(db,job):return False
            clause=','.join(f'{key}=:{key}' for key in values)
            db.execute(text(f'UPDATE telephony_ai_runs SET {clause},version=version+1,updated_at=now() WHERE workspace_id=:w AND id=:id'),values | {'w':job['workspace'],'id':job['run']['id']})
            if values.get('phase')=='complete':
                from center_messenger.phone_care import enqueue
                enqueue(db,job['workspace'],job['run']['call_id'],priority=0 if job['run']['historical'] else 2)
        job['run'].update(values)
        return True

    def operations(self,job,prefix):
        with self.tenant(job['workspace']) as db:
            return [dict(row) for row in db.execute(text("SELECT * FROM telephony_ai_operations WHERE workspace_id=:w AND run_id=:id AND operation LIKE :prefix AND status='complete' ORDER BY operation"),{'w':job['workspace'],'id':job['run']['id'],'prefix':prefix+'%'}).mappings()]

    def receipt(self,job,operation,input_hash,estimate,seconds=0):
        """Commit reservation BEFORE the provider call. None means no ownership."""
        with self.tenant(job['workspace'],job['run']['requested_by']) as db:
            if not self.owned(db,job):return None
            current=db.execute(text('SELECT * FROM telephony_ai_operations WHERE workspace_id=:w AND run_id=:id AND operation=:op FOR UPDATE'),{'w':job['workspace'],'id':job['run']['id'],'op':operation}).mappings().one_or_none()
            if current:
                if current['input_hash']!=input_hash:raise AuthError(409,'Входные данные задания изменились.')
                if current['status']!='complete':raise AuthError(409,'Результат предыдущего платного запроса требует проверки.')
                return dict(current)
            # Recheck archive pause/window immediately before ANY paid part.
            if job['run']['historical']:
                enabled=db.execute(text('SELECT enabled FROM telephony_archive_control WHERE workspace_id=:w'),{'w':job['workspace']}).scalar_one_or_none()
                if not enabled or not archive.night(self.clock()):return None
            allowed=db.execute(text("SELECT 1 FROM workspace_memberships m JOIN auth_users u ON u.id=m.user_id WHERE m.workspace_id=:w AND m.user_id=:u AND m.active AND u.active AND m.role IN ('owner','admin')"),{'w':job['workspace'],'u':job['run']['requested_by']}).first()
            if not allowed:raise AuthError(403,'Разрешение на обработку отозвано.')
            # Lock the shared budget row to serialize telephone reservations.
            limit=db.execute(text('SELECT monthly_limit_rub FROM ai_settings WHERE workspace_id=:w FOR UPDATE'),{'w':job['workspace']}).scalar_one_or_none()
            spent=db.execute(text("SELECT coalesce(sum(u.cost_rub),0) FROM ai_usage u JOIN workspaces w ON w.id=u.workspace_id WHERE u.workspace_id=:w AND u.created_at >= (date_trunc('month',now() AT TIME ZONE w.timezone) AT TIME ZONE w.timezone)"),{'w':job['workspace']}).scalar_one()
            reserved=db.execute(text("SELECT coalesce(sum(o.cost_rub),0) FROM telephony_ai_operations o JOIN workspaces w ON w.id=o.workspace_id WHERE o.workspace_id=:w AND o.status IN ('sent','unknown') AND o.started_at >= (date_trunc('month',now() AT TIME ZONE w.timezone) AT TIME ZONE w.timezone)"),{'w':job['workspace']}).scalar_one()
            if limit is not None and float(spent)+float(reserved)+estimate>float(limit):
                raise AuthError(429,'Месячный бюджет ИИ исчерпан.')
            db.execute(text("INSERT INTO telephony_ai_operations(workspace_id,run_id,operation,input_hash,status,cost_rub,audio_seconds) VALUES(:w,:id,:op,:hash,'sent',:cost,:seconds)"),{'w':job['workspace'],'id':job['run']['id'],'op':operation,'hash':input_hash,'cost':estimate,'seconds':seconds})
            return {'status':'sent','new':True}

    def complete(self,job,operation,value,*,cost,seconds=0,tokens=(0,0),kind='call_asr',usable=True):
        with self.tenant(job['workspace']) as db:
            if not self.owned(db,job):return False
            changed=db.execute(text("UPDATE telephony_ai_operations SET status='complete',result=:result,usable=:usable,cost_rub=:cost,audio_seconds=:seconds,input_tokens=:ti,output_tokens=:to,finished_at=now() WHERE workspace_id=:w AND run_id=:id AND operation=:op AND status='sent' RETURNING operation"),{'w':job['workspace'],'id':job['run']['id'],'op':operation,'result':value,'cost':cost,'seconds':seconds,'ti':tokens[0],'to':tokens[1],'usable':usable}).scalar_one_or_none()
            if changed is None:return False
            db.execute(text("INSERT INTO ai_usage(workspace_id,id,user_id,agent_id,kind,audio_seconds,input_tokens,output_tokens,cost_rub,outcome) VALUES(:w,:usage,:u,:agent,:kind,:seconds,:ti,:to,:cost,:outcome)"),{'w':job['workspace'],'usage':uuid4(),'u':job['run']['requested_by'],'agent':UUID(job['run']['profile']['id']),'kind':kind,'seconds':seconds,'ti':tokens[0],'to':tokens[1],'cost':cost,'outcome':'ok' if usable else 'error'})
            if not usable:
                db.execute(text("UPDATE telephony_ai_runs SET phase='needs_review',error_code='provider_result_incomplete',version=version+1,updated_at=now() WHERE workspace_id=:w AND id=:id"),{'w':job['workspace'],'id':job['run']['id']})
            return True

    def uncertain(self,job,code='provider_result_unknown'):
        with self.tenant(job['workspace']) as db:
            if not self.owned(db,job):return
            db.execute(text("UPDATE telephony_ai_operations SET status='unknown' WHERE workspace_id=:w AND run_id=:id AND status='sent'"),{'w':job['workspace'],'id':job['run']['id']})
            db.execute(text("UPDATE telephony_ai_runs SET phase='needs_review',error_code=:code,version=version+1,updated_at=now() WHERE workspace_id=:w AND id=:id"),{'w':job['workspace'],'id':job['run']['id'],'code':code})

    def pause_automatic(self,job,code):
        with self.tenant(job['workspace']) as db:
            if not self.owned(db,job):return
            db.execute(text("UPDATE telephony_archive_control SET enabled=false,last_error_code=:code,version=version+1,updated_at=now() WHERE workspace_id=:w AND enabled"),{'w':job['workspace'],'code':code})
            db.execute(text('UPDATE telephony_ai_settings SET auto_new=false,version=version+1,updated_at=now() WHERE workspace_id=:w AND auto_new'),{'w':job['workspace']})

    def reuse_asr(self,job,operation,input_hash):
        with self.tenant(job['workspace']) as db:
            if not self.owned(db,job):return None
            previous=db.execute(text("SELECT o.result,o.audio_seconds FROM telephony_ai_operations o JOIN telephony_ai_runs r ON r.workspace_id=o.workspace_id AND r.id=o.run_id WHERE o.workspace_id=:w AND r.call_id=:call AND r.id<>:id AND r.audio_hash=:audio AND r.profile->>'recognition_provider'=:provider AND r.profile->>'contract'=:contract AND o.operation=:op AND o.input_hash=:hash AND o.status='complete' AND o.usable ORDER BY o.finished_at DESC LIMIT 1"),{'w':job['workspace'],'call':job['run']['call_id'],'id':job['run']['id'],'audio':job['run']['audio_hash'],'provider':job['run']['profile']['recognition_provider'],'contract':job['run']['profile']['contract'],'op':operation,'hash':input_hash}).mappings().one_or_none()
            if previous is None:return None
            db.execute(text("INSERT INTO telephony_ai_operations(workspace_id,run_id,operation,input_hash,status,result,audio_seconds,finished_at) VALUES(:w,:id,:op,:hash,'complete',:result,:seconds,now()) ON CONFLICT DO NOTHING"),{'w':job['workspace'],'id':job['run']['id'],'op':operation,'hash':input_hash,'result':previous['result'],'seconds':previous['audio_seconds']})
            return dict(previous)
