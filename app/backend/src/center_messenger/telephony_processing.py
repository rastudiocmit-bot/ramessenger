"""Authorized local controls/read models for telephone-only AI and night archive."""
from uuid import UUID

from sqlalchemy import text
from center_messenger.accounts import audit
from center_messenger.auth_security import AuthError
from center_messenger.telephony_agent import TelephonyAgent
from center_messenger.telephony_ai_store import snapshot,enqueue,wake
from center_messenger import telephony_archive as archive


def views(db,workspace,calls):
    if not calls:return {}
    rows=db.execute(text("""SELECT l.call_id,l.summary_override,l.edit_version,
      r.id,r.phase,r.error_code,r.retry_at,r.historical,r.version,r.summary,
      r.profile->>'name' AS agent_name,r.profile->>'version' AS agent_version,
      r.transcript IS NOT NULL AS full_transcript,
      EXISTS(SELECT 1 FROM telephony_ai_operations o WHERE o.workspace_id=r.workspace_id AND o.run_id=r.id AND o.operation LIKE 'asr:%' AND o.status='complete' AND o.usable) AS has_parts,
      previous.id AS previous_id,previous.summary AS previous_summary,previous.has_transcript AS previous_transcript,
      coalesce(q.lease_until>now(),false) AS leased,a.enabled AS archive_enabled
      FROM telephony_ai_links l JOIN telephony_ai_runs r ON r.workspace_id=l.workspace_id AND r.id=l.run_id
      LEFT JOIN telephony_ai_routes q ON q.workspace_id=l.workspace_id AND q.run_id=r.id
      LEFT JOIN telephony_archive_control a ON a.workspace_id=l.workspace_id
      LEFT JOIN LATERAL(SELECT id,summary,transcript IS NOT NULL AS has_transcript FROM telephony_ai_runs old
        WHERE old.workspace_id=r.workspace_id AND old.call_id=r.call_id AND old.id<>r.id AND old.transcript IS NOT NULL
        ORDER BY (old.summary IS NOT NULL) DESC,old.created_at DESC LIMIT 1) previous ON true
      WHERE l.workspace_id=:w AND l.call_id=ANY(CAST(:calls AS uuid[]))"""),{'w':workspace,'calls':calls}).mappings()
    result={}
    for row in rows:
        summary=row['summary_override'] if row['summary_override'] is not None else row['summary'] or row['previous_summary']
        source='manual' if row['summary_override'] is not None else 'current' if row['summary'] else 'previous' if row['previous_summary'] else None
        transcript_run=row['id'] if row['full_transcript'] else row['previous_id'] if row['previous_transcript'] else row['id'] if row['has_parts'] else None
        state={key:row[key] for key in ('phase','error_code','retry_at','historical','version','agent_name','agent_version','edit_version')}
        state|={'run_id':str(row['id']),'summary':summary,'summary_source':source,
                'asr_complete':bool(row['full_transcript']),
                'transcript_run_id':str(transcript_run) if transcript_run else None,
                'processing':bool(row['leased'] and row['phase'] in ('fetching','recognizing','summarizing')),
                'waiting_for_night':bool(row['historical'] and not archive.night(archive.now_utc())),
                'archive_paused':bool(row['historical'] and not row['archive_enabled'])}
        result[row['call_id']]=state
    return result


