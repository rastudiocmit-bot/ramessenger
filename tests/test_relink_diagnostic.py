"""Saved relink evidence must not change an address, session or message."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

SPEC = importlib.util.spec_from_file_location('relink_diagnostic',
    Path(__file__).resolve().parents[1]/'scripts/diagnose_max_relink.py')
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


class RelinkDiagnosticTests(unittest.TestCase):
    def message(self, customer, **changes):
        return {'id': uuid4(), 'conversation_id': uuid4(), 'contact_id': uuid4(),
            'occurred_at': datetime(2026,10,30,tzinfo=timezone.utc), 'crm_link_manual': False,
            'payload': {'branch_id': 4, 'crm_record_id': '52', 'customer_id': customer,
                'customer_branch_id': 1, 'crm_customer_ids': [customer],
                'crm_observed_customers': [[4,customer]], **changes}}

    def test_output_whitelists_metadata_and_never_discloses_payload_text_phone_or_peer(self):
        row = self.message(7, text='private-text', phone='+79001111111', max_user_id='123456789', csrf='private-csrf')
        value = diagnostic.safe_message(row)
        self.assertTrue(value['has_explicit_peer'])
        for private in ('private-text','+79001111111','123456789','private-csrf'):
            self.assertNotIn(private, json.dumps(value))

    def test_alias_report_omits_phone_but_exposes_failing_version_fence(self):
        value = diagnostic.safe_alias({'phone': '+79001111111', 'reply_card': {
            'customer':8,'branch':1,'from':[7],'settings_version':2,'crm_connection':'crm'}},
            {'version':3,'connection_id':'crm'})
        self.assertFalse(value['settings_match'])
        self.assertTrue(value['connection_match'])
        self.assertNotIn('+79001111111', json.dumps(value))

    def test_same_source_row_can_be_inspected_without_being_relinked(self):
        old, new = self.message(7), self.message(8)
        item = diagnostic.counterpart(old, new, 8)
        self.assertEqual(diagnostic.native_relink.relink_pair(old,item,4), (7,8))
        # The candidate's catalog branch differs from the journal viewing branch.
        self.assertEqual(item['customers'], [{'branch_id':4,'customer_id':8}])

    def test_different_source_or_missing_whole_source_observation_is_not_reconstructed(self):
        old = self.message(7)
        for changes in ({'branch_id':3},{'crm_record_id':'53'},{'crm_observed_customers':None}):
            with self.subTest(changes=changes):
                self.assertIsNone(diagnostic.counterpart(old,self.message(8,**changes),8))

    def test_bulk_observation_remains_ambiguous_instead_of_becoming_an_alias(self):
        old, new = self.message(7), self.message(8,crm_observed_customers=[[4,8],[4,9]])
        item = diagnostic.counterpart(old,new,8)
        self.assertIsNone(diagnostic.native_relink.association_pair(old,item,4))

    def test_catalog_output_uses_external_customer_id_and_queries_only_read_saved_evidence(self):
        contact = uuid4()
        directory = {7:[{'external_id':7,'branch_id':1,'contact_id':contact,
            'status':'archive','available':True,'primary_phone':'+79001111111'}]}
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = None
        db.execute.return_value.mappings.return_value = []
        with patch.object(diagnostic,'customer_directory',return_value=directory):
            result = diagnostic.inspect(db,uuid4(),uuid4(),{'run':uuid4(),'connection_id':uuid4(),'version':3},7,8)
        self.assertEqual(result['cards'][0]['customer_id'],7)
        self.assertEqual(result['pairs'],[])
        self.assertNotIn('+79001111111',json.dumps(result))
        self.assertTrue(all(str(c.args[0]).startswith('SELECT ') for c in db.execute.call_args_list))

    def test_positive_customer_id_has_postgres_bigint_bounds(self):
        self.assertEqual(diagnostic.positive('9751'),9751)
        for value in ('0','-1',str(2**63)):
            with self.assertRaises(diagnostic.argparse.ArgumentTypeError):
                diagnostic.positive(value)

    def test_exact_source_pair_reports_the_real_history_block_without_mutation(self):
        old = self.message(7,crm_recipient_stale=True)
        new = self.message(8)
        phone = '+79001111111'
        directory = {identifier:[{'external_id':identifier,'branch_id':1,'contact_id':row['contact_id'],
            'status':'client','available':True,'primary_phone':phone}]
            for identifier,row in ((7,old),(8,new))}
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = None
        db.execute.return_value.mappings.return_value = [old,new]
        with patch.object(diagnostic,'customer_directory',return_value=directory), \
             patch.object(diagnostic.alfa_shared_parent,'members',return_value=[old['contact_id']]), \
             patch.object(diagnostic.alfa_shared_parent,'_current',return_value=True), \
             patch.object(diagnostic.native_relink,'reply_target',side_effect=lambda *a:a[-2]), \
             patch.object(diagnostic.native_relink,'history_proven',return_value=False), \
             patch.object(diagnostic.native_relink,'prove') as write:
            result = diagnostic.inspect(db,uuid4(),uuid4(),{'run':uuid4(),'connection_id':uuid4(),'version':3},7,8)
        self.assertEqual(result['pairs'][0]['counterparts'][0]['failed_checks'],['conversation_history_proven'])
        self.assertTrue(old['payload']['crm_recipient_stale'])
        write.assert_not_called()
        self.assertTrue(all(str(c.args[0]).startswith('SELECT ') for c in db.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
