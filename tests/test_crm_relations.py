"""Identity and CGI contracts: names, branches and purchases are not identities."""
from datetime import date
from pathlib import Path
import sys
import unittest
from uuid import uuid4, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.crm_directory import customer_row, project_directory, student_bindings
from center_messenger.group_links import fetch
from center_messenger.alfa_client import AlfaError


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.connection=uuid4()

    def row(self, customer=7, branch=1, **fields):
        raw={'id':customer,'name':'Анна Тестовая','legal_name':'Мария Тестовая',
             'dob':'2016-01-01','phone':'+7 900 000 00 00','is_study':1,**fields}
        return {**customer_row(raw,archive=False),'branch_id':branch}

    def test_different_customer_ids_with_identical_name_and_birthday_are_two_children(self):
        contacts,students,bindings=project_directory(self.connection,[self.row(7),self.row(8)])
        self.assertEqual(len(contacts),1)
        self.assertEqual(len(students),2)
        self.assertEqual(len({b['student_id'] for b in bindings}),2)

    def test_one_global_id_in_two_branches_is_one_child_even_without_a_birthday(self):
        _,students,bindings=project_directory(self.connection,[self.row(dob=None),self.row(branch=2,dob=None)])
        self.assertEqual(len(students),1)
        self.assertEqual({b['external_id'] for b in bindings},{7})
        self.assertEqual({b['branch_id'] for b in bindings},{1,2})

    def test_correcting_name_and_birthday_keeps_the_verified_id(self):
        stable=uuid4()
        _,students,bindings=project_directory(self.connection,[self.row(name='Анна Исправленная',dob='2016-02-02')],
            [{'external_id':7,'student_id':stable}])
        self.assertEqual(students,[{'id':stable,'name':'Анна Исправленная'}])
        self.assertEqual(bindings[0]['student_id'],stable)

    def test_an_old_cross_id_alias_splits_and_remains_stable_on_the_next_publication(self):
        shared=uuid5(self.connection,'student:7')
        previous=[{'external_id':i,'student_id':shared} for i in (7,8)]
        rows=[self.row(7),self.row(8)]
        contacts,students,bindings=project_directory(self.connection,rows,previous)
        self.assertEqual(len(students),2)
        first={b['external_id']:b['student_id'] for b in bindings}
        self.assertEqual(first[7],shared)
        self.assertNotEqual(first[8],shared)
        self.assertTrue(contacts[0]['needs_review'])
        _,_,again=project_directory(self.connection,rows,bindings)
        self.assertEqual({b['external_id']:b['student_id'] for b in again},first)

    def test_preexisting_id_owned_by_another_customer_is_not_reused_for_a_new_customer(self):
        legacy=uuid5(self.connection,'student:7')
        ids,_=student_bindings(self.connection,[7,8],[{'external_id':8,'student_id':legacy}])
        self.assertEqual(ids[8],legacy)
        self.assertNotEqual(ids[7],legacy)

    def test_conflicting_branch_names_require_review_without_creating_a_second_child(self):
        contacts,students,_=project_directory(self.connection,[self.row(),self.row(branch=2,name='Другое ФИО')])
        self.assertEqual(len(students),1)
        self.assertTrue(contacts[0]['needs_review'])

    def test_a_shared_phone_with_different_payers_does_not_merge_their_families(self):
        contacts,students,_=project_directory(self.connection,[self.row(),self.row(8,legal_name='Другой Родитель')])
        self.assertEqual((len(contacts),len(students)),(2,2))
        self.assertTrue(all(c['needs_review'] for c in contacts))

    def test_a_secondary_phone_does_not_create_a_chain_between_unrelated_families(self):
        rows=[self.row(phone=['+79000000000','+79111111111']),self.row(8,phone=['+79222222222','+79111111111'])]
        contacts,_,_=project_directory(self.connection,rows)
        self.assertEqual(len(contacts),2)


class GroupApiTests(unittest.TestCase):
    def client(self, members, groups):
        calls=[]
        def call(path,body):
            calls.append((path,dict(body)))
            items=members if '/cgi/customer?' in path else groups.get(body['id'],[])
            return {'items':items,'page':body['page'],'total':len(items)}
        return type('Client',(),{'call':staticmethod(call)})(),calls

    def test_groups_use_customer_id_and_cgi_periods_and_verify_each_group(self):
        members=[{'id':10,'customer_id':7,'group_id':20,'b_date':'01.10.2026','e_date':'2027-01-01'}]
        client,calls=self.client(members,{20:[{'id':20,'name':'Робототехника','branch_id':2}]})
        groups,links=fetch(client,2,7)
        self.assertEqual(groups,{20:'Робототехника'})
        self.assertEqual(links[0]['begin'],date(2026,10,1))
        self.assertEqual(links[0]['end'],date(2027,1,1))
        self.assertEqual(calls[0][0],'/v2api/2/cgi/customer?customer_id=7')
        self.assertEqual(calls[0][1]['customer_id'],7)
        self.assertEqual(calls[1][1]['id'],20)

    def test_another_customer_or_reversed_period_cannot_replace_saved_relations(self):
        for member in ({'id':10,'customer_id':8,'group_id':20},
                       {'id':10,'customer_id':7,'group_id':20,'b_date':'2027-01-01','e_date':'2026-01-01'}):
            client,_=self.client([member],{})
            with self.subTest(member=member),self.assertRaises(AlfaError):
                fetch(client,1,7)

    def test_an_archived_group_and_a_mirror_from_another_branch_are_not_current_links(self):
        members=[{'id':10,'customer_id':7,'group_id':20},{'id':11,'customer_id':7,'group_id':21}]
        client,_=self.client(members,{20:[],21:[{'id':21,'name':'Другая группа','branch_id':2}]})
        self.assertEqual(fetch(client,1,7),({},[]))

    def test_a_wrong_group_id_is_a_failed_response_not_an_empty_success(self):
        client,_=self.client([{'id':10,'customer_id':7,'group_id':20}],{20:[{'id':21,'name':'Wrong'}]})
        with self.assertRaises(AlfaError):
            fetch(client,1,7)
