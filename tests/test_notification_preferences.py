"""Preference validation and the authenticated, CSRF-protected API boundary."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock, patch
import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from center_messenger import accounts_api, notification_preferences as prefs
from center_messenger.auth_security import AuthError, csrf_token

class NotificationPreferenceTests(unittest.TestCase):
    def test_defaults_and_strict_input(self):
        self.assertFalse(prefs.DEFAULTS['service_popups'])
        self.assertFalse(prefs.DEFAULTS['sound_enabled'])
        for data in [{'volume':101},{'volume':-1},{'volume':'55'},{'service_popups':'yes'},
                     {'client':'loud'},{'user_id':str(uuid4())}]:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                prefs.NotificationPreferencesInput(**data)
        self.assertEqual(prefs.NotificationPreferencesInput(volume=20).model_dump(exclude_unset=True),{'volume':20})

    def setUp(self):
        self.workspace=uuid4();self.actor=SimpleNamespace(user_id=uuid4(),raw_session='opaque-test-session')
        self.transactions=[];self.db=object()
        @contextmanager
        def transaction(actor,**kwargs):
            self.transactions.append((actor,kwargs));yield self.db
        self.accounts=SimpleNamespace(settings=SimpleNamespace(cookie_name='session'),authenticate=Mock(return_value=self.actor),transaction=transaction,_workspace=Mock())
        app=FastAPI();app.state.accounts=self.accounts;app.include_router(accounts_api.router)
        @app.exception_handler(AuthError)
        async def denied(request,error):return JSONResponse({'detail':error.detail},status_code=error.status)
        self.client=TestClient(app);self.url=f'/api/workspaces/{self.workspace}/notification-preferences'

    def test_read_uses_authenticated_user_and_workspace_membership(self):
        with patch.object(prefs,'read',return_value=prefs.DEFAULTS) as read:
            self.assertEqual(self.client.get(self.url).status_code,200)
            read.assert_called_once_with(self.db,self.workspace,self.actor.user_id)
        self.assertEqual(self.transactions,[(self.actor,{'snapshot':True})])
        self.accounts._workspace.assert_called_once_with(self.db,self.actor,self.workspace)

    def test_write_requires_csrf_and_cannot_choose_another_user(self):
        with patch.object(prefs,'save',return_value=prefs.DEFAULTS) as save:
            self.assertEqual(self.client.patch(self.url,json={'volume':70}).status_code,403)
            save.assert_not_called()
            headers={'X-CSRF-Token':csrf_token(self.actor.raw_session)}
            self.assertEqual(self.client.patch(self.url,headers=headers,json={'user_id':str(uuid4())}).status_code,422)
            self.assertEqual(self.client.patch(self.url,headers=headers,json={'volume':70}).status_code,200)
            save.assert_called_once_with(self.db,self.workspace,self.actor.user_id,{'volume':70})

    def test_service_status_requires_an_administrator_and_never_calls_a_provider(self):
        with patch.object(prefs,'status',return_value={'crm':None,'max':None}) as status:
            response=self.client.get(f'/api/workspaces/{self.workspace}/notification-status')
            self.assertEqual(response.status_code,200)
            status.assert_called_once_with(self.db,self.workspace)
            self.accounts._workspace.assert_called_once_with(self.db,self.actor,self.workspace,manage=True)
            self.accounts._workspace.side_effect=AuthError(403,'Нужны права администратора.')
            status.reset_mock()
            self.assertEqual(self.client.get(f'/api/workspaces/{self.workspace}/notification-status').status_code,403)
            status.assert_not_called()

    def test_inactive_or_foreign_membership_cannot_read_or_write(self):
        self.accounts._workspace.side_effect=AuthError(404,'Организация недоступна.')
        with patch.object(prefs,'read') as read,patch.object(prefs,'save') as save:
            self.assertEqual(self.client.get(self.url).status_code,404)
            self.assertEqual(self.client.patch(self.url,headers={'X-CSRF-Token':csrf_token(self.actor.raw_session)},json={'task_reminders':False}).status_code,404)
            read.assert_not_called();save.assert_not_called()

if __name__=='__main__':unittest.main()
