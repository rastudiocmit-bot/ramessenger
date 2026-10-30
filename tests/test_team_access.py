"""Atomic employee grants, invitation scope and unchanged shared CRM sessions."""
from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from center_messenger import accounts_api
from center_messenger.accounts import Accounts
from center_messenger.auth_security import AuthError, MemberAccessInput, InvitationInput, AcceptInvitationInput, csrf_token
from center_messenger.config import Settings
import test_message_sync as fixture


class TeamAccessTests(unittest.TestCase):
    def test_strict_grants_and_dependencies(self):
        for value in [dict(role='owner',sections=['messages']),dict(role='operator',sections=[]),
                      dict(role='operator',sections=['tasks']),dict(role='operator',sections=['connections']),
                      dict(role='operator',sections=['messages'],user_id=str(uuid4()))]:
            with self.subTest(value=value),self.assertRaises(ValidationError):
                MemberAccessInput(**value)
        self.assertEqual(MemberAccessInput(role='admin',sections=['messages']).sections,[])
        self.assertEqual(InvitationInput(email='test@example.com',role='operator').sections,['overview','security'])

    def test_combined_route_requires_csrf_and_validated_grants(self):
        actor=SimpleNamespace(user_id=uuid4(),raw_session='synthetic-session')
        ops=SimpleNamespace(settings=SimpleNamespace(cookie_name='session'),authenticate=Mock(return_value=actor),set_member_access=Mock())
        app=FastAPI();app.state.accounts=ops;app.include_router(accounts_api.router)
        @app.exception_handler(AuthError)
        async def rejected(request,error): return JSONResponse({'detail':error.detail},status_code=error.status)
        client=TestClient(app);workspace,user=uuid4(),uuid4();url=f'/api/workspaces/{workspace}/members/{user}/access'
        body={'role':'operator','sections':['messages','tasks']}
        self.assertEqual(client.post(url,json=body).status_code,403);ops.set_member_access.assert_not_called()
        headers={'X-CSRF-Token':csrf_token(actor.raw_session)}
        self.assertEqual(client.post(url,json={**body,'role':'owner'},headers=headers).status_code,422)
        self.assertEqual(client.post(url,json=body,headers=headers).status_code,204)
        args=ops.set_member_access.call_args.args
        self.assertEqual(args[:3],(actor,workspace,user));self.assertEqual(args[3].sections,['messages','tasks'])


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class TeamAccessDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.MessageSyncTests.setUpClass.__func__)
    tearDownClass=classmethod(fixture.MessageSyncTests.tearDownClass.__func__)
    tearDown=fixture.MessageSyncTests.tearDown
    add_customer=fixture.MessageSyncTests.add_customer

    def setUp(self):
        fixture.MessageSyncTests.setUp(self)
        self.target=uuid4();self.session=uuid4()
        self.db.execute(text("INSERT INTO auth_users(id,email,display_name,password_hash) VALUES(:u,'target@example.com','Target','unused')"),{'u':self.target})
        self.db.execute(text("INSERT INTO workspace_memberships(workspace_id,user_id,role,sections) VALUES(:w,:u,'owner','{}'),(:w,:t,'operator',ARRAY['messages','tasks','security'])"),{'w':self.workspace,'u':self.user,'t':self.target})
        self.db.execute(text("INSERT INTO auth_sessions(id,user_id,token_hash,auth_epoch,device_label,expires_at) VALUES(:s,:u,:hash,0,'Owner',now()+interval '1 day')"),{'s':self.session,'u':self.user,'hash':uuid4().hex*2})
        outer=self
        class Engine:
            @contextmanager
            def begin(self):
                with outer.db.begin_nested():yield outer.db
        self.ops=Accounts(Engine(),Settings(public_url='https://example.test'),passwords=Mock())
        self.ops.passwords.hash.return_value='synthetic-hash';self.ops.throttle=Mock()
        self.actor=SimpleNamespace(user_id=self.user,session_id=self.session,auth_epoch=0,email='owner@example.com')
        self.db.execute(text(f'GRANT USAGE ON SCHEMA "{self.schema}" TO cm_app'))

    def value(self):
        return dict(self.db.execute(text('SELECT role,sections,active FROM workspace_memberships WHERE workspace_id=:w AND user_id=:u'),{'w':self.workspace,'u':self.target}).mappings().one())

    def save(self,role='observer',sections=None):
        with self.db.begin_nested():
            self.db.execute(text('SET LOCAL ROLE cm_app'))
            self.ops.set_member_access(self.actor,self.workspace,self.target,MemberAccessInput(role=role,sections=sections or ['messages','security']))

    def test_role_and_sections_commit_together_without_changing_sessions_or_crm(self):
        before=dict(self.db.execute(text('SELECT session_ciphertext,version FROM alfa_max_settings WHERE workspace_id=:w'),{'w':self.workspace}).mappings().one())
        self.save()
        self.assertEqual(self.value(),{'role':'observer','sections':['messages','security'],'active':True})
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM auth_events WHERE action='member_access_updated'")).scalar_one(),1)
        self.assertIsNone(self.db.execute(text('SELECT revoked_at FROM auth_sessions WHERE id=:s'),{'s':self.session}).scalar_one())
        self.assertEqual(dict(self.db.execute(text('SELECT session_ciphertext,version FROM alfa_max_settings WHERE workspace_id=:w'),{'w':self.workspace}).mappings().one()),before)
        self.save('admin',['messages']);self.assertEqual(self.value()['sections'],[])
        self.save('operator',['messages']);self.assertEqual(self.value()['role'],'operator')

    def test_failure_rolls_back_both_role_and_sections(self):
        before=self.value()
        with patch('center_messenger.accounts.audit',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):self.save()
        self.assertEqual(self.value(),before)

    def test_server_rejects_self_owner_and_admin_escalation_under_rls(self):
        for who in [self.user]:
            with self.assertRaises(AuthError) as denied:self.ops.set_member_access(self.actor,self.workspace,who,MemberAccessInput(role='operator',sections=['messages']))
            self.assertEqual(denied.exception.status,403)
        self.db.execute(text("UPDATE workspace_memberships SET role='admin' WHERE user_id=:u"),{'u':self.user})
        before=self.value()
        with self.assertRaises(AuthError) as denied:self.save('admin')
        self.assertEqual(denied.exception.status,403);self.assertEqual(self.value(),before)
        self.db.execute(text("UPDATE workspace_memberships SET role='admin' WHERE user_id=:u"),{'u':self.target})
        with self.assertRaises(AuthError):self.save('operator')
        self.db.execute(text("UPDATE workspace_memberships SET role='operator' WHERE user_id=:u"),{'u':self.user})
        with self.assertRaises(AuthError):self.save('observer')

    def test_foreign_or_inactive_membership_cannot_be_modified(self):
        with self.assertRaises(AuthError):self.ops.set_member_access(self.actor,uuid4(),self.target,MemberAccessInput(role='observer',sections=['messages']))
        self.db.execute(text('UPDATE workspace_memberships SET active=false WHERE user_id=:u'),{'u':self.target})
        with self.assertRaises(AuthError) as denied:self.save()
        self.assertEqual(denied.exception.status,404)

    def invite_accept(self,email,identity=None):
        invite=self.ops.invite(self.actor,self.workspace,InvitationInput(email=email,role='observer',sections=['messages','security']))
        token=invite['url'].split('#invite=')[1]
        self.ops.accept_invitation(AcceptInvitationInput(token=token,display_name='New employee',password='synthetic-long-password'),identity,'127.0.0.1','Other device')

    def test_invitation_applies_selected_sections_and_does_not_overwrite_existing_active_access(self):
        self.invite_accept('new@example.com')
        row=self.db.execute(text("SELECT role,sections FROM workspace_memberships JOIN auth_users ON user_id=auth_users.id WHERE email='new@example.com'")).mappings().one()
        self.assertEqual(dict(row),{'role':'observer','sections':['messages','security']})
        target_session=uuid4()
        self.db.execute(text("INSERT INTO auth_sessions(id,user_id,token_hash,auth_epoch,device_label,expires_at) VALUES(:s,:u,:hash,0,'Target',now()+interval '1 day')"),{'s':target_session,'u':self.target,'hash':uuid4().hex*2})
        target_actor=SimpleNamespace(user_id=self.target,session_id=target_session,auth_epoch=0,email='target@example.com')
        before=self.value();self.invite_accept('target@example.com',target_actor);self.assertEqual(self.value(),before)
        self.assertIsNone(self.db.execute(text('SELECT revoked_at FROM auth_sessions WHERE id=:s'),{'s':self.session}).scalar_one())
