"""Real shared cm_app counters and concurrent reservations in isolated PG."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from uuid import uuid4

from sqlalchemy import text
from center_messenger.novofon_budget import PostgresStore
from center_messenger.novofon_quota import Budget, QuotaWait, SLOTS, digest
import test_novofon_connection as fixture


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'),'isolated PostgreSQL URL required')
class QuotaDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.NovofonDatabaseTests.setUpClass.__func__(cls)
        cls.db.execute(text('GRANT USAGE ON SCHEMA "'+cls.schema+'" TO cm_app'))
        cls.db.commit()

    tearDownClass=classmethod(fixture.NovofonDatabaseTests.tearDownClass.__func__)

    def setUp(self):
        self.token='synthetic-'+uuid4().hex
        self.scope=digest('token:'+self.token)
        @contextmanager
        def begin():
            with self.engine.begin() as db:
                db.execute(text('SET LOCAL search_path TO "'+self.schema+'",public'))
                db.execute(text('SET LOCAL ROLE cm_app'))
                yield db
        self.store=PostgresStore(SimpleNamespace(begin=begin))

    def tearDown(self):
        self.db.rollback()
        keys=[digest(self.scope+':'+slot) for slot in SLOTS]
        self.db.execute(text('DELETE FROM auth_throttles WHERE bucket_hash=ANY(CAST(:keys AS text[]))'),{'keys':keys})
        self.db.commit()

    def budget(self):return Budget(self.token,self.store)

    def test_committed_zero_balance_survives_new_client_instances_and_role_contexts(self):
        self.budget().observe('get.account',{'limits':{'day_limit':100,'day_remaining':0,'day_reset':3000}})
        with self.assertRaises(QuotaWait) as wait:self.budget().reserve('get.calls_report')
        self.assertGreaterEqual(wait.exception.retry_after,2999)
        row=self.db.execute(text('SELECT attempts FROM auth_throttles WHERE bucket_hash=:k'),
                            {'k':digest(self.scope+':day_left')}).scalar_one()
        self.assertEqual(row,1)

    def test_parallel_transactions_cannot_overdraw_the_shared_minute_balance(self):
        self.budget().observe('get.account',{'limits':{'minute_limit':3,'minute_remaining':3,'minute_reset':55,
                                                    'day_limit':1000,'day_remaining':1000,'day_reset':4000}})
        def reserve(_):
            try:self.budget().reserve('get.calls_report');return True
            except QuotaWait:return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(reserve,range(12))),3)

    def test_a_login_counter_with_a_different_hash_is_never_modified(self):
        key=digest(self.scope+':login-fixture')
        try:
            self.db.execute(text('INSERT INTO auth_throttles(bucket_hash,attempts) VALUES(:k,7)'),{'k':key})
            self.db.commit()
            self.budget().reserve('get.account')
            self.assertEqual(self.db.execute(text('SELECT attempts FROM auth_throttles WHERE bucket_hash=:k'),{'k':key}).scalar_one(),7)
        finally:
            self.db.execute(text('DELETE FROM auth_throttles WHERE bucket_hash=:k'),{'k':key})
            self.db.commit()
