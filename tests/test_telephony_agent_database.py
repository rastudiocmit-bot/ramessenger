"""Dedicated-agent JSON persistence and tenant checks in isolated PostgreSQL."""
from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import text
from center_messenger.auth_security import AuthError
from center_messenger.telephony_agent import TelephonyAgent, defaults
import test_novofon_connection as fixture


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class TelephonyAgentDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.NovofonDatabaseTests.setUpClass.__func__)
    tearDownClass=classmethod(fixture.NovofonDatabaseTests.tearDownClass.__func__)
    tearDown=fixture.NovofonDatabaseTests.tearDown

    def setUp(self):
        fixture.NovofonDatabaseTests.setUp(self)
        self.actor=SimpleNamespace(user_id=self.user)
        @contextmanager
        def transaction(actor,**_):
            with self.db.begin_nested(): yield self.db
        def workspace(db,actor,workspace,**_):
            if workspace!=self.workspace: raise AuthError(404,'Not found')
        accounts=SimpleNamespace(transaction=transaction,_workspace=workspace)
        self.service=TelephonyAgent(accounts,Mock())

    def save(self,version=0,**changes):
        data={k:v for k,v in defaults(self.workspace).items() if k in ('name','model','prompt','recognition_provider','temperature')}
        return self.service.save(self.actor,self.workspace,data | {'expected_version':version} | changes)

    def test_agent_versions_and_other_gateway_settings_survive_reopening_service(self):
        from center_messenger.ai_gateways import Gateways
        self.db.execute(text("INSERT INTO ai_settings(workspace_id,gateway_config) VALUES (:w,'{\"recognition_provider\":\"mimo\"}'::jsonb)"),{'w':self.workspace})
        one=self.save(); two=self.save(1,prompt='Только факты звонка.')
        # A later ordinary gateway route save reads and writes the full config;
        # it must retain the independently versioned telephone agent.
        gateway_service=SimpleNamespace(accounts=self.service.accounts,_settings=self.service._settings,
            _public=lambda db,w:None)
        Gateways(gateway_service).routes(self.actor,self.workspace,{'recognition_provider':'yandex',
            'recognition_language':'ru','synthesis_provider':'disabled','synthesis_voice':'Mia'})
        result=TelephonyAgent(self.service.accounts,Mock()).settings(self.actor,self.workspace)
        self.assertEqual(result['version'],2); self.assertEqual(result['history'][0]['version'],1)
        self.assertEqual(result['history'][0]['prompt'],one['prompt'])
        self.assertEqual(result['prompt'],two['prompt'])
        self.assertEqual(self.db.execute(text("SELECT gateway_config->>'recognition_provider' FROM ai_settings WHERE workspace_id=:w"),{'w':self.workspace}).scalar_one(),'yandex')
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM ai_agents')).scalar_one(),0)

    def test_a_stale_admin_save_is_rejected_without_overwriting_the_saved_prompt(self):
        self.save(); self.save(1,prompt='Актуальная инструкция')
        with self.assertRaises(AuthError) as error: self.save(1,prompt='Устаревшая инструкция')
        self.assertEqual(error.exception.status,409)
        value=self.service.settings(self.actor,self.workspace)
        self.assertEqual((value['version'],value['prompt']),(2,'Актуальная инструкция'))

    def test_foreign_workspace_cannot_read_the_dedicated_agent_namespace(self):
        self.save(); self.db.execute(text('GRANT USAGE ON SCHEMA "'+self.schema+'" TO cm_app'))
        self.db.execute(text('SET LOCAL ROLE cm_app'))
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(self.workspace)})
        self.assertEqual(self.db.execute(text("SELECT gateway_config->'telephony_agent'->>'name' FROM ai_settings")).scalar_one(),'Телефонные разговоры')
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(uuid4())})
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM ai_settings')).scalar_one(),0)
