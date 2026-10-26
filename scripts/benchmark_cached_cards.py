#!/usr/bin/env python3
"""Synthetic cache-read regression. Requires an isolated local CM_TEST_DATABASE_URL.

Creates a temporary schema and rolls it back. No external CRM clients or customer
data are used. This is a scalability check, not a production latency measurement.
"""
import json,os,sys,time
from urllib.parse import urlsplit
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import event,text
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_message_sync import MessageSyncTests
from center_messenger import card_preference
from center_messenger.crm_api import DirectoryReader
from uuid import UUID

def main():
    url=os.environ.get('CM_TEST_DATABASE_URL','')
    if urlsplit(url).hostname not in {'127.0.0.1','localhost'}:
        raise SystemExit('An isolated local PostgreSQL test database is required')
    MessageSyncTests.setUpClass();f=MessageSyncTests();f.setUp()
    try:
     db=f.db;f.snapshot.update(origin='https://example.s20.online',version=1,today=f.at.date(),finished_at=f.at,timezone='Europe/Moscow')
     p={'w':f.workspace,'r':f.run,'cn':f.channel,'crm':f.crm,'at':f.at,'user':f.user}
     db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'));db.execute(text('SET CONSTRAINTS ALL DEFERRED'))
     # Bulk fixture setup skips only derived invalidation triggers. Source foreign
     # keys, message counters and tenant policies remain enabled; reads below run
     # after these triggers are restored and under the ordinary application role.
     seeded=('contacts','alfa_customers','alfa_directory','conversations','messages','message_changes','outbox_events')
     for table in seeded:db.execute(text('ALTER TABLE '+table+' DISABLE TRIGGER chat_read_version'))
     db.execute(text("INSERT INTO alfa_sync_control(workspace_id,connection_id,published_id,last_run_id,requested_by) VALUES(:w,:crm,:r,:r,:user)"),p)
     db.execute(text("INSERT INTO alfa_branches(workspace_id,connection_id,external_id,name) VALUES(:w,:crm,3,'Branch 3'),(:w,:crm,4,'Branch 4')"),p)
     db.execute(text("INSERT INTO contacts(workspace_id,id,display_name) SELECT :w,md5('cold-person-'||n)::uuid,'Synthetic parent '||n FROM generate_series(1,3741) n"),p)
     db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,legal_name,status,contact_id,phones) SELECT :w,:r,b,100000+n,'Synthetic child '||n,'Synthetic parent '||n,'client',md5('cold-person-'||n)::uuid,jsonb_build_array('+7998'||lpad(n::text,7,'0')) FROM generate_series(1,3741) n CROSS JOIN generate_series(1,4) b"),p)
     db.execute(text("INSERT INTO alfa_directory(workspace_id,run_id,contact_id,display_name,phones,emails,search_text) SELECT :w,:r,md5('cold-person-'||n)::uuid,'Synthetic parent '||n,jsonb_build_array('+7998'||lpad(n::text,7,'0')),'[]','synthetic' FROM generate_series(1,3741) n"),p)
     db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id,peer_name,needs_reply,last_message_at,last_inbound_at,inbound_seq) SELECT :w,md5('cold-chat-'||n)::uuid,:cn,md5('cold-person-'||n)::uuid,'cold:'||n,'Synthetic parent '||n,true,CAST(:at AS timestamptz)-n*interval '1 minute',CAST(:at AS timestamptz)-n*interval '1 minute',1 FROM generate_series(1,2776) n"),p)
     db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,provider_message_id,direction,actor_type,text,payload,occurred_at) SELECT :w,md5('cold-message-'||n||':'||k)::uuid,md5('cold-chat-'||n)::uuid,'cold:'||n||':'||k,CASE WHEN k=20 THEN 'in' ELSE 'out' END,CASE WHEN k=20 THEN 'contact' ELSE 'automation' END,'Synthetic history',jsonb_build_object('crm_order',k),CAST(:at AS timestamptz)-n*interval '1 minute'-(20-k)*interval '1 second' FROM generate_series(1,2776) n CROSS JOIN generate_series(1,20) k"),p)
     db.execute(text("INSERT INTO message_changes(workspace_id,conversation_id,message_id,new_inbound) SELECT workspace_id,conversation_id,id,direction='in' FROM messages WHERE workspace_id=:w ORDER BY occurred_at,id"),p)
     db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) SELECT :w,md5('cold-queue-'||n)::uuid,'chat.list.state',md5('cold-chat-'||n)::uuid::text,jsonb_build_object('projection_version',2,'unanswered_count',1,'needs_reply',true,'latest_inbound_id',md5('cold-message-'||n||':'||20)::uuid,'last_unanswered_message_id',md5('cold-message-'||n||':'||20)::uuid,'preview_message_id',md5('cold-message-'||n||':'||20)::uuid,'last_unanswered_at',CAST(:at AS timestamptz)-n*interval '1 minute','last_inbound_at',CAST(:at AS timestamptz)-n*interval '1 minute','last_human_activity_at',CAST(:at AS timestamptz)-n*interval '1 minute','last_event_at',CAST(:at AS timestamptz)-n*interval '1 minute','priority_at',CAST(:at AS timestamptz)-n*interval '1 minute','preview_text','Synthetic history','preview_attachments','[]'::jsonb,'last_direction','in','last_state','received'),'published' FROM generate_series(1,2776) n"),p)
     sources=list(db.execute(text("SELECT contact_id FROM conversations WHERE workspace_id=:w"),p).scalars())
     started=time.perf_counter();card_preference.refresh(db,f.workspace,f.snapshot,sources)
     report={'customers':14965,'dialogs':2776,'messages':55520,'preference_seconds':round(time.perf_counter()-started,2)}
     db.execute(text("UPDATE connections SET capabilities=jsonb_build_object('chat_queue_projection',jsonb_build_object('version',2,'complete',true)) WHERE workspace_id=:w AND id=:cn"),p)
     for table in seeded:db.execute(text('ALTER TABLE '+table+' ENABLE TRIGGER chat_read_version'))
     db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'));db.execute(text('SET CONSTRAINTS ALL DEFERRED'))
     db.execute(text('ANALYZE'));db.execute(text(f'GRANT USAGE ON SCHEMA "{f.schema}" TO cm_app'))
     db.execute(text("SELECT set_config('app.workspace_id',:w,true),set_config('app.user_id',:u,true)"),{'w':str(f.workspace),'u':str(f.user)})
     db.execute(text('SET LOCAL ROLE cm_app'));db.execute(text('SET LOCAL jit=off'))
     timings=[]
     def begin(conn,cursor,statement,parameters,context,many):
      context.cold_started=time.perf_counter()
     def end(conn,cursor,statement,parameters,context,many):
      timings.append({'query':('counts' if 'AS total,count' in statement else 'rows' if 'WITH selected AS MATERIALIZED' in statement else 'families' if 'AS children' in statement else statement.split()[0]),'ms':round((time.perf_counter()-context.cold_started)*1000,2)})
     event.listen(db,'before_cursor_execute',begin);event.listen(db,'after_cursor_execute',end)
     started=time.perf_counter();data=f.reader.bootstrap(SimpleNamespace(user_id=f.user),f.workspace)
     report['bootstrap']={'seconds':round(time.perf_counter()-started,2),'chats':data['listing']['total'],'rows':len(data['listing']['conversations']),'slowest_query_ms':max(item['ms'] for item in timings)}
     directory=DirectoryReader(SimpleNamespace(accounts=f.accounts,_transaction=f.reader.max.chat_transaction,chat_transaction=f.reader.max.chat_transaction))
     first=data['listing']['conversations'][0]
     for cached_only in (True,False):
      timings.clear();started=time.perf_counter()
      card=directory.prepare(None,f.workspace,UUID(first['contact_id']),conversation=UUID(first['id']),cached_only=cached_only,compact=True)
      elapsed=time.perf_counter()-started
      report['cached_card' if cached_only else 'open_card']={'seconds':round(elapsed,3),'slowest_query_ms':max(item['ms'] for item in timings)}
      assert elapsed<2.5, 'A cached family read exceeded the transactional query budget'
    finally:
     f.tearDown();f.doCleanups();MessageSyncTests.tearDownClass()
    print(json.dumps(report,indent=2),flush=True)


if __name__=="__main__":
    main()
