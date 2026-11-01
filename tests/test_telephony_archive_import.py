"""Historical provider pages are admitted before facts or cursors are written."""
from contextlib import contextmanager
from datetime import datetime,timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

from center_messenger import telephony_archive as schedule
from center_messenger.auth_security import AuthError
from center_messenger.telephony_agent import defaults
from center_messenger.telephony_archive_import import ArchiveImport
from test_telephony_calls import source


class Result:
    def __init__(self,value=None):self.value=value
    def mappings(self):return self
    def one_or_none(self):return self.value
    def one(self):return self.value
    def scalar_one(self):return self.value
    def scalar_one_or_none(self):return self.value
    def all(self):return [] if self.value is None else [(self.value,)]


class ArchiveImportTests(unittest.TestCase):
    def setUp(self):
        self.workspace=uuid4();self.now=datetime(2026,11,1,0,0,tzinfo=schedule.ZONE)
        self.control={'enabled':True,'version':1,'cursor_start':schedule.START,'cursor_end':None,
                      'page_offset':0,'account_id':None,'started_by':uuid4()}
        self.settings={'connected':True,'key_ciphertext':'encrypted','connection_id':uuid4(),
                       'account_id':'17','timezone':'Europe/Moscow','version':2}
        self.writes=[];self.current=self.control.copy()
        def execute(query,params=None):
            sql=str(query)
            if sql.startswith('SELECT n.'):return Result(self.settings)
            if sql.startswith('SELECT * FROM ai_settings'):return Result({'gateway_config':{'telephony_agent':defaults(self.workspace)|{'version':1}}})
            if sql.startswith('SELECT * FROM telephony_archive_control'):return Result(self.current)
            if sql.startswith('SELECT version FROM novofon_settings'):return Result(self.settings['version'])
            self.writes.append((sql,params))
            if sql.startswith('INSERT INTO telephony_calls'):return Result(params['id'])
            return Result(params.get('call') if sql.startswith('INSERT INTO telephony_archive_seen') else None)
        @contextmanager
        def tenant(*_):yield SimpleNamespace(execute=execute)
        self.store=SimpleNamespace(engine=Mock(),tenant=tenant,owned=lambda *_:True,clock=lambda:self.now)
        self.vault=Mock();self.vault.open.return_value={'api_key':'synthetic-key'}
        self.client=Mock();self.client.read.return_value=([source(start_time='2020-11-01 03:00:00',finish_time='2020-11-01 03:01:00')],{'total_items':1})
        with patch('center_messenger.telephony_archive_import.client_factory',return_value=Mock(return_value=self.client)):
            self.importer=ArchiveImport(self.store,self.vault)
        self.job={'workspace':self.workspace,'token':uuid4(),'run':None,'archive':self.control}

    def test_one_call_page_uses_account_quota_and_never_exceeds_a_thirty_day_window(self):
        with patch('center_messenger.telephony_archive_import.enqueue') as queue:self.importer.run(self.job)
        self.client.budget.bind_account.assert_called_once_with('17')
        args=self.client.read.call_args.kwargs
        self.assertEqual((args['limit'],args['offset']),(1,0))
        self.assertEqual(args['date_from'],'2020-10-31 00:00:00')
        self.assertEqual(args['date_till'],'2020-11-30 00:00:00')
        self.assertFalse(args['include_ongoing_calls']);self.assertTrue(queue.call_args.kwargs['historical'])
        care_source=next(params for sql,params in self.writes if params and params.get('t')=='phone-care-source-v1')
        self.assertEqual(care_source['state'],'pending')
        self.assertIn('"priority": 0',care_source['p'])
        cursor=next(params for sql,params in self.writes if 'cursor_start=:start' in sql)
        self.assertEqual(cursor['start'],schedule.START+timedelta(days=30));self.assertEqual(cursor['offset'],0)

    def test_empty_page_with_a_remaining_total_cannot_advance_the_archive(self):
        self.client.read.return_value=([],{'total_items':1})
        with self.assertRaises(AuthError):self.importer.run(self.job)
        self.assertEqual(self.writes,[])

    def test_pause_during_a_provider_request_discards_the_page_and_keeps_the_cursor(self):
        def paused(*_,**__):
            self.current['enabled']=False
            return [source(start_time='2020-11-01 03:00:00',finish_time='2020-11-01 03:01:00')],{'total_items':1}
        self.client.read.side_effect=paused
        with patch('center_messenger.telephony_archive_import.enqueue') as queue:self.importer.run(self.job)
        self.assertEqual(self.writes,[]);queue.assert_not_called()

    def test_daytime_and_overflowed_offsets_never_send_provider_requests(self):
        self.now=self.now.replace(hour=8);self.importer.run(self.job)
        self.client.read.assert_not_called()
        self.now=self.now.replace(hour=0);self.control['page_offset']=100001
        with self.assertRaises(AuthError):self.importer.run(self.job)
        self.client.read.assert_not_called();self.assertEqual(self.writes,[])

    def test_out_of_range_call_cannot_be_assigned_to_the_archive(self):
        self.client.read.return_value=([source()],{'total_items':1})
        with self.assertRaises(AuthError):self.importer.run(self.job)
        self.assertEqual(self.writes,[])


if __name__=='__main__':unittest.main()
