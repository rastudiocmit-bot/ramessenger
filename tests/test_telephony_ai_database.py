"""Mandatory isolated PostgreSQL checks for receipts, history and night pacing."""
from contextlib import contextmanager
from datetime import datetime,timedelta
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

from sqlalchemy import text
from center_messenger.accounts import context
from center_messenger.auth_security import AuthError
from center_messenger.telephony_ai_store import Store,enqueue,snapshot,wake
from center_messenger.telephony_agent import defaults
from center_messenger.telephony_processing import views
from center_messenger import telephony_archive as archive
import test_novofon_connection as fixture


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class TelephoneAIDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.NovofonDatabaseTests.setUpClass.__func__)
    tearDownClass=classmethod(fixture.NovofonDatabaseTests.tearDownClass.__func__)
    tearDown=fixture.NovofonDatabaseTests.tearDown

    def setUp(self):
        fixture.NovofonDatabaseTests.setUp(self)
        self.now=datetime(2026,11,1,0,0,tzinfo=archive.ZONE)
        self.db.execute(text("INSERT INTO workspace_memberships(workspace_id,user_id,role) VALUES(:w,:u,'owner')"),{'w':self.workspace,'u':self.user})
        context(self.db,workspace_id=self.workspace,user_id=self.user,membership_admin='true')
        self.agent=defaults(self.workspace) | {'version':1}
        self.profile=snapshot({'gateway_config':{'telephony_agent':self.agent}},self.workspace)
        self.db.execute(text("INSERT INTO ai_settings(workspace_id,key_ciphertext,folder_id,gateway_config) VALUES(:w,'synthetic-cipher','syntheticfolder',CAST(:g AS jsonb))"),{'w':self.workspace,'g':json.dumps({'telephony_agent':self.agent})})
        self.db.execute(text('INSERT INTO telephony_archive_control(workspace_id,enabled,started_by,cursor_start) VALUES(:w,true,:u,:start)'),{'w':self.workspace,'u':self.user,'start':archive.START})
        @contextmanager
        def begin():
            with self.db.begin_nested():yield self.db
        self.engine=SimpleNamespace(begin=begin)
        self.store=Store(self.engine,lambda:self.now)
        self.call=self.make_call()

    def make_call(self):
        value=uuid4()
        self.db.execute(text("INSERT INTO telephony_calls(workspace_id,id,provider,account_id,source_id,connection_id,phone,direction,started_at,finished_at,talk_seconds,missed,fingerprint) VALUES(:w,:c,'novofon','17',:source,:connection,'+79990000000','in',:start,:finish,30,false,:hash)"),{'w':self.workspace,'c':value,'source':str(value),'connection':uuid4(),'start':self.now-timedelta(days=8),'finish':self.now-timedelta(days=8)+timedelta(seconds=30),'hash':'a'*64})
        return value

    def queue(self,call=None,request=None,**kwargs):
        return enqueue(self.db,self.workspace,call or self.call,request or uuid4(),self.user,self.profile,historical=True,**kwargs)

    def force_due(self):
        self.db.execute(text("UPDATE telephony_ai_routes SET due_at=now()-interval '1 second' WHERE workspace_id=:w"),{'w':self.workspace})

    def test_duplicate_queue_requests_and_two_buttons_keep_one_current_run(self):
        request=uuid4();one=self.queue(request=request)
        self.assertEqual(self.queue(request=request),one);self.assertEqual(self.queue(),one)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_ai_runs')).scalar_one(),1)

    def test_request_id_cannot_be_reused_for_a_different_call(self):
        request=uuid4();self.queue(request=request)
        with self.assertRaises(AuthError):self.queue(call=self.make_call(),request=request)

    def test_a_committed_sent_receipt_is_not_replayed_after_lease_expiry(self):
        run=self.queue();job=self.store.claim();self.assertEqual(job['run']['id'],run)
        receipt=self.store.receipt(job,'asr:00000:0','b'*64,.2,25)
        self.assertTrue(receipt['new'])
        self.db.execute(text("UPDATE telephony_ai_routes SET lease_until=now()-interval '1 second',due_at=now()-interval '1 second'"))
        restarted=Store(self.engine,lambda:self.now)
        self.assertIsNone(restarted.claim())
        self.assertEqual(self.db.execute(text('SELECT phase FROM telephony_ai_runs')).scalar_one(),'needs_review')
        self.assertEqual(self.db.execute(text('SELECT status FROM telephony_ai_operations')).scalar_one(),'unknown')

    def test_completed_receipt_and_billing_are_atomic_and_cannot_be_counted_twice(self):
        self.queue();job=self.store.claim();self.store.receipt(job,'asr:00000:0','b'*64,.2,25)
        self.assertTrue(self.store.complete(job,'asr:00000:0','Синтетический текст',cost=.2,seconds=25))
        self.assertFalse(self.store.complete(job,'asr:00000:0','Повтор',cost=.2,seconds=25))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM ai_usage')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT result FROM telephony_ai_operations')).scalar_one(),'Синтетический текст')

    def test_incomplete_paid_output_cannot_be_published_as_complete_after_restart(self):
        self.queue();job=self.store.claim();self.store.receipt(job,'summary:final:00000','b'*64,1)
        self.store.complete(job,'summary:final:00000','Неполный ответ',cost=.3,kind='call_summary',usable=False)
        self.assertEqual(self.db.execute(text('SELECT phase FROM telephony_ai_runs')).scalar_one(),'needs_review')
        self.assertFalse(self.db.execute(text('SELECT usable FROM telephony_ai_operations')).scalar_one())
        self.assertEqual(self.db.execute(text('SELECT outcome FROM ai_usage')).scalar_one(),'error')

    def test_night_window_prevents_new_paid_requests_but_preserves_an_inflight_result(self):
        self.queue();self.now=self.now.replace(hour=7,minute=59);job=self.store.claim()
        self.store.receipt(job,'asr:00000:0','b'*64,.2,25)
        self.now=self.now.replace(hour=8,minute=0)
        self.assertTrue(self.store.complete(job,'asr:00000:0','Принятый результат',cost=.2,seconds=25))
        self.assertIsNone(self.store.receipt(job,'asr:00000:1','c'*64,.2,25))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_ai_operations')).scalar_one(),1)

    def test_whole_call_rate_survives_a_new_store_instance(self):
        self.queue();other=self.make_call();self.queue(call=other)
        job=self.store.claim();self.store.update(job,phase='complete');self.store.release(job)
        self.now+=timedelta(seconds=30);self.force_due()
        self.assertIsNone(Store(self.engine,lambda:self.now).claim())
        self.now+=timedelta(seconds=90);self.force_due()
        self.assertIsNotNone(Store(self.engine,lambda:self.now).claim())

    def test_pause_blocks_old_jobs_without_deleting_cursor_or_results(self):
        self.queue();self.db.execute(text('UPDATE telephony_archive_control SET enabled=false'))
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_ai_runs')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT cursor_start FROM telephony_archive_control')).scalar_one(),archive.START)

    def test_budget_reservations_stop_an_additional_paid_request(self):
        self.queue();job=self.store.claim();self.db.execute(text('UPDATE ai_settings SET monthly_limit_rub=.3'))
        self.store.receipt(job,'asr:00000:0','b'*64,.2,25)
        with self.assertRaises(AuthError) as error:self.store.receipt(job,'asr:00000:1','c'*64,.2,25)
        self.assertEqual(error.exception.status,429)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_ai_operations')).scalar_one(),1)

    def test_manual_description_survives_an_explicit_new_run(self):
        old=self.queue();self.db.execute(text("UPDATE telephony_ai_runs SET phase='complete',summary='ИИ описание',transcript='Полная расшифровка'"))
        self.db.execute(text("UPDATE telephony_ai_links SET summary_override='Исправление сотрудника',edit_version=1"))
        new=self.queue(reprocess=True,expected_run=old,ack=True)
        self.assertNotEqual(new,old)
        value=views(self.db,self.workspace,[self.call])[self.call]
        self.assertEqual(value['summary'],'Исправление сотрудника');self.assertEqual(value['summary_source'],'manual')
        self.assertEqual(value['transcript_run_id'],str(old))

    def test_another_workspace_cannot_read_runs_receipts_or_archive_progress(self):
        self.queue();self.db.execute(text('GRANT USAGE ON SCHEMA "'+self.schema+'" TO cm_app'));self.db.execute(text('SET LOCAL ROLE cm_app'))
        context(self.db,workspace_id=uuid4(),membership_admin='false')
        for table in ('telephony_ai_runs','telephony_ai_links','telephony_ai_operations','telephony_archive_control'):
            self.assertEqual(self.db.execute(text(f'SELECT count(*) FROM {table}')).scalar_one(),0)

    def test_schema_downgrade_keeps_receipts_manual_edits_and_source_calls(self):
        self.queue();job=self.store.claim();self.store.receipt(job,'asr:00000:0','b'*64,.2,25)
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from test_message_sync import ROOT
        path=ROOT/'app/backend/migrations/versions/0032_telephony_ai.py'
        spec=importlib.util.spec_from_file_location('telephone_ai_migration_test',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(self.db)):module.downgrade();module.upgrade()
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT status FROM telephony_ai_operations')).scalar_one(),'sent')

    def archive_import(self):
        from center_messenger.telephony_archive_import import ArchiveImport
        from test_telephony_calls import source
        connection=uuid4()
        self.db.execute(text("INSERT INTO novofon_settings(workspace_id,connection_id,key_ciphertext) VALUES(:w,:c,'synthetic-cipher')"),{'w':self.workspace,'c':connection})
        self.db.execute(text("INSERT INTO telephony_state(workspace_id,connected,account_id,timezone) VALUES(:w,true,'17','Europe/Moscow')"),{'w':self.workspace})
        self.db.execute(text('UPDATE telephony_archive_control SET cursor_start=:start,cursor_end=:finish WHERE workspace_id=:w'),{'w':self.workspace,'start':self.now-timedelta(days=9),'finish':self.now-timedelta(days=7)})
        wake(self.db,self.workspace);job=self.store.claim();self.assertIsNone(job['run'])
        client=Mock();client.read.return_value=([source(start_time='2026-10-24 03:00:00',finish_time='2026-10-24 03:01:00')],{'total_items':1})
        vault=Mock();vault.open.return_value={'api_key':'synthetic-key'};vault.seal_recording.return_value='synthetic-record-cipher'
        with patch('center_messenger.telephony_archive_import.client_factory',return_value=Mock(return_value=client)):
            importer=ArchiveImport(self.store,vault)
        return importer,job

    def test_archive_facts_queue_unique_counter_and_cursor_commit_together(self):
        importer,job=self.archive_import();importer.run(job)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),2)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_ai_runs')).scalar_one(),1)
        progress=self.db.execute(text('SELECT imported,cursor_start,page_offset FROM telephony_archive_control')).one()
        self.assertEqual((progress.imported,progress.cursor_start,progress.page_offset),(1,self.now-timedelta(days=7),0))

    def test_failed_queue_insert_rolls_back_archived_facts_counter_and_cursor(self):
        importer,job=self.archive_import()
        with patch('center_messenger.telephony_archive_import.enqueue',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError,'synthetic failure'):importer.run(job)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT imported FROM telephony_archive_control')).scalar_one(),0)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_archive_seen')).scalar_one(),0)
        self.assertEqual(self.db.execute(text('SELECT cursor_start FROM telephony_archive_control')).scalar_one(),self.now-timedelta(days=9))
