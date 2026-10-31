"""Mandatory release gate against isolated PostgreSQL, never the live database."""
from contextlib import contextmanager
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import text
from center_messenger.telephony_import import TelephonyImport
from center_messenger.telephony_model import normalize
from test_telephony_calls import source,NOW
import test_novofon_connection as fixture


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class TelephonyDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.NovofonDatabaseTests.setUpClass.__func__)
    tearDownClass=classmethod(fixture.NovofonDatabaseTests.tearDownClass.__func__)
    tearDown=fixture.NovofonDatabaseTests.tearDown

    def setUp(self):
        fixture.NovofonDatabaseTests.setUp(self)
        self.connection,self.token=uuid4(),uuid4()
        self.db.execute(text("INSERT INTO novofon_settings(workspace_id,connection_id,key_ciphertext) VALUES(:w,:c,'synthetic-cipher')"),{'w':self.workspace,'c':self.connection})
        TelephonyImport.ensure(self.db,self.workspace)
        self.db.execute(text("UPDATE telephony_routes SET lease_owner=:t,lease_until=now()+interval '1 minute' WHERE workspace_id=:w"),{'w':self.workspace,'t':self.token})
        settings=dict(self.db.execute(text('SELECT * FROM novofon_settings WHERE workspace_id=:w'),{'w':self.workspace}).mappings().one())
        state=dict(self.db.execute(text('SELECT * FROM telephony_state WHERE workspace_id=:w'),{'w':self.workspace}).mappings().one())
        self.job={'workspace':self.workspace,'token':self.token,'settings':settings,'state':state}
        @contextmanager
        def begin():yield self.db
        self.importer=TelephonyImport(SimpleNamespace(engine=SimpleNamespace(begin=begin)),Mock(seal_recording=Mock(return_value='encrypted-url')))

    def publish(self,*rows):
        calls=[normalize(row,workspace=self.workspace,account='17',connection=self.connection,zone='Europe/Moscow') for row in rows]
        self.importer.commit(self.job,'17','Europe/Moscow',{'offset':len(rows)}, {},False,calls,NOW,60,True)

    def lease(self):
        self.db.execute(text("UPDATE telephony_routes SET lease_owner=:t,lease_until=now()+interval '1 minute' WHERE workspace_id=:w"),{'w':self.workspace,'t':self.token})

    def test_repeat_page_is_idempotent_and_distinct_ids_are_not_merged_by_phone(self):
        self.publish(source(),source(id=125))
        self.lease();self.publish(source(),source(id=125))
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),2)
        self.assertEqual(self.db.execute(text('SELECT max(version) FROM telephony_calls')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM messages')).scalar_one(),0)

    def test_changed_credentials_or_lease_owner_cannot_publish_calls_or_advance_cursor(self):
        for stale in ('version','lease'):
            if stale=='version':self.db.execute(text('UPDATE novofon_settings SET version=2'))
            else:
                self.db.execute(text('UPDATE novofon_settings SET version=1'))
                self.db.execute(text('UPDATE telephony_routes SET lease_owner=:t'),{'t':uuid4()})
            self.publish(source())
            self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),0)
            self.assertEqual(self.db.execute(text('SELECT recent FROM telephony_state')).scalar_one(),{})

    def test_late_metadata_updates_keep_an_already_available_recording(self):
        self.publish(source());self.lease()
        self.publish(source(talk_duration=50,call_records=[]))
        row=self.db.execute(text('SELECT talk_seconds,recording_ciphertext,version FROM telephony_calls')).mappings().one()
        self.assertEqual((row['talk_seconds'],row['recording_ciphertext'],row['version']),(50,'encrypted-url',2))

    def test_tenant_and_operator_read_rules_never_expose_settings_keys(self):
        self.publish(source())
        self.db.execute(text(f'GRANT USAGE ON SCHEMA "{self.schema}" TO cm_app'))
        self.db.execute(text('SET LOCAL ROLE cm_app'))
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true),set_config('app.membership_admin','false',true)"),{'w':str(self.workspace)})
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_state')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM novofon_settings')).scalar_one(),0)
        self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(uuid4())})
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),0)

    def test_downgrade_and_reupgrade_preserve_calls_and_progress(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from test_message_sync import ROOT
        self.publish(source())
        path=ROOT/'app/backend/migrations/versions/0031_telephony_calls.py'
        spec=importlib.util.spec_from_file_location('calls_migration_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(self.db)):
            module.downgrade();module.upgrade()
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM telephony_calls')).scalar_one(),1)
        self.assertEqual(self.db.execute(text('SELECT recent FROM telephony_state')).scalar_one(),{'offset':1})
