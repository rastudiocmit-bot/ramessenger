"""Exact-source archive recovery is separate from same-phone family aliases."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import json

from sqlalchemy.dialects import postgresql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app/backend/src'))
from center_messenger import native_archive_relink as archive


class ArchiveRelinkTests(unittest.TestCase):
    def test_database_fixture_binds_json_without_interpreting_true_as_a_sql_parameter(self):
        from test_native_archive_database import ArchiveDatabaseTests
        fixture = ArchiveDatabaseTests()
        fixture.workspace, fixture.run, fixture.contact, fixture.channel = [uuid4() for _ in range(4)]
        fixture.at = datetime.now(UTC)
        fixture.db = Mock()
        fixture.db.execute.return_value.scalar_one.side_effect = [uuid4(), uuid4()]
        fixture.add_customer = fixture.customer_phone = fixture.project = Mock()
        with patch('test_native_archive_database.refresh_unread'):
            fixture.archived_source_fixture()
        json_values = []
        for call in fixture.db.execute.call_args_list:
            statement, values = call.args
            compiled = statement.compile(dialect=postgresql.dialect())
            compiled.construct_params(params=values)
            self.assertNotIn('true', compiled.params)
            if 'payload' in values:
                json_values.append(json.loads(values['payload']))
        self.assertEqual(json_values, [{'crm_recipient_stale': True}, {'failed': True}])

    def cards(self):
        old = {'customer_id':7,'contact_id':uuid4(),'global_id_verified':True,'status':'archive',
               'primary_phone':None,'available':True,'empty_archive':True}
        new = {'customer_id':8,'contact_id':uuid4(),'global_id_verified':True,'status':'client',
               'primary_phone':'+79001111111','available':True,'empty_archive':False}
        return old,new

    def test_exact_empty_archive_to_active_card_is_allowed_without_a_phone_alias(self):
        self.assertTrue(archive.allowed(*self.cards()))

    def test_nonempty_archive_or_active_old_card_cannot_use_this_recovery(self):
        old,new = self.cards()
        for change in ({'primary_phone':new['primary_phone']},{'status':'client'},
                       {'empty_archive':False},{'global_id_verified':False},{'available':False}):
            with self.subTest(change=change):
                self.assertFalse(archive.allowed(old|change,new))

    def test_inactive_unknown_or_same_contact_target_is_rejected(self):
        old,new = self.cards()
        for change in ({'status':'archive'},{'status':'lead'},{'primary_phone':None},
                       {'global_id_verified':False},{'contact_id':old['contact_id']},{'available':False}):
            with self.subTest(change=change):
                self.assertFalse(archive.allowed(old,new|change))

    def test_all_available_old_branches_must_be_empty_archives(self):
        old,_ = self.cards()
        row = {'external_id':7,'branch_id':1,'contact_id':old['contact_id'],'status':'archive',
               'primary_phone':None,'available':True}
        directory = {7:[row,row|{'branch_id':2,'primary_phone':'+79001111111'}]}
        self.assertFalse(archive.current_card(7,1,directory)['empty_archive'])
        directory[7][1] = row|{'branch_id':2,'status':'client'}
        self.assertFalse(archive.current_card(7,1,directory)['empty_archive'])

    def test_a_single_current_global_customer_observation_is_required(self):
        source = {'branch_id':4,'customer_id':8,'crm_observed_customers':[[4,8]],'crm_recipient_stale':False}
        self.assertTrue(archive.observation(source,8))
        for change in ({'crm_observed_customers':None},{'crm_observed_customers':[[4,8],[4,9]]},
                       {'crm_observed_customers':[[3,8]]},{'crm_recipient_stale':True},{'customer_id':9}):
            self.assertFalse(archive.observation(source|change,8))

    def test_same_time_or_body_cannot_substitute_for_exact_source_and_peer(self):
        old = {'payload':{'branch_id':4,'crm_record_id':'52'},'occurred_at':datetime.now(UTC)}
        new = {'payload':dict(old['payload']),'occurred_at':old['occurred_at']}
        self.assertTrue(archive.same_source(old,new))
        for change in ({'branch_id':3},{'crm_record_id':'53'}):
            self.assertFalse(archive.same_source(old,new|{'payload':new['payload']|change}))
        self.assertFalse(archive.same_source(old,new|{'occurred_at':old['occurred_at']+timedelta(seconds=1)}))
        self.assertFalse(archive.same_source(old|{'payload':old['payload']|{'max_user_id':'77'}},
            new|{'payload':new['payload']|{'max_user_id':'78'}}))

    def test_multiple_replacement_projections_are_not_chosen_arbitrarily(self):
        row = {'id':uuid4(),'occurred_at':datetime.now(UTC),
               'payload':{'branch_id':4,'crm_record_id':'52','customer_id':7,'customer_branch_id':1}}
        db = Mock()
        db.execute.return_value.mappings.return_value = [{},{}]
        self.assertIsNone(archive.counterpart(db,uuid4(),uuid4(),row))

    def test_incomplete_legacy_metadata_is_skipped_without_blocking_the_history_worker(self):
        db = Mock()
        self.assertIsNone(archive.counterpart(db,uuid4(),uuid4(),{'payload':{'customer_id':7}}))
        db.execute.assert_not_called()

    def test_changed_settings_reject_cached_redirect_before_any_catalog_or_message_reads(self):
        contact,crm = uuid4(),uuid4()
        proof = {'version':1,'old_contact':str(contact),'crm_connection':str(crm),'settings_version':1}
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = proof
        with patch.object(archive,'customer_directory') as lookup:
            self.assertIsNone(archive.canonical_alias(db,uuid4(),uuid4(),{'version':2,'connection_id':crm},contact))
        lookup.assert_not_called()

    def test_invalid_redirect_identifiers_fail_closed(self):
        contact,crm = uuid4(),uuid4()
        proof = {'version':1,'old_contact':str(contact),'crm_connection':str(crm),'settings_version':1,
                 'old_customer':True,'new_customer':8,'new_branch':1}
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = proof
        self.assertIsNone(archive.canonical_alias(db,uuid4(),uuid4(),{'version':1,'connection_id':crm},contact))

    def test_manual_conversation_is_not_changed(self):
        db,service = Mock(),Mock()
        db.execute.return_value.mappings.return_value = [{'crm_link_manual':True}]
        self.assertFalse(archive.recover(db,service,uuid4(),uuid4(),{},uuid4()))
        service._supersede.assert_not_called()
        service._merge_contact_conversations.assert_not_called()

    def test_incomplete_source_receipt_fails_closed_without_catalog_reads(self):
        contact,crm = uuid4(),uuid4()
        proof = {'version':1,'old_contact':str(contact),'crm_connection':str(crm),'settings_version':1,
                 'old_customer':7,'new_customer':8,'new_branch':1,'old_conversation':str(uuid4()),
                 'canonical':str(uuid4()),'old_message':str(uuid4()),'new_message':str(uuid4())}
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = proof
        with patch.object(archive,'customer_directory') as lookup:
            self.assertIsNone(archive.canonical_alias(db,uuid4(),uuid4(),{'version':1,'connection_id':crm},contact))
        lookup.assert_not_called()

    def test_bounded_repair_yields_between_scans_and_restarts_after_new_publication(self):
        snapshot = {'run':uuid4(),'version':3}
        cursor = {'archived_source_repair': {'generation':[str(snapshot['run']),3],
            'next_at':(datetime.now(UTC)+timedelta(minutes=1)).isoformat()}}
        db = Mock()
        self.assertFalse(archive.repair_batch(Mock(),db,uuid4(),uuid4(),snapshot,cursor))
        db.execute.assert_not_called()
        db.execute.return_value.scalars.return_value = []
        self.assertTrue(archive.repair_batch(Mock(),db,uuid4(),uuid4(),snapshot|{'run':uuid4()},cursor))
        self.assertIsNone(cursor['archived_source_repair']['after'])


if __name__ == '__main__':
    unittest.main()
