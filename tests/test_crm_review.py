from datetime import date
import json
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/backend/src'))
from center_messenger.crm_review import ReviewIndex
from center_messenger.crm_directory import project_directory


class CurrentReviewTests(unittest.TestCase):
    def row(self, customer=7, branch=1, **fields):
        return {'external_id':customer,'branch_id':branch,'contact_id':'family',
                'student_id':f'child-{customer}','name':'Анна Тестовая','legal_name':'Мария Тестовая',
                'legal_type':1,'phones':['+79000000000'],'emails':[],
                'dob':date(2016,1,1),'status':'client',**fields}

    def codes(self, rows, ids=(7,)):
        return {item['code'] for item in ReviewIndex(rows).reasons(ids)}

    def test_resolved_history_and_missing_optional_branch_fields_are_not_current_conflicts(self):
        rows=[self.row(needs_review=True),self.row(branch=2,dob=None,name='  АННА   ТЕСТОВАЯ ')]
        self.assertEqual(self.codes(rows),set())
        contacts,_,_=project_directory(uuid4(),rows)
        self.assertFalse(contacts[0]['needs_review'])

    def test_conflicting_names_and_known_birthdays_have_specific_ids_and_branches(self):
        index=ReviewIndex([self.row(),self.row(branch=2,name='Другое имя',dob=date(2017,2,2))])
        reasons=index.reasons([7])
        self.assertEqual({r['code'] for r in reasons},{'customer_name_conflict','customer_birthdate_conflict'})
        self.assertTrue(all(r['customer_ids']==['7'] and r['branch_ids']==['1','2'] for r in reasons))

    def test_shared_parent_contacts_remain_separate_and_foreign_names_are_not_disclosed(self):
        rows=[self.row(),self.row(8,contact_id='other',legal_name='Другой Представитель')]
        reasons=ReviewIndex(rows).reasons([7])
        self.assertEqual({r['code'] for r in reasons},{'conflicting_parents'})
        self.assertNotIn('Другой Представитель',json.dumps(reasons,ensure_ascii=False))
        self.assertEqual(reasons[0]['customer_ids'],['7'])

    def test_phone_placeholders_and_secondary_phone_coincidence_do_not_create_parent_conflicts(self):
        rows=[self.row(),self.row(8,contact_id='archive',name='+79000000000',legal_name='+7 (900) 000-00-00')]
        self.assertEqual(self.codes(rows),set())
        rows=[self.row(phones=['+79000000000','+79111111111']),
              self.row(8,contact_id='other',phones=['+79222222222','+79111111111'],legal_name='Другой Представитель')]
        self.assertEqual(self.codes(rows),set())

    def test_current_binding_collisions_clear_after_actual_repair(self):
        rows=[self.row(student_id='shared'),self.row(8,student_id='shared')]
        self.assertIn('shared_student_binding',self.codes(rows))
        rows[1]['student_id']='separate'
        self.assertNotIn('shared_student_binding',self.codes(rows))
        rows.append(self.row(branch=2,student_id='other-id'))
        self.assertIn('customer_binding_conflict',self.codes(rows))

    def test_successfully_rebound_import_does_not_keep_a_historical_warning(self):
        rows=[self.row(),self.row(8)]
        shared=uuid4()
        contacts,_,bindings=project_directory(uuid4(),rows,
            [{'external_id':7,'student_id':shared},{'external_id':8,'student_id':shared}])
        self.assertEqual(len({r['student_id'] for r in bindings}),2)
        self.assertFalse(any(r['needs_review'] for r in contacts))

    def test_conflicting_primary_email_without_phones_still_requires_review(self):
        rows=[self.row(phones=[],emails=['shared@example.test']),
              self.row(8,contact_id='other',phones=[],emails=['shared@example.test'],legal_name='Другой Представитель')]
        self.assertIn('conflicting_parents',self.codes(rows))

    def test_a_phone_in_the_parent_name_is_missing_data_even_when_no_phone_field_is_filled(self):
        rows=[self.row(phones=[],emails=['shared@example.test']),self.row(branch=2,phones=[],
              emails=['shared@example.test'],legal_name='+7 (900) 000-00-00')]
        self.assertEqual(self.codes(rows),set())


if __name__=='__main__':
    unittest.main()
