"""One historical call per pass, only after the night/rate scheduler admits it."""
from datetime import datetime, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo
from uuid import uuid4

from sqlalchemy import text
from center_messenger import telephony_archive as schedule
from center_messenger.telephony_ai_store import enqueue, snapshot
from center_messenger.telephony_import import UPSERT
from center_messenger.telephony_model import FIELDS, normalize
from center_messenger.novofon_budget import client_factory
from center_messenger.auth_security import AuthError


class ArchiveImport:
    def __init__(self,store,vault):
        self.store,self.vault=store,vault
        self.client=client_factory(store.engine)

    def run(self,job):
        workspace,control,now=job['workspace'],job['archive'],self.store.clock()
        if not schedule.night(now) or not control['enabled']:return
        with self.store.tenant(workspace) as db:
            row=db.execute(text('SELECT n.*,s.account_id,s.timezone,s.connected FROM novofon_settings n JOIN telephony_state s USING(workspace_id) WHERE n.workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            ai=db.execute(text('SELECT * FROM ai_settings WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            agent=snapshot(ai,workspace)
        if not row or not row['connected'] or not row['key_ciphertext']:
            raise AuthError(409,'Архив ожидает подключения Новофона.')
        row=dict(row)
        if control['account_id'] and control['account_id']!=row['account_id']:
            raise AuthError(409,'Источник архива изменился; требуется новый запуск.')
        start=control['cursor_start']
        end=control['cursor_end'] or min(start+timedelta(days=30),schedule.old_cutoff(now))
        if start>=end:
            with self.store.tenant(workspace) as db:
                if self.store.owned(db,job):
                    db.execute(text("UPDATE telephony_archive_control SET caught_up_at=:now,retry_at=:now+interval '1 hour' WHERE workspace_id=:w"),{'w':workspace,'now':now})
            return
        if control['page_offset']>100000:
            # Never silently jump over an unrepresentable page. An unusually
            # dense 30-day window needs a separately reviewed finer split.
            raise AuthError(409,'Достигнут предел страницы архива. Требуется уменьшить период чтения.')
        token=self.vault.open(workspace,row['connection_id'],row['key_ciphertext'],purpose='novofon')['api_key']
        client=self.client(token)
        client.budget.bind_account(row['account_id'])
        zone=ZoneInfo(row['timezone'])
        records,metadata=client.read('get.calls_report',limit=1,offset=control['page_offset'],
            date_from=start.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S'),
            date_till=end.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S'),
            sort=[{'field':'start_time','order':'asc'}],fields=FIELDS,include_ongoing_calls=False)
        if len(records)>1:raise AuthError(502,'Некорректная страница архива.')
        total=metadata.get('total_items')
        if total is not None and (type(total) is not int or total<0 or control['page_offset']+len(records)>total
                                  or not records and control['page_offset']<total):
            raise AuthError(502,'Архив Новофона изменился во время чтения.')
        normalized=[normalize(record,workspace=workspace,account=row['account_id'],connection=row['connection_id'],zone=row['timezone']) for record in records]
        if any(not start<=call['started_at']<=end for call,url in normalized):
            raise AuthError(502,'Новофон вернул звонок вне периода архива.')
        with self.store.tenant(workspace) as db:
            if not self.store.owned(db,job):return
            current=db.execute(text('SELECT * FROM telephony_archive_control WHERE workspace_id=:w FOR UPDATE'),{'w':workspace}).mappings().one()
            version=db.execute(text('SELECT version FROM novofon_settings WHERE workspace_id=:w'),{'w':workspace}).scalar_one()
            if (not current['enabled'] or current['version']!=control['version'] or version!=row['version']
                    or not schedule.night(self.store.clock())):return
            added=0
            for call,url in normalized:
                cipher=self.vault.seal_recording(workspace,row['connection_id'],call['id'],url) if url else None
                db.execute(text(UPSERT),call | {'w':workspace,'employees':json.dumps(call['employees']),
                    'recording':cipher,'record_hash':hashlib.sha256(url.encode()).hexdigest() if url else None})
                seen=db.execute(text('INSERT INTO telephony_archive_seen(workspace_id,call_id) VALUES(:w,:call) ON CONFLICT DO NOTHING RETURNING call_id'),{'w':workspace,'call':call['id']}).scalar_one_or_none()
                added+=int(seen is not None)
                if schedule.eligible(call['started_at'],self.store.clock()) and not call['missed'] and call['talk_seconds']:
                    enqueue(db,workspace,call['id'],uuid4(),current['started_by'],agent,historical=True)
                # Separate durable clocks govern imported calls and first AI
                # starts. Resuming a part never admits a second whole call.
                db.execute(text('UPDATE telephony_archive_control SET last_imported_at=:now WHERE workspace_id=:w'),{'w':workspace,'now':self.store.clock()})
            offset=control['page_offset']+len(records)
            complete=not records or total is not None and offset>=total
            db.execute(text('UPDATE telephony_archive_control SET cursor_start=:start,cursor_end=:end,page_offset=:offset,account_id=:account,last_fetch_at=:now,imported=imported+:n,last_error_code=NULL,retry_at=NULL,updated_at=now() WHERE workspace_id=:w'),
                {'w':workspace,'start':end if complete else start,'end':None if complete else end,
                 'offset':0 if complete else offset,'account':row['account_id'],'now':now,'n':added})
