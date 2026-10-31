"""Authorized local call views; a matching phone never changes MAX identity."""
import hashlib
import json
from uuid import UUID

from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.auth_security import AuthError
from center_messenger.crm_api import DirectoryReader
from center_messenger.crm_directory import phone_number
from center_messenger import card_preference
from center_messenger.telephony_audio import WorkerAudio
from center_messenger.telephony_import import TelephonyImport
from center_messenger.telephony_model import call_view


class TelephonyCalls:
    def __init__(self,accounts,vault,alfa,chats,config):
        self.accounts,self.vault,self.alfa,self.chats=accounts,vault,alfa,chats
        self.audio=WorkerAudio(config)

    def credentials_changed(self,db,workspace,configured):
        if configured:TelephonyImport.ensure(db,workspace)
        db.execute(text('UPDATE telephony_state SET connected=false,settings_version=NULL WHERE workspace_id=:w'),{'w':workspace})

    def settings(self,actor,workspace):
        with self.accounts.transaction(actor,snapshot=True) as db:
            self.accounts._workspace(db,actor,workspace,manage=True)
            row=db.execute(text("SELECT s.*,r.lease_until>now() AS running,r.due_at FROM telephony_state s LEFT JOIN telephony_routes r USING(workspace_id) WHERE s.workspace_id=:w"),{'w':workspace}).mappings().one_or_none()
            if not row:return {'enabled':True,'history_days':7,'poll_seconds':60,'version':0,'running':False,'call_count':0}
            count=db.execute(text('SELECT count(*) FROM telephony_calls WHERE workspace_id=:w AND account_id=:a'),{'w':workspace,'a':row['account_id']}).scalar_one()
            return {key:row[key] for key in ('enabled','history_days','poll_seconds','version','running','last_sync_at','last_error_code','retry_at','archive_done')} | {'call_count':count,'next_at':row['due_at']}

    def configure(self,actor,workspace,data):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db,actor,workspace,manage=True)
            configured=db.execute(text('SELECT key_ciphertext IS NOT NULL FROM novofon_settings WHERE workspace_id=:w'),{'w':workspace}).scalar_one_or_none()
            if not configured:raise AuthError(409,'Сначала сохраните реквизиты Новофона.')
            db.execute(text('SELECT workspace_id FROM telephony_routes WHERE workspace_id=:w FOR UPDATE'),{'w':workspace})
            row=db.execute(text('SELECT version FROM telephony_state WHERE workspace_id=:w FOR UPDATE'),{'w':workspace}).scalar_one_or_none()
            if (row or 0)!=data['expected_version']:raise AuthError(409,'Настройки загрузчика изменились. Обновите состояние.')
            if row is None:
                TelephonyImport.ensure(db,workspace)
            db.execute(text("UPDATE telephony_state SET enabled=:enabled,history_days=:history_days,poll_seconds=:poll_seconds,version=version+1,settings_version=NULL,retry_at=NULL,last_error_code=NULL,updated_at=now() WHERE workspace_id=:w"),data | {'w':workspace})
            db.execute(text('UPDATE telephony_routes SET due_at=now() WHERE workspace_id=:w'),{'w':workspace})
            audit(db,'telephony_loader_updated',user_id=actor.user_id,workspace_id=workspace)
        return self.settings(actor,workspace)

    def phones(self,db,workspace,conversation):
        row=self.chats._conversation(db,workspace,conversation,cached=True)
        selected=row.get('card_selection') or {}
        contacts=selected.get('contacts') or [row.get('family_contact_id') or row.get('contact_id')]
        contacts=[UUID(str(value)) for value in contacts if value]
        snapshot=DirectoryReader._published(db,workspace)
        if not snapshot or not contacts:return []
        rows=db.execute(text('SELECT phones FROM alfa_directory WHERE workspace_id=:w AND run_id=:r AND contact_id=ANY(CAST(:ids AS uuid[]))'),{'w':workspace,'r':snapshot['run'],'ids':contacts}).mappings().all()
        return sorted({number for item in rows for raw in item['phones'] if (number:=phone_number(raw))})

    def contact_phones(self,db,workspace,contact):
        snapshot=DirectoryReader._published(db,workspace)
        if not snapshot:raise AuthError(404,'Контакт недоступен.')
        selection=card_preference.selected(db,workspace,snapshot,None,contact)
        ids=[UUID(value) for value in selection['contacts']] if selection else [contact]
        rows=db.execute(text('SELECT phones FROM alfa_directory WHERE workspace_id=:w AND run_id=:r AND contact_id=ANY(CAST(:ids AS uuid[]))'),{'w':workspace,'r':snapshot['run'],'ids':ids}).mappings().all()
        if not rows:raise AuthError(404,'Контакт недоступен.')
        return sorted({number for row in rows for raw in row['phones'] if (number:=phone_number(raw))})

    def listing(self,actor,workspace,conversation,*,before_time=None,before_id=None,limit=40,contact=False):
        if (before_time is None)!=(before_id is None):raise AuthError(422,'Нужен полный указатель страницы звонков.')
        transaction=self.alfa._transaction if contact else self.alfa.chat_transaction
        with transaction(actor,workspace,snapshot=True) as db:
            phones=self.contact_phones(db,workspace,conversation) if contact else self.phones(db,workspace,conversation)
            account=db.execute(text('SELECT account_id FROM telephony_state WHERE workspace_id=:w'),{'w':workspace}).scalar_one_or_none()
            scope=hashlib.sha256(json.dumps([phones,account],sort_keys=True).encode()).hexdigest()
            if not phones:return {'calls':[],'has_more':False,'scope':scope}
            where=''
            params={'w':workspace,'phones':phones,'limit':limit+1}
            if before_time is not None:
                where=' AND (c.started_at,c.id)<(:before_time,:before_id)'
                params.update(before_time=before_time,before_id=before_id)
            rows=list(db.execute(text("SELECT c.*,s.connected FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND c.phone=ANY(CAST(:phones AS text[]))"+where+' ORDER BY c.started_at DESC,c.id DESC LIMIT :limit'),params).mappings())
            return {'calls':[call_view(row,audio_available=row['connected']) for row in reversed(rows[:limit])],
                    'has_more':len(rows)>limit,'match':'current_crm_phone','scope':scope}

    def recording(self,actor,workspace,conversation,call,range_header,*,contact=False):
        transaction=self.alfa._transaction if contact else self.alfa.chat_transaction
        with transaction(actor,workspace) as db:
            phones=self.contact_phones(db,workspace,conversation) if contact else self.phones(db,workspace,conversation)
            row=db.execute(text("SELECT c.id FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND c.id=:id AND c.phone=ANY(CAST(:phones AS text[])) AND s.connected AND c.recording_ciphertext IS NOT NULL"),{'w':workspace,'id':call,'phones':phones}).scalar_one_or_none()
            if row is None:raise AuthError(404,'Запись недоступна в этой карточке.')
            audit(db,'telephony_recording_read',user_id=actor.user_id,workspace_id=workspace,subject_id=str(call))
        return self.audio.read(workspace,call,range_header)
