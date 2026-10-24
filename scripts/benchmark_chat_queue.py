#!/usr/bin/env python3
"""Synthetic queue benchmark; requires an isolated local CM_TEST_DATABASE_URL.

Creates a disposable schema through the regression fixture and rolls it back.
No customer data or production URLs are accepted.
"""
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from sqlalchemy import event, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_message_sync import MessageSyncTests
from center_messenger.chat_queue import repair_batch, refresh


def main():
    url = os.environ.get('CM_TEST_DATABASE_URL', '')
    if urlsplit(url).hostname not in {'127.0.0.1', 'localhost'}:
        raise SystemExit('An isolated local PostgreSQL test database is required')
    MessageSyncTests.setUpClass()
    fixture = MessageSyncTests()
    fixture.setUp()
    db, workspace, channel = fixture.db, fixture.workspace, fixture.channel
    params = {'w': workspace, 'channel': channel, 'run': fixture.run, 'at': fixture.at}
    try:
        db.execute(text("INSERT INTO contacts(workspace_id,id,display_name) SELECT :w,md5('bench-contact-'||n)::uuid,'Synthetic '||n FROM generate_series(1,3741) n"), params)
        db.execute(text("INSERT INTO alfa_customers(workspace_id,run_id,branch_id,external_id,name,status,contact_id) SELECT :w,:run,1,10000+n,'Synthetic child '||n,'client',md5('bench-contact-'||n)::uuid FROM generate_series(1,3741) n"), params)
        db.execute(text("INSERT INTO alfa_directory(workspace_id,run_id,contact_id,display_name,phones,emails,search_text) SELECT :w,:run,md5('bench-contact-'||n)::uuid,'Synthetic '||n,'[]','[]','synthetic' FROM generate_series(1,3741) n"), params)
        db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id,peer_name) SELECT :w,md5('bench-conv-'||n)::uuid,:channel,md5('bench-contact-'||n)::uuid,'bench:'||n,'Synthetic '||n FROM generate_series(1,3741) n"), params)
        db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,provider_message_id,direction,actor_type,text,payload,occurred_at) SELECT :w,md5('bench-message-'||n||':'||k)::uuid,md5('bench-conv-'||n)::uuid,'bench:'||n||':'||k,CASE WHEN k>12 THEN 'out' ELSE 'in' END,CASE WHEN k=14 THEN 'automation' WHEN k=13 THEN CASE WHEN n%3=0 THEN 'human' ELSE 'unknown' END ELSE 'contact' END,'Synthetic message '||k,jsonb_build_object('source','synthetic'),CAST(:at AS timestamptz)-n*interval '1 minute'+k*interval '1 second' FROM generate_series(1,3741) n CROSS JOIN generate_series(1,14) k"), params)
        db.execute(text("INSERT INTO message_changes(workspace_id,conversation_id,message_id) SELECT workspace_id,conversation_id,id FROM messages WHERE workspace_id=:w ORDER BY occurred_at,id"), params)
        db.execute(text('SET LOCAL jit=off'))
        db.execute(text('ANALYZE'))
        db.execute(text("SET LOCAL statement_timeout='2500ms'"))
        started = time.perf_counter()
        initial = fixture.reader.listing(None,workspace)
        assert initial['total'] == 3741
        initial_live_ms = round((time.perf_counter()-started)*1000,2)
        batches, repair_started = [], time.perf_counter()
        while True:
            started = time.perf_counter()
            more = repair_batch(db, workspace, channel)
            batches.append((time.perf_counter()-started)*1000)
            if not more: break
        repair_ms = (time.perf_counter()-repair_started)*1000
        query_count = 0
        def count(*args):
            nonlocal query_count
            query_count += 1
        event.listen(db, 'before_cursor_execute', count)
        reads = []
        for pages in (1,2,3):
            # Warm one read, then report three independent prefix snapshots.
            fixture.reader.listing(None,workspace,through_page=pages-1)
            timings, counts = [], []
            for _ in range(3):
                query_count = 0; started = time.perf_counter()
                listing = fixture.reader.listing(None,workspace,through_page=pages-1)
                timings.append(round((time.perf_counter()-started)*1000,2)); counts.append(query_count)
                assert len(listing['conversations']) == pages*30
                assert len({c['id'] for c in listing['conversations']}) == pages*30
            reads.append({'rows':pages*30,'milliseconds':timings,'queries':counts})
        windows = []
        prior = None
        for size in (90,180,360):
            started = time.perf_counter()
            listing = fixture.reader.listing(None,workspace,window=size,window_token=prior)
            load_ms = round((time.perf_counter()-started)*1000,2)
            started = time.perf_counter()
            unchanged = fixture.reader.listing(None,workspace,window=size,window_token=listing['window_token'])
            unchanged_ms = round((time.perf_counter()-started)*1000,2)
            assert unchanged['not_modified'] and not unchanged['conversations']
            assert len(listing['order']) == size
            windows.append({'loaded_rows':size,'transferred_rows':len(listing['conversations']),
                'milliseconds':load_ms,'unchanged_ms':unchanged_ms,
                'unchanged_bytes':len(json.dumps(unchanged,default=str).encode())})
            prior = listing['window_token']
        identifier = db.execute(text('SELECT id FROM conversations WHERE workspace_id=:w ORDER BY id LIMIT 1'),params).scalar_one()
        query_count = 0; started = time.perf_counter()
        db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('failed',true) WHERE workspace_id=:w AND conversation_id=:c AND actor_type='human'"), {'w':workspace,'c':identifier})
        refresh(db,workspace,identifier)
        update = {'milliseconds':round((time.perf_counter()-started)*1000,2),'queries':query_count}
        print(json.dumps({'database':db.execute(text('SELECT version()')).scalar_one(),
                          'conversations':3741,'messages':52374,
                          'initial_live_read_ms':initial_live_ms,'repair_ms':round(repair_ms,2),'batches':len(batches),'max_batch_ms':round(max(batches),2),
                          'prefix_reads':reads,'window_reads':windows,'one_message_update':update},indent=2))
    finally:
        fixture.tearDown()
        fixture.doCleanups()
        MessageSyncTests.tearDownClass()


if __name__ == '__main__':
    main()
