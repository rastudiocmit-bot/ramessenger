"""Secrets, permissions, stale checks and HTTP contracts; no live Novofon calls."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.auth_security import AuthError, csrf_token
from center_messenger.connector_vault import ConnectorVault
from center_messenger.novofon_service import Novofon
from test_novofon_diagnostic import FakeHTTP, TOKEN, PHONE, URL


class Result:
    def __init__(self, row): self.row = row
    def mappings(self): return self
    def one_or_none(self): return dict(self.row) if self.row else None


class MemoryDB:
    def __init__(self): self.rows, self.events = {}, []
    def execute(self, sql, data=None):
        sql, p = str(sql), data or {}
        if sql.startswith('SELECT * FROM novofon_settings'):
            return Result(self.rows.get(p['w']))
        if sql.startswith('INSERT INTO auth_events'):
            self.events.append(dict(p)); return Result(None)
        if sql.startswith('INSERT INTO novofon_settings'):
            self.rows[p['w']] = {'connection_id':p['id'], 'key_ciphertext':p['c'], 'sample_hours':p['h'],
                'version':1, 'status':'saved', 'checked_at':None, 'check_token':None, 'check_until':None, 'report':{}}
            return Result({'version':1})
        row = self.rows[p['w']]
        if "key_ciphertext=:c" in sql:
            row.update(key_ciphertext=p['c'], sample_hours=p['h'], version=row['version']+1,
                       status='saved',checked_at=None,check_token=None,check_until=None,report={})
        elif "key_ciphertext=NULL" in sql:
            row.update(key_ciphertext=None,version=row['version']+1,status='not_configured',
                       checked_at=None,check_token=None,check_until=None,report={})
        elif "status='checking'" in sql:
            row.update(status='checking',check_token=p['t'],check_until=p['until'])
        elif 'status=:s' in sql:
            row.update(status=p['s'],report=json.loads(p['r']),checked_at=datetime.now(timezone.utc),
                       check_token=None,check_until=None)
        else: raise AssertionError('Unexpected SQL operation')
        return Result(None)


class Accounts:
    def __init__(self, db, workspace, role='owner'):
        self.db, self.workspace, self.role, self.depth = db, workspace, role, 0
    @contextmanager
    def transaction(self, actor, **kwargs):
        self.depth += 1
        try: yield self.db
        finally: self.depth -= 1
    def _workspace(self, db, actor, workspace, *, manage=False):
        if workspace != self.workspace: raise AuthError(404, 'Организация недоступна.')
        if manage and self.role not in ('owner','admin'): raise AuthError(403,'Нужны права администратора.')
    def throttle(self, *args, **kwargs): pass


class NovofonServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        key = Path(self.temp.name)/'master'; key.write_text(secrets.token_urlsafe(48))
        self.vault = ConnectorVault(key)
        self.workspace = uuid4(); self.actor = SimpleNamespace(user_id=uuid4())
        self.db = MemoryDB(); self.accounts = Accounts(self.db,self.workspace)
        self.service = Novofon(self.accounts,self.vault,lambda key: __import__('center_messenger.novofon_client',fromlist=['Client']).Client(key,FakeHTTP()))
    def save(self, key=TOKEN, version=0, hours=24):
        return self.service.save(self.actor,self.workspace,{'api_key':key,'expected_version':version,'sample_hours':hours})

    def test_default_settings_are_useful_without_a_saved_key(self):
        result=self.service.settings(self.actor,self.workspace)
        self.assertEqual((result['sample_hours'],result['version'],result['configured']),(24,0,False))
        self.assertFalse(result['browser_call_ready'])

    def test_key_is_encrypted_bound_to_tenant_and_never_returned(self):
        public=self.save(); row=self.db.rows[self.workspace]
        self.assertNotIn(TOKEN,row['key_ciphertext'])
        self.assertNotIn(TOKEN,str(public))
        self.assertNotIn('key_ciphertext',public)
        key=self.vault.open(self.workspace,row['connection_id'],row['key_ciphertext'],purpose='novofon')
        self.assertEqual(key['api_key'],TOKEN)
        with self.assertRaises(AuthError): self.vault.open(uuid4(),row['connection_id'],row['key_ciphertext'],purpose='novofon')
        with self.assertRaises(AuthError): self.vault.open(self.workspace,row['connection_id'],row['key_ciphertext'],purpose='alfa')

    def test_empty_key_preserves_saved_secret_and_settings_remain_editable(self):
        self.save(); cipher=self.db.rows[self.workspace]['key_ciphertext']
        result=self.save(key=None,version=1,hours=72)
        self.assertEqual(self.db.rows[self.workspace]['key_ciphertext'],cipher)
        self.assertEqual((result['sample_hours'],result['version']),(72,2))

    def test_first_save_requires_a_real_key(self):
        with self.assertRaises(AuthError): self.save(key=None)
        self.assertEqual(self.db.rows,{})

    def test_two_admins_cannot_silently_overwrite_each_other(self):
        self.save()
        with self.assertRaises(AuthError) as e: self.save(version=0,key='other-synthetic-key')
        self.assertEqual(e.exception.status,409)
        self.assertEqual(self.db.rows[self.workspace]['version'],1)

    def test_operator_and_foreign_workspace_cannot_read_or_change_settings(self):
        self.save(); self.accounts.role='operator'
        for action in (lambda:self.service.settings(self.actor,self.workspace),lambda:self.save(version=1),
                       lambda:self.service.verify(self.actor,self.workspace,1),lambda:self.service.disconnect(self.actor,self.workspace,1)):
            with self.assertRaises(AuthError) as e: action()
            self.assertEqual(e.exception.status,403)
        self.accounts.role='owner'
        with self.assertRaises(AuthError) as e: self.service.settings(self.actor,uuid4())
        self.assertEqual(e.exception.status,404)

    def test_diagnostic_does_not_hold_a_transaction_or_save_contacts(self):
        self.save()
        def client(key):
            self.assertEqual(self.accounts.depth,0)
            from center_messenger.novofon_client import Client
            return Client(key,FakeHTTP())
        self.service.client_factory=client
        result=self.service.verify(self.actor,self.workspace,1,True)
        self.assertEqual(result['status'],'api_verified')
        self.assertFalse(result['browser_call_ready'])
        for value in (TOKEN,PHONE,URL,'phone_tag','calls'):
            self.assertNotIn(value,json.dumps(result,default=str))
        self.assertNotIn(TOKEN,str(self.db.events))

    def test_removing_key_while_check_is_running_cannot_resurrect_it(self):
        self.save()
        def client(key):
            self.service.disconnect(self.actor,self.workspace,1)
            from center_messenger.novofon_client import Client
            return Client(key,FakeHTTP())
        self.service.client_factory=client
        with self.assertRaises(AuthError) as e: self.service.verify(self.actor,self.workspace,1)
        self.assertEqual(e.exception.status,409)
        self.assertFalse(self.service.settings(self.actor,self.workspace)['configured'])

    def test_temporary_network_failure_preserves_key_and_retry_is_possible(self):
        self.save(); self.service.client_factory=Mock(side_effect=RuntimeError(TOKEN))
        with self.assertLogs('center_messenger.novofon_service',level='WARNING') as output:
            result=self.service.verify(self.actor,self.workspace,1)
        self.assertTrue(result['configured']); self.assertEqual(result['status'],'error')
        self.assertNotIn(TOKEN,str(output.output))
        self.assertIsNone(self.db.rows[self.workspace]['check_until'])

    def test_duplicate_check_is_rejected_and_worker_slot_is_released(self):
        self.save()
        self.db.rows[self.workspace]['check_until']=datetime.max.replace(tzinfo=timezone.utc)
        with self.assertRaises(AuthError) as e: self.service.verify(self.actor,self.workspace,1)
        self.assertEqual(e.exception.status,409)
        self.db.rows[self.workspace]['check_until']=None
        self.assertEqual(self.service.verify(self.actor,self.workspace,1)['status'],'api_verified')


class NovofonHttpTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from center_messenger.main import create_app
        from center_messenger.config import Settings
        config=Settings(public_url='http://testserver')
        self.app=create_app(config); self.actor=SimpleNamespace(user_id=uuid4(),raw_session='synthetic-session')
        self.app.state.accounts=SimpleNamespace(settings=SimpleNamespace(cookie_name='session'),authenticate=lambda _:self.actor)
        self.app.state.novofon=Mock()
        self.app.state.novofon.save.return_value={'configured':True,'version':1}
        self.client=TestClient(self.app)
        self.client.headers['origin']=config.public_url
        self.path='/api/workspaces/'+str(uuid4())+'/integrations/novofon'
        self.headers={'x-csrf-token':csrf_token(self.actor.raw_session)}

    def test_mutation_requires_csrf_and_does_not_echo_key(self):
        result=self.client.put(self.path,json={'expected_version':0,'api_key':TOKEN})
        self.assertEqual(result.status_code,403); self.assertNotIn(TOKEN,result.text)
        self.app.state.novofon.save.assert_not_called()

    def test_valid_save_passes_unmasked_key_only_to_server_service(self):
        result=self.client.put(self.path,headers=self.headers,json={'expected_version':0,'api_key':TOKEN})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.app.state.novofon.save.call_args.args[2]['api_key'],TOKEN)
        self.assertNotIn(TOKEN,result.text)

    def test_validation_rejects_boolean_versions_and_never_echoes_input(self):
        result=self.client.put(self.path,headers=self.headers,json={'expected_version':True,'api_key':TOKEN,'sample_hours':0})
        self.assertEqual(result.status_code,422); self.assertNotIn(TOKEN,result.text)


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class NovofonDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_message_sync import MessageSyncTests
        MessageSyncTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        from test_message_sync import MessageSyncTests
        MessageSyncTests.tearDownClass.__func__(cls)
    def setUp(self):
        from sqlalchemy import text
        self.transaction = self.db.begin()
        self.workspace, self.user = uuid4(), uuid4()
        self.db.execute(text("INSERT INTO workspaces(id,name) VALUES(:w,'Novofon test')"), {'w':self.workspace})
        self.db.execute(text("INSERT INTO auth_users(id,email,display_name,password_hash) VALUES(:u,:e,'Admin','unused')"),
                        {'u':self.user,'e':str(self.user)+'@example.test'})
    def tearDown(self):
        from test_message_sync import MessageSyncTests
        MessageSyncTests.tearDown(self)

    def test_real_migration_enforces_rls_for_workspace_and_admin(self):
        from sqlalchemy import text
        other=uuid4()
        self.db.execute(text("INSERT INTO novofon_settings(workspace_id,connection_id,key_ciphertext) VALUES(:w,:id,'encrypted')"),
                        {'w':self.workspace,'id':uuid4()})
        self.db.execute(text('GRANT USAGE ON SCHEMA "'+self.schema+'" TO cm_app'))
        self.db.execute(text('SET LOCAL ROLE cm_app'))
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true),set_config('app.membership_admin','true',true)"),{'w':str(self.workspace)})
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM novofon_settings')).scalar_one(),1)
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(other)})
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM novofon_settings')).scalar_one(),0)
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true),set_config('app.membership_admin','false',true)"),{'w':str(self.workspace)})
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM novofon_settings')).scalar_one(),0)

    def test_downgrade_and_reupgrade_preserve_saved_ciphertext(self):
        import importlib.util
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import text
        from test_message_sync import ROOT
        self.db.execute(text("INSERT INTO novofon_settings(workspace_id,connection_id,key_ciphertext) VALUES(:w,:id,'encrypted-retained')"),
                        {'w':self.workspace,'id':uuid4()})
        path=ROOT/'app/backend/migrations/versions/0030_novofon_settings.py'
        spec=importlib.util.spec_from_file_location('novofon_migration_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(self.db)):
            module.downgrade();module.upgrade()
        value=self.db.execute(text('SELECT key_ciphertext FROM novofon_settings WHERE workspace_id=:w'),{'w':self.workspace}).scalar_one()
        self.assertEqual(value,'encrypted-retained')


if __name__=='__main__': unittest.main()