class Processing:
    def __init__(self,calls):
        self.calls,self.accounts,self.alfa=calls,calls.accounts,calls.alfa
        self.agent=TelephonyAgent(calls.accounts,calls.vault)

    def ready(self,db,workspace):
        saved=snapshot(self.agent._settings(db,workspace),workspace)
        from center_messenger.ai_voice import provider_of
        selected={saved['recognition_provider'],provider_of(saved['model'])}
        credentials=self.agent._credentials(db,workspace,yandex=False,selected=selected)
        if any(not credentials.get(provider) for provider in selected):
            raise AuthError(409,'Подключите выбранные шлюзы распознавания и описания в «ИИ-шлюзах».')
        return saved

    def settings(self,actor,workspace):
        with self.accounts.transaction(actor,snapshot=True) as db:
            self.accounts._workspace(db,actor,workspace,manage=True)
            row=db.execute(text('SELECT * FROM telephony_ai_settings WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            control=db.execute(text('SELECT * FROM telephony_archive_control WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            counts={r['phase']:r['n'] for r in db.execute(text('SELECT r.phase,count(*) AS n FROM telephony_ai_runs r JOIN telephony_ai_links l ON l.workspace_id=r.workspace_id AND l.run_id=r.id WHERE r.workspace_id=:w GROUP BY r.phase'),{'w':workspace}).mappings()}
            nightly=archive.status(control,archive.now_utc()) | {'version':control['version'] if control else 0,
                'imported':control['imported'] if control else 0,'checked_from':control['cursor_start'] if control else archive.START,
                'error_code':control['last_error_code'] if control else None,'retry_at':control['retry_at'] if control else None}
            return {'version':row['version'] if row else 0,'auto_new':bool(row and row['auto_new']),
                    'enabled_after':row['enabled_after'] if row else None,'archive':nightly,'counts':counts}

    def configure(self,actor,workspace,data):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db,actor,workspace,manage=True)
            wake(db,workspace)  # same lock order as a worker: route -> settings
            row=db.execute(text('SELECT * FROM telephony_ai_settings WHERE workspace_id=:w FOR UPDATE'),{'w':workspace}).mappings().one_or_none()
            if (row['version'] if row else 0)!=data['expected_version']:raise AuthError(409,'Настройки обработки изменились. Обновите состояние.')
            if data['auto_new']:self.ready(db,workspace)
            db.execute(text("INSERT INTO telephony_ai_settings(workspace_id,auto_new,enabled_after,configured_by) VALUES(:w,:enabled,CASE WHEN :enabled THEN now() END,:u) ON CONFLICT(workspace_id) DO UPDATE SET auto_new=EXCLUDED.auto_new,enabled_after=CASE WHEN EXCLUDED.auto_new AND NOT telephony_ai_settings.auto_new THEN now() ELSE telephony_ai_settings.enabled_after END,configured_by=:u,version=telephony_ai_settings.version+1,updated_at=now()"),{'w':workspace,'enabled':data['auto_new'],'u':actor.user_id})
            audit(db,'telephony_processing_configured',user_id=actor.user_id,workspace_id=workspace)
        return self.settings(actor,workspace)

    def configure_archive(self,actor,workspace,data):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db,actor,workspace,manage=True)
            wake(db,workspace)
            old=db.execute(text('SELECT * FROM telephony_archive_control WHERE workspace_id=:w FOR UPDATE'),{'w':workspace}).mappings().one_or_none()
            if (old['version'] if old else 0)!=data['expected_version']:raise AuthError(409,'Состояние ночного архива изменилось. Обновите его.')
            if data['enabled']:
                self.ready(db,workspace)
                connected=db.execute(text('SELECT connected FROM telephony_state WHERE workspace_id=:w'),{'w':workspace}).scalar_one_or_none()
                if not connected:raise AuthError(409,'Сначала подключите и проверьте Новофон.')
            db.execute(text('INSERT INTO telephony_archive_control(workspace_id,enabled,started_by,cursor_start) VALUES(:w,:enabled,:u,:start) ON CONFLICT(workspace_id) DO UPDATE SET enabled=EXCLUDED.enabled,started_by=:u,version=telephony_archive_control.version+1,last_error_code=NULL,retry_at=NULL,updated_at=now()'),{'w':workspace,'enabled':data['enabled'],'u':actor.user_id,'start':archive.START})
            audit(db,'telephony_archive_started' if data['enabled'] else 'telephony_archive_paused',user_id=actor.user_id,workspace_id=workspace)
        return self.settings(actor,workspace)

    def authorized_call(self,db,workspace,subject,call,contact=False):
        phones=self.calls.contact_phones(db,workspace,subject) if contact else self.calls.phones(db,workspace,subject)
        row=db.execute(text('SELECT c.* FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND c.id=:call AND c.phone=ANY(CAST(:phones AS text[]))'),{'w':workspace,'call':call,'phones':phones}).mappings().one_or_none()
        if row is None:raise AuthError(404,'Звонок недоступен в этой карточке.')
        return row

    def request(self,actor,workspace,subject,call,data,*,contact=False):
        transaction=self.alfa._transaction if contact else self.alfa.chat_transaction
        with transaction(actor,workspace) as db:
            self.accounts._workspace(db,actor,workspace,manage=True)
            wake(db,workspace)
            row=self.authorized_call(db,workspace,subject,call,contact)
            historical=row['started_at']<archive.old_cutoff(archive.now_utc())
            if historical:
                if row['started_at']<archive.START:raise AuthError(422,'Звонок раньше начала архивного периода 31.10.2020.')
                enabled=db.execute(text('SELECT enabled FROM telephony_archive_control WHERE workspace_id=:w'),{'w':workspace}).scalar_one_or_none()
                if not enabled:raise AuthError(409,'Запустите ночной архив в настройках телефонного агента. Старые звонки обрабатываются с 00:00 до 08:00.')
            if row['missed'] or not row['talk_seconds']:raise AuthError(409,'В этом звонке нет состоявшегося разговора.')
            saved=self.ready(db,workspace)
            run=enqueue(db,workspace,call,data['request_id'],actor.user_id,saved,historical=historical,
                reprocess=data['reprocess'],expected_run=data.get('expected_run'),ack=data['acknowledge_cost'])
            audit(db,'telephony_ai_requested',user_id=actor.user_id,workspace_id=workspace,subject_id=str(call))
            return views(db,workspace,[call]).get(call) | {'run_id':str(run)}

    def transcript(self,actor,workspace,subject,call,run=None,after='',*,contact=False):
        transaction=self.alfa._transaction if contact else self.alfa.chat_transaction
        with transaction(actor,workspace,snapshot=True) as db:
            self.authorized_call(db,workspace,subject,call,contact)
            if run is None:
                current=views(db,workspace,[call]).get(call)
                run=UUID(current['transcript_run_id']) if current and current['transcript_run_id'] else None
            found=db.execute(text('SELECT id,phase,transcript IS NOT NULL AS asr_complete FROM telephony_ai_runs WHERE workspace_id=:w AND call_id=:call AND id=:run'),{'w':workspace,'call':call,'run':run}).mappings().one_or_none()
            if not found:raise AuthError(404,'Расшифровка пока недоступна.')
            rows=list(db.execute(text("SELECT operation,result,audio_seconds FROM telephony_ai_operations WHERE workspace_id=:w AND run_id=:run AND operation LIKE 'asr:%' AND operation>:after AND status='complete' AND usable ORDER BY operation LIMIT 101"),{'w':workspace,'run':run,'after':after}).mappings())
            segments=[]
            for row in rows[:100]:
                _,part,channel=row['operation'].split(':');start=int(part)*25
                segments.append({'channel':int(channel)+1,'start':start,'end':start+float(row['audio_seconds']),'text':row['result']})
            return {'run_id':str(run),'complete':found['asr_complete'],'segments':segments,
                    'next':rows[99]['operation'] if len(rows)>100 else None}

    def edit(self,actor,workspace,subject,call,data,*,contact=False):
        transaction=self.alfa._transaction if contact else self.alfa.chat_transaction
        with transaction(actor,workspace) as db:
            member=self.accounts._workspace_chat(db,actor,workspace)
            if member['role']=='observer':raise AuthError(403,'Наблюдатель не может редактировать описание.')
            self.authorized_call(db,workspace,subject,call,contact)
            changed=db.execute(text('UPDATE telephony_ai_links SET summary_override=:text,edited_by=:u,edited_at=now(),edit_version=edit_version+1 WHERE workspace_id=:w AND call_id=:call AND edit_version=:version RETURNING call_id'),{'w':workspace,'call':call,'version':data['expected_version'],'text':data['summary'],'u':actor.user_id}).scalar_one_or_none()
            if changed is None:raise AuthError(409,'Описание изменилось. Обновите звонок перед сохранением.')
            audit(db,'telephony_ai_summary_edited',user_id=actor.user_id,workspace_id=workspace,subject_id=str(call))
            return views(db,workspace,[call]).get(call)
