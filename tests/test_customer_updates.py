"""Editable facts, stable ID/address bindings and conservative family continuity."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.crm_directory import customer_row, project_directory
from center_messenger.customer_updates import merge_fields, label, is_older, key
from center_messenger.alfa_client import AlfaError


def row(customer=7, **changes):
    return dict(customer_row({'id': customer, 'name': 'Pupil', 'legal_name': 'Parent',
        'is_study': 1, 'phone': ['+79000000001'], 'email': ['parent@example.test'],
        'dob': '2015-01-01', **changes}, archive=False), branch_id=1)


class CustomerFactsTests(unittest.TestCase):
    def test_partial_identity_preserves_absent_names_contacts_and_optional_fields(self):
        old = row(legal_type=2, study_status_id=3, lead_status_ids=[4])
        merged = merge_fields({'id': 7, 'name': 'Corrected pupil'}, old)
        self.assertEqual(merged['name'], 'Corrected pupil')
        for field in ('legal_name', 'legal_type', 'phones', 'emails', 'dob', 'study_status_id', 'lead_status_ids', 'status'):
            self.assertEqual(merged[field], old[field], field)

    def test_explicit_empty_values_clear_only_the_fields_that_were_present(self):
        merged = merge_fields({'id': 7, 'legal_name': '', 'phone': [], 'dob': None}, row())
        self.assertEqual((merged['legal_name'], merged['phones'], merged['dob']), ('', [], None))
        self.assertEqual(merged['emails'], ['parent@example.test'])

    def test_new_incomplete_card_is_rejected_instead_of_inventing_identity(self):
        with self.assertRaises(AlfaError):
            merge_fields({'id': 8}, None)

    def test_status_can_change_without_replacing_the_source_id(self):
        old = row(is_study=0)
        updated = merge_fields({'id': 7, 'is_study': 1}, old)
        self.assertEqual((updated['external_id'], updated['status']), (7, 'client'))

    def test_a_partial_response_without_status_does_not_unarchive_the_last_known_card(self):
        old = row()
        old['status'] = 'archive'
        self.assertEqual(merge_fields({'id': 7, 'name': 'Corrected'}, old)['status'], 'archive')

    def test_labels_are_derived_from_current_cards_and_do_not_use_phone_placeholders_as_names(self):
        self.assertEqual(label([row(legal_name='New parent')], 'Old parent'), 'New parent')
        self.assertEqual(label([row(legal_name='Parent A'), row(8, legal_name='Parent B')], 'Parent B'), 'Parent B')
        self.assertEqual(label([row(legal_name='+79000000001')]), '+79000000001')

    def test_observation_ordering_and_scope_include_the_published_generation(self):
        at = datetime(2026, 10, 29, tzinfo=UTC)
        saved = {'observed_at': at.isoformat()}
        self.assertTrue(is_older(saved, at - timedelta(seconds=1)))
        self.assertTrue(is_older(saved, at))
        self.assertFalse(is_older(saved, at + timedelta(seconds=1)))
        self.assertNotEqual(key('crm', 'one', 1, 7), key('crm', 'two', 1, 7))


class ContactContinuityTests(unittest.TestCase):
    def project(self, rows, previous=()):
        return project_directory(self.connection, rows, previous)

    def setUp(self):
        self.connection = uuid4()

    def previous(self, rows):
        bindings = self.project(rows)[2]
        return [dict(r, **next(b for b in bindings if b['external_id'] == r['external_id'])) for r in rows]

    def test_payer_rename_in_a_shared_phone_conflict_keeps_the_existing_contact_and_pupil(self):
        rows = [row(legal_name='Parent A'), row(8, legal_name='Parent B')]
        previous = self.previous(rows)
        current = self.project([row(legal_name='Parent A corrected'), rows[1]], previous)[2]
        by_id = {r['external_id']: r for r in current}
        for old in previous:
            new = by_id[old['external_id']]
            self.assertEqual((new['contact_id'], new['student_id']), (old['contact_id'], old['student_id']))

    def test_existing_distinct_contacts_do_not_merge_when_current_names_become_equal(self):
        rows = [row(legal_name='Parent A'), row(8, legal_name='Parent B')]
        previous = self.previous(rows)
        contacts, _, bindings = self.project([row(legal_name='Parent'), row(8, legal_name='Parent')], previous)
        self.assertEqual(len(contacts), 2)
        self.assertEqual({b['contact_id'] for b in bindings}, {r['contact_id'] for r in previous})

    def test_a_changed_primary_address_cannot_join_another_existing_family(self):
        rows = [row(), row(8, phone=['+79000000002'])]
        previous = self.previous(rows)
        current = self.project([row(phone=['+79000000002']), rows[1]], previous)[2]
        by_id = {r['external_id']: r for r in current}
        self.assertNotEqual(by_id[7]['contact_id'], by_id[8]['contact_id'])
        self.assertEqual(by_id[7]['student_id'], previous[0]['student_id'])

    def test_a_new_card_does_not_take_over_an_existing_contact_by_phone_and_label(self):
        previous = self.previous([row()])
        contacts, _, _ = self.project([row(), row(8)], previous)
        self.assertEqual(len(contacts), 2)

    def test_two_existing_families_changing_to_the_same_new_phone_remain_distinct(self):
        previous = self.previous([row(), row(8, phone=['+79000000002'])])
        contacts, _, _ = self.project([row(phone=['+79000000003']), row(8, phone=['+79000000003'])], previous)
        self.assertEqual(len(contacts), 2)


if __name__ == '__main__':
    unittest.main()
