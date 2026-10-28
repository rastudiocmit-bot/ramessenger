"""Immutable bodies, mutable source associations, and fenced exact-source relinks."""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger import native_relink as relink, alfa_shared_parent as parents
from center_messenger.history_receipts import new_records

AT = datetime(2026, 10, 28, 15, 57, tzinfo=UTC)
A, B, CRM = uuid4(), uuid4(), uuid4()
PHONE = '+79161234567'
SNAPSHOT = {'run': uuid4(), 'connection_id': CRM, 'version': 1}


def card(customer, contact, phone=PHONE):
    return {'external_id': customer, 'branch_id': 1, 'contact_id': contact,
            'primary_phone': phone, 'available': True, 'status': 'client'}


class MetadataFilterTests(unittest.TestCase):
    def filter(self, incoming, **changes):
        saved = {'external_record_id': 'in:4:52', 'direction': 'in', 'record': '52',
                 'max_user_id': None, 'customer_ids': [7], 'customer_id': '7',
                 'observed_customers': None, 'recipient_stale': None} | changes
        db = Mock()
        db.execute.return_value.mappings.return_value = [saved]
        return new_records(db, 'w', 'c', 4, [incoming])

    def record(self, customer=7, **changes):
        return {'direction': 'in', 'record_id': '52', 'text': 'original',
                'customers': [{'branch_id': 4, 'customer_id': customer}]} | changes

    def test_changed_customer_on_same_source_id_reaches_projector_as_metadata_only(self):
        result = self.filter(self.record(8))
        self.assertEqual([r['record_id'] for r in result], ['52'])
        self.assertTrue(result[0]['source_metadata_refresh'])

    def test_body_change_alone_does_not_reload_or_update_a_saved_message(self):
        self.assertEqual(self.filter(self.record(text='edited upstream')), [])

    def test_lost_peer_attribute_does_not_erase_stored_peer(self):
        self.assertEqual(self.filter(self.record(), max_user_id='77'), [])

    def test_new_explicit_peer_is_metadata_only(self):
        self.assertTrue(self.filter(self.record(max_user_id='77'))[0]['source_metadata_refresh'])

    def test_branch_reference_change_is_metadata_only_but_legacy_missing_hint_does_not_reimport(self):
        self.assertEqual(self.filter(self.record()), [])
        result = self.filter(self.record(), observed_customers=[[1, 7]])
        self.assertTrue(result[0]['source_metadata_refresh'])

    def test_fanout_uses_whole_source_signature_instead_of_one_family_projection(self):
        incoming = self.record()
        incoming['customers'].append({'branch_id': 4, 'customer_id': 8})
        self.assertEqual(self.filter(incoming, observed_customers=[[4, 7], [4, 8]]), [])
        self.assertTrue(self.filter(incoming)[0]['source_metadata_refresh'])


