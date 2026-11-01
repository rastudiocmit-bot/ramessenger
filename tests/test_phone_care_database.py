"""Required isolated PostgreSQL/RLS gate, never run against a live workspace."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from sqlalchemy import text
from center_messenger.accounts import Accounts, context
from center_messenger.auth_security import AuthError, Identity
from center_messenger.config import Settings
from center_messenger.telephony_import import TelephonyImport
from center_messenger.phone_care import FamilyCare, load, CASE, SOURCE, enqueue
from center_messenger.phone_care_worker import CareWorker
import test_novofon_connection as fixture

NOW=datetime(2026,11,1,10,0,tzinfo=timezone.utc)


class FixtureAccounts(Accounts):
    def transaction(self, actor=None, *, snapshot=False, **scopes):
        # The schema fixture encloses each test in one rollback transaction.
        # PostgreSQL cannot change its isolation after seeding. Keep the real
        # session/membership checks and savepoints; top-level snapshot flags
        # are checked separately in the offline service tests.
        return super().transaction(actor, snapshot=False, **scopes)


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class FamilyCareDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.NovofonDatabaseTests.setUpClass.__func__)
    tearDownClass=classmethod(fixture.NovofonDatabaseTests.tearDownClass.__func__)
    tearDown=fixture.NovofonDatabaseTests.tearDown

    def setUp(self):
        fixture.NovofonDatabaseTests.setUp(self)
        self.db.execute(text("INSERT INTO workspace_memberships(workspace_id,user_id,role) VALUES(:w,:u,'owner')"),{'w':self.workspace,'u':self.user})
        self.other=uuid4()
        self.db.execute(text("INSERT INTO auth_users(id,email,display_name,password_hash) VALUES(:u,:e,'Operator','unused')"),{'u':self.other,'e':str(self.other)+'@example.test'})
        self.db.execute(text("INSERT INTO workspace_memberships(workspace_id,user_id,role,sections) VALUES(:w,:u,'operator',ARRAY['messages','tasks'])"),{'w':self.workspace,'u':self.other})
        self.actor=self.actor_for(self.user);self.operator=self.actor_for(self.other)
        self.db.execute(text("INSERT INTO novofon_settings(workspace_id,connection_id,key_ciphertext) VALUES(:w,:c,'synthetic')"),{'w':self.workspace,'c':uuid4()})
        TelephonyImport.ensure(self.db,self.workspace)
        self.db.execute(text("UPDATE telephony_state SET account_id='17',connected=true,timezone='Europe/Moscow' WHERE workspace_id=:w"),{'w':self.workspace})
        @contextmanager
        def begin():
            with self.db.begin_nested():yield self.db
        self.engine=SimpleNamespace(begin=begin)
        self.accounts=FixtureAccounts(self.engine,Settings(),passwords=Mock())
        self.service=FamilyCare(self.accounts,clock=lambda:NOW);self.worker=CareWorker(self.accounts,clock=lambda:NOW)
        self.db.execute(text('GRANT USAGE ON SCHEMA "'+self.schema+'" TO cm_app'))
        self.db.execute(text('SET LOCAL ROLE cm_app'))
        context(self.db,workspace_id=self.workspace,user_id=self.user,membership_admin='true')

    def actor_for(self,user):
        session=uuid4()
        self.db.execute(text("INSERT INTO auth_sessions(id,user_id,token_hash,auth_epoch,device_label,expires_at) VALUES(:id,:u,:hash,0,'Synthetic',now()+interval '1 day')"),{'id':session,'u':user,'hash':hashlib.sha256(str(session).encode()).hexdigest()})
        return Identity(user_id=user,session_id=session,email=str(user)+'@example.test',display_name='Synthetic',email_verified=False,auth_epoch=0,raw_session='synthetic-session')

    def make_call(self,*,missed=True,at=NOW,phone='+79990000000'):
        identifier=uuid4()
        self.db.execute(text("INSERT INTO telephony_calls(workspace_id,id,provider,account_id,source_id,connection_id,phone,direction,started_at,finished_at,talk_seconds,total_seconds,missed,fingerprint) VALUES(:w,:c,'novofon','17',:source,:connection,:phone,'in',:at,:finish,:talk,60,:missed,:hash)"),{'w':self.workspace,'c':identifier,'source':str(identifier),'connection':uuid4(),'phone':phone,'at':at,'finish':at+timedelta(seconds=60),'talk':0 if missed else 40,'missed':missed,'hash':'a'*64})
        enqueue(self.db,self.workspace,identifier)
        return identifier

    def one_case(self):
        self.make_call();self.assertTrue(self.worker.process(self.workspace))
        board=self.service.board(self.actor,self.workspace,{})
        self.assertEqual(len(board['cases']),1)
        return self.service.detail(self.actor,self.workspace,{'case_id':board['cases'][0]['id']})

    def task_data(self,value,**extra):
        return {'case_id':value['id'],'expected_version':value['version'],'signal_id':value['signals'][0]['id'],
                'title':'Связаться с семьёй','details':'Проверено сотрудником','due_at':NOW+timedelta(hours=2),'assignee_id':self.user}|extra

    def test_worker_deduplicates_family_and_a_later_answer_changes_contact_fact(self):
        self.make_call(at=NOW-timedelta(hours=2));self.make_call(missed=False,at=NOW)
        for _ in range(4):self.worker.process(self.workspace)
        board=self.service.board(self.actor,self.workspace,{})
        self.assertEqual(len(board['cases']),1)
        value=self.service.detail(self.actor,self.workspace,{'case_id':board['cases'][0]['id']})
        self.assertEqual(len(value['calls']),2)
        self.assertEqual(next(x['state'] for x in value['signals'] if x['kind']=='missed'),'contacted')
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM messages')).scalar_one(),0)

    def test_confirmed_task_and_case_are_atomic_and_repeated_confirmation_is_idempotent(self):
        value=self.one_case();data=self.task_data(value)
        first=self.service.task(self.actor,self.workspace,data)
        second=self.service.task(self.actor,self.workspace,data|{'expected_version':first['version']})
        self.assertEqual(first['task']['id'],second['task']['id']);self.assertEqual(first['version'],second['version'])
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM tasks')).scalar_one(),1)
        self.assertEqual(first['task']['sources'][0]['case_id'],value['id'])
        before=load(self.db,self.workspace,CASE,value['id'])
        with self.assertRaises(AuthError):self.service.task(self.actor,self.workspace,data)
        self.assertEqual(load(self.db,self.workspace,CASE,value['id']),before)

    def test_failure_after_task_insert_rolls_back_both_objects(self):
        value=self.one_case();before=load(self.db,self.workspace,CASE,value['id'])
        with patch('center_messenger.phone_care.save_case',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):self.service.task(self.actor,self.workspace,self.task_data(value))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM tasks')).scalar_one(),0)
        self.assertEqual(load(self.db,self.workspace,CASE,value['id']),before)

    def test_task_assignee_can_read_source_but_cannot_reassign_another_cases_owner(self):
        value=self.one_case()
        self.service.patch(self.actor,self.workspace,{'case_id':value['id'],'expected_version':value['version'],'assignee_id':self.user})
        with self.assertRaises(AuthError):self.service.detail(self.operator,self.workspace,{'case_id':value['id']})
        value=self.service.detail(self.actor,self.workspace,{'case_id':value['id']})
        self.service.task(self.actor,self.workspace,self.task_data(value,assignee_id=self.other))
        own=self.service.detail(self.operator,self.workspace,{'case_id':value['id']})
        self.assertFalse(own['can_edit']);self.assertEqual(len(own['linked_tasks']),1)
        with self.assertRaises(AuthError):self.service.patch(self.operator,self.workspace,{'case_id':value['id'],'expected_version':own['version'],'assignee_id':None})
        self.assertEqual(len(self.service.board(self.operator,self.workspace,{})['cases']),1)

    def test_other_workspace_and_replaced_account_do_not_expose_old_cases(self):
        value=self.one_case()
        with self.assertRaises(AuthError):self.service.detail(self.actor,uuid4(),{'case_id':value['id']})
        self.db.execute(text("UPDATE telephony_state SET account_id='18' WHERE workspace_id=:w"),{'w':self.workspace})
        self.assertEqual(self.service.board(self.actor,self.workspace,{})['cases'],[])
        with self.assertRaises(AuthError):self.service.detail(self.actor,self.workspace,{'case_id':value['id']})

    def test_schema_keeps_sources_separate_and_unknown_source_does_not_block_next_call(self):
        self.db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) VALUES(:w,:id,:t,'bad','{}','pending')"),{'w':self.workspace,'id':uuid4(),'t':SOURCE})
        self.worker.process(self.workspace)
        self.assertEqual(self.db.execute(text("SELECT state FROM outbox_events WHERE topic=:t AND event_key='bad'"),{'t':SOURCE}).scalar_one(),'failed')
        self.make_call();self.assertTrue(self.worker.process(self.workspace))
        self.assertEqual(len(self.service.board(self.actor,self.workspace,{})['cases']),1)

    def test_completion_requires_result_and_stage_is_never_inferred(self):
        value=self.one_case()
        with self.assertRaises(AuthError):self.service.patch(self.actor,self.workspace,{'case_id':value['id'],'expected_version':value['version'],'status':'resolved'})
        self.service.patch(self.actor,self.workspace,{'case_id':value['id'],'expected_version':value['version'],'status':'resolved','stage':'trial_booked','journey':'new','note':'Запись проверена в CRM'})
        metrics=self.service.board(self.actor,self.workspace,{})['metrics']
        self.assertEqual(metrics['confirmed_stages']['trial_booked'],1)
        self.assertEqual(metrics['confirmed_stages']['trial_attended'],0)

    def test_call_history_and_recording_authorization_are_bound_to_the_case(self):
        value=self.one_case();other=self.make_call(phone='+79991111111')
        self.assertEqual(len(self.service.history(self.actor,self.workspace,{'case_id':value['id']})['calls']),1)
        with self.assertRaises(AuthError):self.service.call(self.actor,self.workspace,{'case_id':value['id'],'call_id':other,'recording':True})

    def test_recording_read_writes_audit_without_changing_case_or_task(self):
        value=self.one_case();call=value['calls'][0]['id']
        self.db.execute(text("UPDATE telephony_calls SET recording_ciphertext='opaque-synthetic' WHERE id=:id"),{'id':call})
        before=load(self.db,self.workspace,CASE,value['id'])
        result=self.service.call(self.actor,self.workspace,{'case_id':value['id'],'call_id':call,'recording':True})
        self.assertEqual(result,{'authorized':True})
        self.assertEqual(load(self.db,self.workspace,CASE,value['id']),before)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM auth_events WHERE action='phone_care_recording_read'")).scalar_one(),1)

    def test_worker_rechecks_revoked_session_instead_of_trusting_api_identity(self):
        value=self.one_case()
        self.db.execute(text('UPDATE auth_sessions SET revoked_at=now() WHERE id=:id'),{'id':self.actor.session_id})
        with self.assertRaises(AuthError) as raised:self.service.detail(self.actor,self.workspace,{'case_id':value['id']})
        self.assertEqual(raised.exception.status,401)
