"""Customer totals, component separation and live read contracts for AlfaCRM."""
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_family import customer_balance, fetch_family_card, fetch_family_finance, fetch_family_lessons, merge_family_lessons, customer_paid_lessons, customer_subscriptions, current_lessons, group_facts, _lesson, mapping_pending


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

    def subscription_cards(self):
        return [{'id':'7','branch_id':str(branch),'branch_name':f'Филиал {branch}',
            'balance':'8062.50','paid_lesson_count':9,'refreshed_at':'2026-10-25T00:00:00+00:00',
            'subscriptions':[{'id':40,'tariff_id':9,'name':'Художественная школа','balance':'8062.50',
                'begin':'2026-10-01','end':'2027-07-04','archived':False}]} for branch in (1,2)]

    def lesson(self, *, branch=2, subject=11, subscription=40):
        return {'id':10,'date':'2026-10-25','time':'15:00','status':1,'branch_id':branch,
            'subject_id':subject,'lesson_type_id':1,'customer_id':7,'customer_verified':True,
            'group_ids':[5],'subscription_id':subscription}

    def test_shared_subscription_and_paid_lessons_are_not_multiplied_by_branches(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[self.lesson()]
        subscriptions=customer_subscriptions(cards)
        self.assertEqual(len(subscriptions),1)
        self.assertEqual(subscriptions[0]['id'],40)
        self.assertEqual(subscriptions[0]['balance'],'8062.50')
        self.assertEqual(subscriptions[0]['branches'],[{'id':'2','name':'Филиал 2'}])
        self.assertEqual(len(subscriptions[0]['observed_branches']),2)
        self.assertEqual(len(subscriptions[0]['usage']),1)
        self.assertEqual(customer_paid_lessons(cards),9)
        self.assertEqual(customer_balance(cards),'8062.50')
        self.assertEqual([len(c['subscriptions']) for c in cards],[1,1],'raw branch cache is retained')
        self.assertNotIn('branches',cards[0]['subscriptions'][0])

    def test_identical_names_or_tariffs_do_not_merge_distinct_customer_tariff_ids(self):
        cards=self.subscription_cards();cards[1]['subscriptions'][0]['id']=41
        self.assertEqual([s['id'] for s in customer_subscriptions(cards)],[40,41])
        cards=self.subscription_cards();cards[1]['id']='8'
        self.assertEqual(len(customer_subscriptions(cards)),2)
        self.assertEqual(customer_paid_lessons(cards),18)

    def test_fresh_subscription_and_paid_count_supersede_older_branch_observation(self):
        cards=self.subscription_cards();cards[1].update(refreshed_at='2026-10-25T01:00:00+00:00',
            balance_updated_at='2026-10-25T01:00:00+00:00',paid_lesson_count=8)
        cards[1]['subscriptions'][0].update(balance='7000.00',archived=True)
        subscriptions=customer_subscriptions(cards)
        self.assertEqual(subscriptions[0]['balance'],'7000.00')
        self.assertTrue(subscriptions[0]['archived'])
        self.assertEqual(customer_paid_lessons(cards),8)

    def test_conflicting_or_missing_counts_remain_unknown_instead_of_being_added(self):
        cards=self.subscription_cards();cards[1]['paid_lesson_count']=8
        self.assertIsNone(customer_paid_lessons(cards))
        cards[1]['subscriptions'][0]['balance']='7000.00'
        subscriptions=customer_subscriptions(cards)
        self.assertEqual(len(subscriptions),1)
        self.assertTrue(subscriptions[0]['needs_review'])
        self.assertIsNone(subscriptions[0]['balance'])
        cards[1].update(id='8',paid_lesson_count=None)
        self.assertIsNone(customer_paid_lessons(cards))

    def test_branch_group_links_to_shared_subscription_are_preserved(self):
        cards=self.subscription_cards()
        groups=[]
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[self.lesson(branch=int(card['branch_id']))]
            groups.append({'id':'5','branch_id':card['branch_id'],'customer_ids':['7']})
        group_facts(groups,cards,today=date(2026,10,25))
        self.assertEqual([g['subscriptions'][0]['id'] for g in groups],[40,40])
        self.assertEqual(len(customer_subscriptions(cards)),1)

    def test_subject_fallback_requires_one_purchase_and_respects_type_and_dates(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[self.lesson(subscription=None)]
        self.assertEqual(customer_subscriptions(cards)[0]['usage'][0]['method'],'subject')
        cards[0]['subscriptions'].append({**cards[0]['subscriptions'][0],'id':41})
        self.assertTrue(all(not item['usage'] for item in customer_subscriptions(cards)))
        cards[0]['subscriptions'].pop()
        for change in ({'subject_id':12},{'lesson_type_id':2},{'date':'2028-01-01'}):
            for card in cards:card['lessons']=[{**self.lesson(subscription=None),**change}]
            self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])

    def test_explicit_purchase_on_another_subject_or_with_wrong_customer_is_not_laundered(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[self.lesson(subject=12)]
        self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])
        for card in cards:card['lessons']=[{**self.lesson(),'customer_id':8}]
        self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])
        for card in cards:card['lessons']=[self.lesson(subscription=999)]
        self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])

    def test_legacy_lesson_without_actual_branch_never_inherits_request_branch(self):
        cards=self.subscription_cards()
        for card in cards:
            card['lessons']=[{'id':10,'status':1,'date':'2026-10-25','group_ids':[5],'subscription_id':40}]
        result=customer_subscriptions(cards)
        self.assertEqual(result[0]['branches'],[])
        self.assertEqual(result[0]['relation_status'],'pending')
        self.assertTrue(mapping_pending(cards[0]))

    def test_allowed_branches_alone_and_conflicting_facts_cannot_prove_lesson_usage(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1],tariff_branch_ids=[1,2])
            card['lessons']=[]
        result=customer_subscriptions(cards)
        self.assertEqual(result[0]['branches'],[])
        self.assertEqual(result[0]['relation_status'],'unconfirmed')
        for card in cards:card['lessons']=[self.lesson()]
        cards[1]['subscriptions'][0]['balance']='100'
        self.assertTrue(customer_subscriptions(cards)[0]['needs_review'])
        self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])

    def test_group_subject_fallback_requires_membership_of_this_customer_and_branch(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[{**self.lesson(subscription=None),'customer_verified':False}]
        self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])
        wrong=[{'id':'5','branch_id':'1','customer_ids':['7']}]
        self.assertEqual(customer_subscriptions(cards,groups=wrong)[0]['branches'],[])
        right=[{'id':'5','branch_id':'2','customer_ids':['7']}]
        self.assertEqual(customer_subscriptions(cards,groups=right)[0]['branches'][0]['id'],'2')

    def test_lesson_parser_preserves_actual_branch_subject_type_and_own_purchase(self):
        lesson=_lesson({'id':10,'branch_id':2,'subject_id':11,'lesson_type_id':1,'date':'2026-10-25',
            'group_ids':[5],'details':[{'customer_id':8,'ctt_id':99},{'customer_id':7,'ctt_id':40,'is_attend':1}]},7)
        self.assertEqual((lesson['branch_id'],lesson['subject_id'],lesson['lesson_type_id'],lesson['subscription_id']),(2,11,1,40))
        self.assertTrue(lesson['customer_verified'])

    def test_group_payment_does_not_follow_identical_group_id_in_wrong_branch(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[self.lesson()]
        groups=[{'id':'5','branch_id':branch,'customer_ids':['7']} for branch in ('1','2')]
        group_facts(groups,cards,today=date(2026,10,25))
        self.assertEqual(groups[0]['subscriptions'],[])
        self.assertIsNone(groups[0]['next_lesson'])
        self.assertEqual(groups[1]['subscriptions'][0]['id'],40)
        self.assertEqual(groups[1]['next_lesson']['branch_id'],2)

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

    def test_background_cache_reuses_tariff_metadata_but_reads_live_customer_money_each_time(self):
        from center_messenger.family_cache import _TariffCache
        client,calls = self.client()
        original = client.call
        observed = 0
        def call(path,body):
            nonlocal observed
            data = original(path,body)
            if path.endswith('/customer/index'):
                observed += 1
                data['items'][0]['balance'] = str(observed*100)
            return data
        client.call = call
        cached = _TariffCache(client,{},('workspace','connection',1))
        first = fetch_family_card(cached,1,7,source_customer={'id':7,'branch_id':1})
        second = fetch_family_card(cached,1,7,source_customer={'id':7,'branch_id':1})
        self.assertEqual(first['balance'],Decimal('100'))
        self.assertEqual(second['balance'],Decimal('200'))
        self.assertEqual(len([path for path,_ in calls if path.endswith('/tariff/index')]),1)
        self.assertEqual(len([path for path,_ in calls if path.endswith('/customer/index')]),2)

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

    def test_refresh_keeps_subject_restrictions_and_actual_lesson_branch(self):
        client,_=self.client();original=client.call
        def call(path,body):
            data=original(path,body)
            if '/customer-tariff/' in path and not body['dead']:
                data['items'][0].update(subject_ids=[11],lesson_type_ids=[1])
            if path.endswith('/lesson/index') and body['status']==1:
                data={'items':[{'id':10,'branch_id':2,'subject_id':11,'lesson_type_id':1,'date':'2026-10-25','customer_ids':[7],'details':[{'customer_id':7,'ctt_id':40}]}],'page':0,'total':1}
            return data
        client.call=call
        facts=fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1},today=date(2026,10,25))
        self.assertEqual(facts['subscriptions'][0]['subject_ids'],[11])
        self.assertEqual(facts['subscriptions'][0]['lesson_type_ids'],[1])
        self.assertEqual(facts['lessons'][0]['branch_id'],2)

    def test_live_missing_or_invalid_amount_is_not_replaced_by_a_fake_zero(self):
        client,_=self.client(amount=None,paid=None)
        facts=fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1,'balance':'999'})
        self.assertIsNone(facts['balance'])
        self.assertIsNone(facts['paid_lesson_count'])
        for value in ('NaN',True,'bad'):
            client,_=self.client(amount=value)
            with self.subTest(value=value), self.assertRaises(AlfaError):
                fetch_family_card(client,1,7,source_customer={'id':7,'branch_id':1})

    def test_financial_refresh_never_requests_lessons(self):
        client, calls = self.client()
        facts = fetch_family_finance(client, 1, 7, source_customer={'id':7,'branch_id':1})
        self.assertEqual(facts['balance'], Decimal('3962.50'))
        self.assertEqual(facts['subscriptions'][0]['id'], 40)
        self.assertFalse(any(path.endswith('/lesson/index') for path, _ in calls))

    def test_recent_lesson_window_reduces_payload_and_page_requests(self):
        today, calls = date(2026,10,25), []
        # 600 completed rows in a year, 20 of them in the last 30 days.
        rows = [dict(id=i+1, date=(today-timedelta(days=31+i%330)).isoformat(),status=3,customer_ids=[7]) for i in range(580)]
        rows += [dict(id=600+i, date=(today-timedelta(days=i)).isoformat(),status=3,customer_ids=[7]) for i in range(20)]
        rows += [dict(id=700+i, date=(today+timedelta(days=i)).isoformat(),status=1,customer_ids=[7]) for i in range(20)]
        def call(path, body):
            calls.append(dict(body))
            selected = [row for row in rows if row['status']==body['status'] and body['date_from']<=row['date']<=body['date_to']]
            first = body['page']*body['pageSize']
            return {'items':selected[first:first+body['pageSize']], 'page':body['page'], 'total':len(selected)}
        client = type('Client', (), {'call':staticmethod(call)})()
        full = fetch_family_lessons(client,1,7,today=today,full=True)
        self.assertEqual((len(full),len(calls)),(620,3))
        calls.clear()
        recent = fetch_family_lessons(client,1,7,today=today)
        self.assertEqual((len(recent),len(calls)),(40,2))
        self.assertEqual(calls[0]['date_from'],'2026-09-25')
        self.assertEqual(calls[1]['date_to'],'2027-01-23')

    def test_short_window_replaces_cancellations_and_preserves_older_branch_proof(self):
        old = {**self.lesson(), 'id':1, 'date':'2026-03-01', 'status':3}
        deleted = {**self.lesson(), 'id':2, 'date':'2026-10-20', 'status':3}
        cancelled = {**self.lesson(), 'id':3, 'date':'2026-11-01'}
        expired = {**self.lesson(), 'id':4, 'date':'2025-01-01', 'status':3}
        moved = {**self.lesson(), 'id':5, 'date':'2026-04-01', 'status':3}
        changed = {**self.lesson(), 'id':5, 'date':'2026-10-25', 'status':3, 'branch_id':1}
        result = merge_family_lessons([old,deleted,cancelled,expired,moved],[changed],today=date(2026,10,25))
        self.assertEqual(result,[old,changed])
        self.assertEqual(result[0]['subscription_id'],40)
        self.assertEqual(result[0]['branch_id'],2)
        self.assertEqual(merge_family_lessons(result,[],today=date(2026,10,25),full=True),[])

    def test_a_partial_or_out_of_window_response_cannot_erase_lesson_cache(self):
        def call(path,body):
            return {'items':[{'id':1,'date':'2020-01-01','customer_ids':[7]}],'total':1,'page':0}
        client=type('Client',(),{'call':staticmethod(call)})()
        with self.assertRaises(AlfaError):
            fetch_family_lessons(client,1,7,today=date(2026,10,25))

    def test_lesson_freshness_is_independent_of_a_newer_balance(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
        cards[0].update(refreshed_at='2026-10-25T12:00:00+00:00',lessons_refreshed_at='2026-10-25T09:00:00+00:00',lessons=[self.lesson()])
        cards[1].update(refreshed_at='2026-10-25T11:00:00+00:00',lessons_refreshed_at='2026-10-25T10:00:00+00:00',lessons=[self.lesson(subject=12)])
        self.assertEqual(customer_subscriptions(cards)[0]['usage'],[])

    def test_recent_lesson_check_does_not_promote_retained_old_branch_evidence(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
        cards[0].update(lessons_refreshed_at='2026-10-25T12:00:00+00:00',
            lessons_full_refreshed_at='2026-10-25T09:00:00+00:00',lessons=[self.lesson()])
        cards[1].update(lessons_refreshed_at='2026-10-25T11:00:00+00:00',
            lessons_full_refreshed_at='2026-10-25T10:00:00+00:00',lessons=[self.lesson(subject=12)])
        self.assertEqual(customer_subscriptions(cards)[0]['usage'],[])
        cards[0]['lessons'][0]['observed_at']='2026-10-25T12:00:00+00:00'
        self.assertEqual(len(customer_subscriptions(cards)[0]['usage']),1)

    def test_latest_complete_subscription_inventory_can_remove_an_old_purchase(self):
        cards=self.subscription_cards()
        cards[1].update(refreshed_at='2026-10-25T01:00:00+00:00',subscriptions=[])
        self.assertEqual(customer_subscriptions(cards),[])
        cards[1]['subscriptions']=[{**cards[0]['subscriptions'][0],'id':41}]
        self.assertEqual([s['id'] for s in customer_subscriptions(cards)],[41])

    def test_conflicting_equal_time_inventories_cannot_prove_a_branch_or_balance(self):
        cards=self.subscription_cards()
        cards[0]['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
        cards[0]['lessons']=[self.lesson()]
        cards[1]['subscriptions']=[]
        result=customer_subscriptions(cards)
        self.assertEqual(len(result),1)
        self.assertTrue(result[0]['needs_review'])
        self.assertIsNone(result[0]['balance'])
        self.assertEqual(result[0]['branches'],[])

    def test_a_moved_lesson_uses_its_latest_date_for_the_group(self):
        cards=self.subscription_cards()
        cards[0].update(lessons_refreshed_at='2026-10-25T10:00:00+00:00',
            lessons=[{**self.lesson(),'date':'2026-11-01'}])
        cards[1].update(lessons_refreshed_at='2026-10-25T11:00:00+00:00',
            lessons=[{**self.lesson(),'date':'2026-11-02'}])
        groups=[{'id':'5','branch_id':'2','customer_ids':['7']}]
        group_facts(groups,cards,today=date(2026,10,25))
        self.assertEqual(groups[0]['next_lesson']['date'],'2026-11-02')

    def test_a_fresh_empty_actual_branch_window_removes_a_stale_mirrored_lesson(self):
        cards=self.subscription_cards()
        cards[0].update(lessons_refreshed_at='2026-10-25T10:00:00+00:00',lessons=[self.lesson()])
        cards[1].update(lessons_refreshed_at='2026-10-25T11:00:00+00:00',lessons=[])
        self.assertEqual(current_lessons(cards),[])
        groups=[{'id':'5','branch_id':'2','customer_ids':['7']}]
        group_facts(groups,cards,today=date(2026,10,25))
        self.assertIsNone(groups[0]['next_lesson'])
        self.assertEqual(customer_subscriptions(cards)[0]['branches'],[])

    def test_current_group_membership_does_not_prove_a_lesson_before_enrollment(self):
        cards=self.subscription_cards()
        for card in cards:
            card['subscriptions'][0].update(subject_ids=[11],lesson_type_ids=[1])
            card['lessons']=[{**self.lesson(subscription=None),'date':'2026-10-10','status':3,'customer_verified':False}]
        groups=[{'id':'5','branch_id':'2','customer_ids':['7'],
            'memberships':[{'customer_id':'7','begin':'2026-10-20','end':None}]}]
        self.assertEqual(customer_subscriptions(cards,groups=groups)[0]['branches'],[])
        group_facts(groups,cards,today=date(2026,10,25))
        self.assertIsNone(groups[0]['previous_lesson'])

    def test_foreign_customer_lesson_and_equal_time_conflicts_do_not_set_group_dates(self):
        cards=self.subscription_cards()
        cards[0]['lessons']=[{**self.lesson(),'customer_id':8}]
        cards[1]['lessons']=[]
        groups=[{'id':'5','branch_id':'2','customer_ids':['7']}]
        group_facts(groups,cards,today=date(2026,10,25))
        self.assertIsNone(groups[0]['next_lesson'])
        cards[0]['lessons']=[self.lesson()]
        cards[1]['lessons']=[{**self.lesson(),'time':'16:00'}]
        self.assertEqual(current_lessons(cards),[])

    def test_absence_coverage_uses_the_workspace_calendar_day(self):
        cards=self.subscription_cards()
        old={**self.lesson(),'date':'2026-09-24','status':3}
        cards[0].update(lessons_full_refreshed_at='2026-10-20T12:00:00+00:00',lessons=[old])
        cards[1].update(lessons_full_refreshed_at='2026-09-01T12:00:00+00:00',lessons_refreshed_at='2026-10-24T21:30:00+00:00',
            lessons_recent_day='2026-10-25',lessons=[])
        self.assertEqual(current_lessons(cards),[old])
        cards[0]['lessons'][0]['date']='2026-09-25'
        self.assertEqual(current_lessons(cards),[])


if __name__=='__main__':
    unittest.main()