class RelinkTests(unittest.TestCase):
    def setUp(self):
        self.old = {'id': uuid4(), 'conversation_id': uuid4(), 'contact_id': A, 'crm_link_manual': False,
                    'occurred_at': AT, 'payload': {'branch_id': 4, 'crm_record_id': '52',
                    'customer_id': 7, 'customer_branch_id': 1, 'crm_customer_ids': [7]}}
        self.item = {'record_id': '52', 'direction': 'in', 'actor_type': 'contact',
                     'occurred_at': AT, 'customers': [{'branch_id': 4, 'customer_id': 8}]}
        self.directory = {7: [card(7, A)], 8: [card(8, B)]}
        self.new = {'customer_id': 8, 'branch_id': 1, 'contact_id': B,
                    'global_id_verified': True, 'primary_phone': PHONE}

    def test_customer_change_is_not_enough_without_exact_source_identity_and_time(self):
        self.assertEqual(relink.relink_pair(self.old, self.item, 4), (7, 8))
        for item, branch in [(self.item | {'record_id': '53'}, 4), (self.item, 3),
                             (self.item | {'occurred_at': AT + timedelta(minutes=1)}, 4),
                             (self.item | {'direction': 'out'}, 4)]:
            with self.subTest(item=item, branch=branch):
                self.assertIsNone(relink.relink_pair(self.old, item, branch))

    def test_conflicting_explicit_peers_never_establish_a_relink(self):
        self.old['payload']['max_user_id'] = '77'
        self.assertIsNone(relink.relink_pair(self.old, self.item | {'max_user_id': '78'}, 4))

    def test_bulk_or_unchanged_customer_is_not_a_one_to_one_relink(self):
        for customers in [[{'branch_id': 4, 'customer_id': 7}],
                          [{'branch_id': 4, 'customer_id': 8}, {'branch_id': 4, 'customer_id': 9}]]:
            self.assertIsNone(relink.relink_pair(self.old, self.item | {'customers': customers}, 4))

    def test_proven_relink_carries_current_crm_fence_and_source_witness(self):
        db = Mock(); db.execute.return_value.scalars.return_value = []
        with patch.object(parents, 'members', return_value=[A]), patch.object(parents, 'prove', return_value=[A, B]) as prove:
            result = relink.prove(db, 'w', 'channel', SNAPSHOT, 4, self.item, self.old,
                                  [self.new], self.directory)
        self.assertEqual(result, [A, B])
        self.assertEqual(prove.call_args.kwargs['source'], 'relink:in:4:52')
        route = prove.call_args.kwargs['reply_card']
        self.assertEqual((route['customer'], route['branch'], route['from']), (8, 1, [7]))
        self.assertEqual(route['settings_version'], 1)
        self.assertEqual(route['crm_connection'], str(CRM))
        query, params = db.execute.call_args_list[0].args
        self.assertEqual(set(query._bindparams), set(params))

    def test_peer_conflict_marks_the_old_address_stale_without_creating_alias(self):
        db = Mock(); db.execute.return_value.scalars.return_value = []
        self.old['payload']['max_user_id'] = '77'
        with patch.object(parents, 'prove') as prove:
            result = relink.prove(db, 'w', 'c', SNAPSHOT, 4, self.item | {'max_user_id': '78'},
                                 self.old, [self.new], self.directory)
        self.assertIsNone(result)
        prove.assert_not_called()
        self.assertIn('crm_recipient_stale', str(db.execute.call_args.args[0]))

    def test_manual_link_or_different_current_phone_blocks_merging(self):
        for manual, phone in [(True, PHONE), (False, '+79851234567')]:
            db = Mock(); db.execute.return_value.scalars.return_value = []
            self.old['crm_link_manual'] = manual
            directory = self.directory | {8: [card(8, B, phone)]}
            with patch.object(parents, 'prove') as prove:
                result = relink.prove(db, 'w', 'c', SNAPSHOT, 4, self.item, self.old,
                                     [self.new | {'primary_phone': phone}], directory)
            self.assertIsNone(result)
            prove.assert_not_called()

    def route(self):
        return {'customer': 8, 'branch': 1, 'from': [7], 'source': 'relink:in:4:52',
                'crm_connection': str(CRM), 'settings_version': 1}

    def test_old_branch_projection_uses_verified_relink_without_changing_stored_body(self):
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = {
            'contacts': [str(A), str(B)], 'phone': PHONE, 'reply_card': self.route()}
        target = self.old['payload'] | {'crm_recipient_stale': True}
        with patch.object(parents, '_current', return_value=True), \
             patch('center_messenger.alfa_repair.customer_directory', return_value=self.directory):
            result = relink.reply_target(db, 'w', 'c', SNAPSHOT, target, self.directory)
        self.assertEqual((result['customer_id'], result['customer_branch_id']), (8, 1))
        self.assertFalse(result['crm_recipient_stale'])
        self.assertEqual(target['customer_id'], 7)

    def test_changed_settings_or_missing_current_family_proof_never_redirect(self):
        for version, current in [(2, True), (1, False)]:
            db = Mock()
            db.execute.return_value.scalar_one_or_none.return_value = {
                'contacts': [str(A), str(B)], 'phone': PHONE,
                'reply_card': self.route() | {'settings_version': version}}
            target = self.old['payload']
            with patch.object(parents, '_current', return_value=current):
                self.assertIs(relink.reply_target(db, 'w', 'c', SNAPSHOT, target, self.directory), target)

    def test_a_reverse_relink_replaces_the_old_route_instead_of_creating_a_cycle(self):
        payload = {'contacts': [str(A), str(B)], 'canonical': str(min(A, B)),
                   'phone': PHONE, 'reply_card': self.route()}
        db = Mock()
        db.execute.side_effect = [Mock(scalar_one_or_none=Mock(return_value=None)),
            Mock(all=Mock(return_value=[('one', payload), ('two', payload)])), Mock(), Mock()]
        records = [dict(self.new, customer_id=7, contact_id=A), self.new]
        with patch.object(parents, '_current', return_value=True), \
             patch.object(parents, 'members', side_effect=lambda _d, _w, _c, _s, person, _p: [person]):
            parents.prove(db, 'w', 'c', SNAPSHOT, records, source='relink:in:4:52',
                          reply_card=self.route() | {'customer': 7, 'from': [8]})
        written = json.loads(db.execute.call_args_list[-1].args[1]['payload'])
        self.assertEqual((written['reply_card']['customer'], written['reply_card']['from']), (7, [8]))

    def test_relinked_mirrors_share_only_the_exact_same_source_id(self):
        old = {'crm_customer_ids': [7], 'crm_record_id': '52'}
        current = {'crm_customer_ids': [8], 'crm_record_id': '52'}
        self.assertEqual(relink.mirror_key(old, {7, 8}), relink.mirror_key(current, {7, 8}))
        self.assertNotEqual(relink.mirror_key(old, {7, 8}), relink.mirror_key(current | {'crm_record_id': '53'}, {7, 8}))
        self.assertNotEqual(relink.mirror_key(old, {7, 8}), relink.mirror_key({'crm_customer_ids': [9], 'crm_record_id': '52'}, {7, 8}))
        self.assertNotEqual(relink.mirror_key(old, set()), relink.mirror_key(current, set()))

    def test_a_new_witness_does_not_inherit_origins_from_previous_crm_settings(self):
        payload = {'contacts': [str(A), str(B)], 'canonical': str(min(A, B)), 'phone': PHONE,
                   'reply_card': self.route() | {'from': [99], 'settings_version': 0}}
        db = Mock()
        db.execute.side_effect = [Mock(scalar_one_or_none=Mock(return_value=None)),
            Mock(all=Mock(return_value=[('one', payload), ('two', payload)])), Mock(), Mock()]
        records = [dict(self.new, customer_id=7, contact_id=A), self.new]
        with patch.object(parents, '_current', return_value=True), \
             patch.object(parents, 'members', side_effect=lambda _d, _w, _c, _s, person, _p: [person]):
            parents.prove(db, 'w', 'c', SNAPSHOT, records, source='relink:in:4:52', reply_card=self.route())
        self.assertEqual(json.loads(db.execute.call_args_list[-1].args[1]['payload'])['reply_card']['from'], [7])

    def test_mirror_witness_requires_current_family_and_current_customer_phones(self):
        for current, new_phone, expected in [(True, PHONE, {7, 8}), (False, PHONE, set()),
                                             (True, '+79851234567', set())]:
            db = Mock()
            db.execute.return_value.mappings.return_value.one_or_none.return_value = {
                'contact_id': A, 'payload': {'contacts': [str(A), str(B)], 'phone': PHONE, 'reply_card': self.route()}}
            directory = self.directory | {8: [card(8, B, new_phone)]}
            with patch.object(parents, '_current', return_value=current), \
                 patch('center_messenger.alfa_repair.customer_directory', return_value=directory):
                self.assertEqual(relink.mirror_customers(db, 'w', 'c', SNAPSHOT, 'conversation'), expected)


if __name__ == '__main__':
    unittest.main()
