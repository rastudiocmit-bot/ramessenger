"""Customer totals, component separation and live read contracts for AlfaCRM."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_family import customer_balance, fetch_family_card


class FamilyBalanceTests(unittest.TestCase):
    def test_total_already_contains_base_and_subscriptions(self):
        # The screenshot case: 2725 on a subscription, 1237.50 on base.
        cards = [{'id':'7','balance':'3962.50','subscriptions':[{'balance':'2725.00'}]}]
        self.assertEqual(customer_balance(cards),'3962.50')

    def test_customer_with_only_base_money_and_real_zero_or_debt(self):
        for amount in ('1237.50','0','-120.75'):
            with self.subTest(amount=amount):
                self.assertEqual(customer_balance([{'id':'7','balance':amount,'subscriptions':[]}]),amount)

    def test_archived_and_expired_subscription_lists_do_not_replace_the_total(self):
        self.assertEqual(customer_balance([{'id':7,'balance':'250.50','subscriptions':[
            {'balance':'9000','archived':True},{'balance':'4000','end':'2020-01-01'}]}]),'250.50')

    def test_duplicate_global_customer_in_branches_is_counted_once(self):
        self.assertEqual(customer_balance([{'id':'7','branch_id':'1','balance':'3962.50'},
                                           {'id':7,'branch_id':'2','balance':'3962.50'}]),'3962.50')

    def test_distinct_financial_customers_of_one_student_are_added_with_decimal_precision(self):
        self.assertEqual(customer_balance([{'id':7,'balance':'0.10'},{'id':8,'balance':'0.20'}]),'0.30')

    def test_unknown_component_does_not_display_a_partial_or_subscription_only_sum(self):
        self.assertIsNone(customer_balance([{'id':7,'balance':None,'subscriptions':[{'balance':'2725'}]}]))
        self.assertIsNone(customer_balance([{'id':7,'balance':'100'},{'id':8,'balance':None}]))
        self.assertIsNone(customer_balance([]))

    def test_latest_customer_observation_wins_but_equal_conflicts_are_unknown(self):
        older={'id':7,'balance':'100','balance_updated_at':'2026-10-24T10:00:00+00:00'}
        newer={'id':7,'balance':'200','balance_updated_at':'2026-10-24T11:00:00+00:00'}
        self.assertEqual(customer_balance([older,newer]),'200')
        self.assertIsNone(customer_balance([older,{**older,'balance':'200'}]))
        self.assertEqual(customer_balance([older,{**older,'balance':None}]),'100')

    def client(self, *, amount='3962.50', paid=3, archived=False, wrong_customer=False):
        calls=[]
        def call(path, body):
            calls.append((path,dict(body)))
            items=[]
            if path.endswith('/customer/index'):
                self.assertEqual(body['id'],7)
                self.assertEqual(body['is_study'],2)
                if body['removed']==(2 if archived else 0):
                    items=[{'id':8 if wrong_customer else 7,'balance':amount,'paid_lesson_count':paid}]
            elif '/customer-tariff/' in path and not body['dead']:
                items=[{'id':40,'customer_id':7,'tariff_id':9,'balance':'2725.00','b_date':'2026-10-01','e_date':'2027-07-04'}]
            elif path.endswith('/tariff/index'):
                items=[{'id':9,'name':'Модельная школа','type':1}]
            elif not ('/customer-tariff/' in path or path.endswith('/lesson/index')):
                self.fail(f'Unexpected endpoint {path}')
            return {'items':items,'total':len(items),'page':body['page']}
        return type('Client',(),{'call':staticmethod(call)})(),calls

    def test_refresh_reads_customer_total_instead_of_stale_snapshot_or_tariff_sum(self):
        client,calls=self.client()
        facts=fetch_family_card(client,1,7,source_customer={'id':'7','branch_id':'1','balance':'50','paid_lesson_count':99},today=date(2026,10,24))
        self.assertEqual(facts['balance'],Decimal('3962.50'))
        self.assertEqual(facts['paid_lesson_count'],3)
        self.assertEqual(facts['subscriptions'][0]['balance'],'2725.00')
        self.assertEqual(calls[0][0],'/v2api/1/customer/index')
        self.assertTrue(all('/index' in path for path,_ in calls),'finance reads must never create/update a payment')

    def test_archive_transition_is_found_without_taking_another_customers_money(self):
        client,calls=self.client(archived=True)
        facts=fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1},today=date(2026,10,24))
        self.assertEqual(facts['balance'],Decimal('3962.50'))
        self.assertEqual([body['removed'] for path,body in calls if path.endswith('/customer/index')],[0,2])
        client,_=self.client(wrong_customer=True)
        with self.assertRaises(AlfaError):
            fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1})

    def test_live_missing_or_invalid_amount_is_not_replaced_by_a_fake_zero(self):
        client,_=self.client(amount=None,paid=None)
        facts=fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1,'balance':'999'})
        self.assertIsNone(facts['balance'])
        self.assertIsNone(facts['paid_lesson_count'])
        for value in ('NaN',True,'bad'):
            client,_=self.client(amount=value)
            with self.subTest(value=value), self.assertRaises(AlfaError):
                fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1})


if __name__=='__main__':
    unittest.main()
