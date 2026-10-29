"""Transactional regression cases, run on an isolated PostgreSQL database."""
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import text
import test_message_sync as fixture
from center_messenger import card_loader_settings as settings, card_queue, family_cache, family_snapshots


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'), 'isolated PostgreSQL URL required')
class CardLoaderDurabilityTests(unittest.TestCase):
    # Reuse setup/helpers, not the base class's hundreds of unrelated test cases.
    for name, method in fixture.MessageSyncTests.__dict__.items():
        if not name.startswith('test') and not name.startswith('__'):
            locals()[name] = method

    def policy(self, **values):
        value = settings.CardLoaderSettings(**values)
        settings.save(self.db, self.workspace, value)
        return value.model_dump()

    def open_job(self, worker, contact=None):
        family_cache.enqueue(self.db, self.workspace, self.snapshot, contact=contact or self.contact,
                             topics=(family_cache.TOPIC,))
        return worker.claim(self.workspace)

    def seed_complete(self, reader, student):
        self.db.execute(text("INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,customer_id,"
            "settings_version,balance,subscriptions,lessons,lessons_refreshed_at,lessons_full_refreshed_at) "
            "VALUES(:w,:c,1,7,1,100,'[]','[]',clock_timestamp(),clock_timestamp())"), {'w':self.workspace,'c':self.crm})
        self.db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,state,payload) "
            "VALUES(:w,:id,'crm.customer.groups',:key,'published',CAST(:p AS jsonb))"),
            {'w':self.workspace,'id':uuid4(),'key':f'{self.crm}:1:7','p':json.dumps({
                'version':1,'student':str(student),'contact':str(self.contact),'checked_run':str(self.run),
                'refreshed_at':datetime.now(UTC).isoformat()})})
        self.assertTrue(family_snapshots.complete(reader._detail(self.db,self.workspace,self.contact,publish_snapshot=True)))

    def test_full_snapshot_hides_staging_and_replaces_all_parts_atomically(self):
        reader, _, student = self.family_worker_fixture(include_groups=True)
        self.seed_complete(reader,student)
        old = reader.detail(None,self.workspace,self.contact)
        self.assertEqual(old['students'][0]['balance'],'100.00')
        self.assertTrue(old['cache']['snapshot']['complete'])
        self.db.execute(text("UPDATE alfa_family_details SET balance=200,lessons_refreshed_at=NULL WHERE workspace_id=:w"), {'w':self.workspace})
        staging = reader._detail(self.db,self.workspace,self.contact,publish_snapshot=True)
        self.assertFalse(family_snapshots.complete(staging))
        self.assertEqual(reader.detail(None,self.workspace,self.contact)['students'][0]['balance'],'100.00')
        self.db.execute(text("UPDATE alfa_family_details SET lessons_refreshed_at=clock_timestamp() WHERE workspace_id=:w"), {'w':self.workspace})
        reader._detail(self.db,self.workspace,self.contact,publish_snapshot=True)
        self.assertEqual(reader.detail(None,self.workspace,self.contact)['students'][0]['balance'],'200.00')

    def test_worker_finishes_all_sections_before_publishing_the_next_snapshot(self):
        reader, worker, student = self.family_worker_fixture(include_groups=True)
        self.seed_complete(reader,student)
        family_cache.enqueue(self.db,self.workspace,self.snapshot,contact=self.contact,force=True)
        with patch('center_messenger.family_cache.fetch_family_finance',return_value={
            'balance':'200','paid_lesson_count':0,'subscriptions':[]}), \
             patch('center_messenger.family_cache.fetch_family_lessons',return_value=[]), \
             patch('center_messenger.group_links.fetch',return_value=({},[])):
            first=worker.claim(self.workspace);worker.process(self.workspace,first)
            self.assertEqual(reader.detail(None,self.workspace,self.contact)['students'][0]['balance'],'100.00')
            for _ in range(6):
                job=worker.claim(self.workspace)
                if job is None: break
                worker.process(self.workspace,job)
        self.assertEqual(reader.detail(None,self.workspace,self.contact)['students'][0]['balance'],'200.00')

    def test_pending_send_pauses_background_but_preserves_manual_and_checkpoint(self):
        from center_messenger.messaging import Messaging
        reader, worker, _ = self.family_worker_fixture()
        self.policy(preload_active_cards=True)
        self.project(self.row());self.local_reply(self.conversation()['id'])
        command=self.db.execute(text("UPDATE send_commands SET state='queued' WHERE workspace_id=:w RETURNING id"),{'w':self.workspace}).scalar_one()
        Messaging.enqueue(self.db,self.workspace,self.channel,command,'send','alfa_max_browser')
        family_cache.enqueue(self.db,self.workspace,self.snapshot,topics=(family_cache.TOPIC,))
        self.assertIsNone(worker.claim(self.workspace))
        self.assertEqual(reader.detail(None,self.workspace,self.contact)['cache']['state'],'retry_wait')
        job=self.open_job(worker)
        self.assertIsNotNone(job)
        self.assertTrue(card_queue.urgent(job))

    def test_wait_target_is_visible_without_faking_an_active_lease(self):
        reader, worker, _ = self.family_worker_fixture()
        self.open_job(worker)
        self.db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until')||jsonb_build_object("
            "'requested_at',clock_timestamp()-interval '70 seconds') WHERE workspace_id=:w"),{'w':self.workspace})
        status=reader.detail(None,self.workspace,self.contact)['cache']
        self.assertTrue(status['wait_exceeded']);self.assertFalse(status['loading'])

    def test_replaced_customer_or_student_cannot_read_the_previous_full_snapshot(self):
        reader, _, student = self.family_worker_fixture(include_groups=True)
        self.seed_complete(reader,student)
        self.db.execute(text("UPDATE alfa_customers SET external_id=8 WHERE workspace_id=:w AND external_id=7"), {'w':self.workspace})
        fresh=reader.detail(None,self.workspace,self.contact)
        self.assertIsNone(fresh['cache']['snapshot'])
        self.assertEqual(fresh['students'][0]['cards'][0]['id'],'8')

    def test_active_background_interval_and_lead_archive_exclusion(self):
        reader, worker, student = self.family_worker_fixture(include_groups=True)
        self.policy(preload_active_cards=True,active_interval_minutes=720)
        family_cache.enqueue(self.db,self.workspace,self.snapshot,topics=(family_cache.TOPIC,))
        self.assertIsNotNone(worker.claim(self.workspace))
        self.db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w"), {'w':self.workspace})
        self.seed_complete(reader,student)
        self.db.execute(text("UPDATE alfa_family_details SET refreshed_at=clock_timestamp()-interval '2 hours' WHERE workspace_id=:w"), {'w':self.workspace})
        self.assertEqual(family_cache.enqueue(self.db,self.workspace,self.snapshot,topics=(family_cache.TOPIC,)),0)
        self.db.execute(text("UPDATE alfa_family_details SET refreshed_at=clock_timestamp()-interval '13 hours' WHERE workspace_id=:w"), {'w':self.workspace})
        self.assertEqual(family_cache.enqueue(self.db,self.workspace,self.snapshot,topics=(family_cache.TOPIC,)),1)
        self.db.execute(text("DELETE FROM outbox_events WHERE workspace_id=:w"), {'w':self.workspace})
        self.db.execute(text("UPDATE alfa_customers SET status='archive' WHERE workspace_id=:w"), {'w':self.workspace})
        self.assertEqual(family_cache.enqueue(self.db,self.workspace,self.snapshot,topics=(family_cache.TOPIC,)),0)

    def test_parallel_limit_and_unique_claim_tokens_are_shared_by_workers(self):
        _, worker, _ = self.family_worker_fixture()
        self.policy(parallel_card_jobs=2)
        other=uuid4();self.add_customer(other,1,8)
        self.db.execute(text("UPDATE alfa_customers SET student_id=(SELECT student_id FROM alfa_customers WHERE workspace_id=:w AND external_id=7) WHERE workspace_id=:w"),{'w':self.workspace})
        first=self.open_job(worker)
        second_worker=family_cache.FamilyCacheWorker(worker.connections)
        second=self.open_job(second_worker,other)
        self.assertIsNotNone(second);self.assertNotEqual(first['lease_owner'],second['lease_owner'])
        self.assertIsNone(worker.claim(self.workspace))
        self.policy(parallel_card_jobs=1)
        worker.yield_job(self.workspace,first)
        self.assertIsNone(worker.claim(self.workspace))

    def test_visible_queue_order_uses_claim_rules_and_skips_retry_wait(self):
        reader, worker, _ = self.family_worker_fixture(include_groups=True)
        self.policy(preload_active_cards=False)
        family_cache.enqueue(self.db,self.workspace,self.snapshot,contact=self.contact)
        status=reader.detail(None,self.workspace,self.contact)['cache']
        first=worker.claim(self.workspace)
        labels={family_cache.TOPIC:'finance',family_cache.LESSONS:'lessons',family_cache.HISTORY:'history',family_cache.GROUPS:'groups'}
        self.assertEqual(status['sections'][labels[first['topic']]]['queue_position'],1)
        worker.yield_job(self.workspace,first)
        self.db.execute(text("UPDATE outbox_events SET payload=payload||jsonb_build_object('available_at',clock_timestamp()+interval '1 hour','error','crm_unavailable') WHERE id=:id"),{'id':first['id']})
        status=reader.detail(None,self.workspace,self.contact)['cache']
        self.assertIsNone(status['sections'][labels[first['topic']]]['queue_position'])

    def test_expired_manual_interest_loses_priority_without_losing_the_request(self):
        _, worker, _ = self.family_worker_fixture()
        self.open_job(worker)
        self.db.execute(text("UPDATE outbox_events SET payload=(payload-'lease_owner'-'lease_until')||jsonb_build_object('manual_until',clock_timestamp()-interval '1 minute') WHERE workspace_id=:w"),{'w':self.workspace})
        other=uuid4();self.add_customer(other,1,8)
        self.db.execute(text("UPDATE alfa_customers SET student_id=(SELECT student_id FROM alfa_customers WHERE workspace_id=:w AND external_id=7) WHERE workspace_id=:w"),{'w':self.workspace})
        next_job=self.open_job(worker,other)
        self.assertEqual(next_job['customer'],'8')
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM outbox_events WHERE workspace_id=:w AND state='pending'"),{'w':self.workspace}).scalar_one(),2)

    def test_rolling_quota_persists_across_new_gate_and_defers_without_dropping_progress(self):
        _, worker, _ = self.family_worker_fixture()
        policy=self.policy(requests_per_minute=6,pause_background_on_send=False)
        job=self.open_job(worker)
        for _ in range(6): card_queue.request_gate(self.accounts,self.workspace,policy,job,lambda:None)()
        with self.assertRaises(card_queue.Deferred) as error:
            card_queue.request_gate(self.accounts,self.workspace,policy,job,lambda:None)()
        self.assertEqual(error.exception.code,'quota_wait')
        worker.retry(self.workspace,job,error=error.exception)
        payload=self.db.execute(text("SELECT payload FROM outbox_events WHERE id=:id"),{'id':job['id']}).scalar_one()
        self.assertEqual(payload['attempts'],0);self.assertNotIn('lease_owner',payload)
        self.assertGreater(datetime.fromisoformat(payload['available_at']),datetime.now(UTC))

    def test_new_tables_have_runtime_grants_and_tenant_isolation(self):
        self.family_worker_fixture()
        self.policy()
        with self.db.begin_nested():
            self.db.execute(text(f'GRANT USAGE ON SCHEMA "{self.schema}" TO cm_app'))
            self.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"),{'w':str(self.workspace)})
            self.db.execute(text('SET LOCAL ROLE cm_app'))
            card_queue.state(self.db,self.workspace,lock=True)
            self.assertEqual(self.db.execute(text('SELECT count(*) FROM crm_family_snapshots')).scalar_one(),0)
            with self.assertRaises(Exception):
                with self.db.begin_nested():
                    card_queue.state(self.db,uuid4(),lock=True)
