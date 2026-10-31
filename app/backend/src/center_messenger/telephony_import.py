"""One leased page per pass, atomic call/cursor commits, no MAX writes."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from center_messenger.accounts import context
from center_messenger.novofon_client import AuditError, Client, identifier
from center_messenger.telephony_model import FIELDS, normalize

LOG = logging.getLogger(__name__)
LIMIT = 100


def window(start, end):
    return {'from':start.isoformat(),'till':end.isoformat(),'offset':0}


def page_complete(rows, metadata, offset):
    total = metadata.get('total_items')
    if len(rows)>LIMIT or (total is not None and (offset+len(rows)>total or not rows and offset<total)):
        raise AuditError('calls_page_incomplete')
    return offset+len(rows)>=total if total is not None else len(rows)<LIMIT


UPSERT = """INSERT INTO telephony_calls(workspace_id,id,provider,account_id,source_id,connection_id,
 phone,direction,started_at,finished_at,talk_seconds,total_seconds,missed,employees,
 recording_ciphertext,recording_fingerprint,fingerprint)
 VALUES(:w,:id,'novofon',:account_id,:source_id,:connection_id,:phone,:direction,:started_at,
 :finished_at,:talk_seconds,:total_seconds,:missed,CAST(:employees AS jsonb),:recording,:record_hash,:fingerprint)
 ON CONFLICT(workspace_id,provider,account_id,source_id) DO UPDATE SET
 phone=EXCLUDED.phone,direction=EXCLUDED.direction,started_at=EXCLUDED.started_at,
 finished_at=EXCLUDED.finished_at,talk_seconds=EXCLUDED.talk_seconds,total_seconds=EXCLUDED.total_seconds,
 missed=EXCLUDED.missed,employees=EXCLUDED.employees,connection_id=EXCLUDED.connection_id,
 recording_ciphertext=coalesce(EXCLUDED.recording_ciphertext,telephony_calls.recording_ciphertext),
 recording_fingerprint=coalesce(EXCLUDED.recording_fingerprint,telephony_calls.recording_fingerprint),
 fingerprint=EXCLUDED.fingerprint,version=telephony_calls.version+1,updated_at=now()
 WHERE telephony_calls.fingerprint<>EXCLUDED.fingerprint OR
 (EXCLUDED.recording_fingerprint IS NOT NULL AND EXCLUDED.recording_fingerprint IS DISTINCT FROM telephony_calls.recording_fingerprint)
 RETURNING id"""


class TelephonyImport:
    def __init__(self, accounts, vault, client_factory=Client, clock=lambda:datetime.now(timezone.utc)):
        self.accounts,self.vault,self.client_factory,self.clock = accounts,vault,client_factory,clock

    @staticmethod
    def ensure(db, workspace):
        db.execute(text('INSERT INTO telephony_state(workspace_id) VALUES(:w) ON CONFLICT DO NOTHING'),{'w':workspace})
        db.execute(text("INSERT INTO telephony_routes(workspace_id) VALUES(:w) ON CONFLICT(workspace_id) DO UPDATE SET due_at=least(telephony_routes.due_at,now())"),{'w':workspace})

    def claim(self):
        with self.accounts.engine.begin() as db:
            route = db.execute(text("SELECT * FROM telephony_routes WHERE due_at<=now() AND lease_until<=now() ORDER BY due_at,workspace_id FOR UPDATE SKIP LOCKED LIMIT 1")).mappings().one_or_none()
            if not route:
                return None
            workspace,token = route['workspace_id'],uuid4()
            context(db,workspace_id=workspace,membership_admin='true')
            state = db.execute(text('SELECT * FROM telephony_state WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            settings = db.execute(text('SELECT * FROM novofon_settings WHERE workspace_id=:w'),{'w':workspace}).mappings().one_or_none()
            if not state or not state['enabled'] or not settings or not settings['key_ciphertext']:
                db.execute(text("UPDATE telephony_routes SET due_at=now()+interval '1 hour' WHERE workspace_id=:w"),{'w':workspace})
                return None
            db.execute(text("UPDATE telephony_routes SET lease_owner=:t,lease_until=now()+interval '60 seconds' WHERE workspace_id=:w"),{'w':workspace,'t':token})
            return {'workspace':workspace,'token':token,'settings':dict(settings),'state':dict(state)}

    def run_next(self):
        job = self.claim()
        if not job:
            return False
        try:
            self.process(job)
        except AuditError as error:
            self.retry(job,error.code,error.details)
        except Exception as error:
            LOG.warning('Telephony import interrupted exception=%s',type(error).__name__)
            self.retry(job,'import_failed',{})
        return True

    def process(self, job):
        workspace,settings,state,now = job['workspace'],job['settings'],job['state'],self.clock()
        key = self.vault.open(workspace,settings['connection_id'],settings['key_ciphertext'],purpose='novofon')['api_key']
        client = self.client_factory(key)
        accounts,_ = client.read('get.account')
        if len(accounts)!=1 or not identifier(accounts[0].get('app_id')):
            raise AuditError('account_schema')
        account,zone = identifier(accounts[0]['app_id']),accounts[0].get('timezone')
        try:
            tz=ZoneInfo(zone)
        except (TypeError,ValueError,ZoneInfoNotFoundError):
            raise AuditError('account_timezone_invalid') from None
        reset = state['settings_version']!=settings['version'] or state['account_id']!=account or state['timezone']!=zone
        recent = {} if reset else dict(state['recent'])
        archive = {} if reset else dict(state['archive'])
        archive_done = False if reset else state['archive_done']
        if not recent:
            recent=window(now-timedelta(days=1),now)
        if not archive and not archive_done:
            start=now-timedelta(days=state['history_days'])
            cutoff=now-timedelta(days=1)
            archive=window(start,min(start+timedelta(days=1),cutoff)) | {'cutoff':cutoff.isoformat()}
            archive_done=start>=cutoff
        stream = 'recent' if reset or state['recent_due']<=now else 'archive'
        if stream=='archive' and archive_done:
            self.commit(job,account,zone,recent,archive,archive_done,[],state['recent_due'],60,False)
            return
        cursor=recent if stream=='recent' else archive
        start,end = datetime.fromisoformat(cursor['from']),datetime.fromisoformat(cursor['till'])
        if cursor['offset']>100000:
            raise AuditError('calls_window_too_large')
        rows,meta=client.read('get.calls_report',limit=LIMIT,offset=cursor['offset'],
            date_from=start.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S'),
            date_till=end.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S'),
            sort=[{'field':'start_time','order':'asc'}],fields=FIELDS,include_ongoing_calls=False)
        complete=page_complete(rows,meta,cursor['offset'])
        calls=[normalize(row,workspace=workspace,account=account,connection=settings['connection_id'],zone=zone) for row in rows]
        if any(not start<=call['started_at']<=end for call,record in calls):
            raise AuditError('calls_outside_window')
        if len({call['source_id'] for call,record in calls})!=len(calls):
            raise AuditError('calls_duplicate_page')
        page_hash=hashlib.sha256(json.dumps([call['source_id'] for call,record in calls]).encode()).hexdigest()
        if rows and cursor.get('page_hash')==page_hash:
            raise AuditError('calls_repeated_page')
        next_cursor=cursor | {'offset':cursor['offset']+len(rows),'page_hash':page_hash}
        recent_due=state['recent_due']
        if stream=='recent':
            recent=next_cursor
            recent_due=now+timedelta(seconds=state['poll_seconds']) if complete else now
            if complete:
                recent=window(now-timedelta(days=1),now)
        else:
            archive=next_cursor
            if complete:
                cutoff=datetime.fromisoformat(archive['cutoff'])
                archive_done=end>=cutoff
                if not archive_done:
                    archive=window(end,min(end+timedelta(days=1),cutoff)) | {'cutoff':cutoff.isoformat()}
        delay=max(1,(recent_due-now).total_seconds()) if archive_done else 1
        self.commit(job,account,zone,recent,archive,archive_done,calls,recent_due,delay,stream=='recent' and complete)

    def locked(self, db, job):
        route=db.execute(text('SELECT lease_owner,lease_until>now() AS live FROM telephony_routes WHERE workspace_id=:w FOR UPDATE'),{'w':job['workspace']}).mappings().one_or_none()
        context(db,workspace_id=job['workspace'],membership_admin='true')
        state=db.execute(text('SELECT enabled,version FROM telephony_state WHERE workspace_id=:w FOR UPDATE'),{'w':job['workspace']}).mappings().one_or_none()
        settings=db.execute(text('SELECT version,key_ciphertext FROM novofon_settings WHERE workspace_id=:w'),{'w':job['workspace']}).mappings().one_or_none()
        owned=bool(route and route['lease_owner']==job['token'] and route['live'])
        valid=bool(owned and settings and settings['key_ciphertext'] and settings['version']==job['settings']['version'] and state and state['enabled'] and state['version']==job['state']['version'])
        return owned,valid

    def release(self,db,job,delay):
        db.execute(text("UPDATE telephony_routes SET lease_owner=NULL,lease_until='2000-01-01T00:00:00Z',due_at=now()+(:delay*interval '1 second') WHERE workspace_id=:w AND lease_owner=:t"),{'w':job['workspace'],'t':job['token'],'delay':delay})

    def commit(self,job,account,zone,recent,archive,archive_done,calls,recent_due,delay,successful_read):
        with self.accounts.engine.begin() as db:
            owned,valid=self.locked(db,job)
            if not valid:
                if owned:self.release(db,job,1)
                return
            changed=0
            for call,record in calls:
                params=call | {'w':job['workspace'],'employees':json.dumps(call['employees']),
                    'recording':self.vault.seal(job['workspace'],call['connection_id'],{'url':record},purpose='novofon') if record else None,
                    'record_hash':hashlib.sha256(record.encode()).hexdigest() if record else None}
                changed+=len(db.execute(text(UPSERT),params).all())
            db.execute(text("UPDATE telephony_state SET connected=true,settings_version=:v,account_id=:a,timezone=:z,recent=CAST(:r AS jsonb),archive=CAST(:b AS jsonb),archive_done=:done,recent_due=:due,last_error_code=NULL,retry_at=NULL,attempts=0,imported=imported+:n,last_sync_at=CASE WHEN :success THEN now() ELSE last_sync_at END,updated_at=now() WHERE workspace_id=:w"),
                {'w':job['workspace'],'v':job['settings']['version'],'a':account,'z':zone,'r':json.dumps(recent),'b':json.dumps(archive),'done':archive_done,'due':recent_due,'n':changed,'success':successful_read})
            self.release(db,job,delay)

    def retry(self,job,code,details):
        delay=min(1800,30*2**min(job['state']['attempts'],6))
        mnemonic=details.get('mnemonic')
        if mnemonic in {'access_token_invalid','access_token_expired','access_token_blocked','forbidden','permission_denied','ip_not_whitelisted','account_inactive'}:
            delay=3600
        elif code=='quota_exhausted' or mnemonic=='limit_exceeded':
            delay=max(delay,60)
        with self.accounts.engine.begin() as db:
            owned,valid=self.locked(db,job)
            if valid:
                db.execute(text("UPDATE telephony_state SET last_error_code=:e,attempts=attempts+1,retry_at=now()+(:d*interval '1 second'),updated_at=now() WHERE workspace_id=:w"),{'w':job['workspace'],'e':mnemonic or code,'d':delay})
            if owned:self.release(db,job,delay if valid else 1)
