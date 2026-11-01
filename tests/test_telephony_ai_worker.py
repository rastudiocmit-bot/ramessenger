"""Real audio segmentation with synthetic providers and durable receipt state."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from center_messenger.ai_yandex import YandexError
from center_messenger.telephony_ai_worker import TelephoneWorker
from center_messenger.telephony_ai_store import snapshot
from center_messenger.telephony_agent import defaults
from center_messenger.telephony_media import RecordingCache
from center_messenger import telephony_archive
from test_telephony_media import wav


class Rows:
    def __init__(self,value):self.value=value
    def mappings(self):return self
    def one_or_none(self):return self.value


class MemoryStore:
    def __init__(self):
        self.workspace=uuid4();agent=defaults(self.workspace) | {'version':1}
        self.run={'id':uuid4(),'call_id':uuid4(),'requested_by':uuid4(),'profile':snapshot({'gateway_config':{'telephony_agent':agent}},self.workspace),
                  'phase':'queued','transcript':None,'summary':None,'audio_hash':None,'gateway_hash':None,'part':0,'historical':False,
                  'created_at':telephony_archive.now_utc()}
        self.records={};self.posts=0;self.allow=True
        self.engine=Mock();self.clock=telephony_archive.now_utc
        self.call={'connected':True,'recording_ciphertext':'synthetic-cipher','connection_id':uuid4()}
    def claim(self):
        if self.run['phase'] in ('complete','error','needs_review'):return None
        for value in self.records.values():
            if value['status'] in ('sent','unknown'):
                self.run.update(phase='needs_review',error_code='provider_result_unknown');return None
        return {'workspace':self.workspace,'token':uuid4(),'run':deepcopy(self.run)}
    @contextmanager
    def tenant(self,*args):yield SimpleNamespace(execute=lambda *_args,**kwargs:Rows(self.call))
    def update(self,job,**values):self.run.update(values);job['run'].update(values);return True
    def operations(self,job,prefix):return [deepcopy(value) for key,value in sorted(self.records.items()) if key.startswith(prefix) and value['status']=='complete']
    def reuse_asr(self,*args):return None
    def receipt(self,job,operation,input_hash,estimate,seconds=0):
        if not self.allow:return None
        if operation in self.records:return deepcopy(self.records[operation])
        self.records[operation]={'operation':operation,'input_hash':input_hash,'cost_rub':estimate,'audio_seconds':seconds,'status':'sent'}
        return {'status':'sent','new':True}
    def complete(self,job,operation,value,**kwargs):
        self.records[operation].update(status='complete',result=value,**kwargs)
        if not kwargs.get('usable',True):self.update(job,phase='needs_review',error_code='provider_result_incomplete')
        return True
    def uncertain(self,job,code='provider_result_unknown'):
        for value in self.records.values():
            if value['status']=='sent':value['status']='unknown'
        self.update(job,phase='needs_review',error_code=code)
    def release(self,*args):pass
    def pause_automatic(self,*args):pass


class TelephoneWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store=MemoryStore()
        self.client=Mock()
        self.client.recognize.return_value='Предложили время, подтверждения пока нет.'
        self.client.complete.return_value={'text':'Обсудили время занятия; запись пока не подтверждена.','input_tokens':100,'output_tokens':20,'truncated':False}
        self.agent=SimpleNamespace(client=self.client,deepseek=Mock(),mimo=Mock())
        self.vault=Mock(open_recording=Mock(return_value='synthetic-provider-url'))
        self.cache=RecordingCache(reader=Mock(return_value=wav(27)))
        self.addCleanup(self.cache.close)
        self.worker=self.new_worker()
    def new_worker(self):
        value=TelephoneWorker(SimpleNamespace(engine=Mock()),self.vault,store=self.store,cache=self.cache,agent=self.agent)
        value.credentials=lambda job:{'yandex':'synthetic-key','folder':'synthetic-folder'}
        return value
    def finish(self,limit=100):
        for index in range(limit):
            if not self.worker.run_next():return
        self.fail('job did not finish within bounded steps')
    def test_both_stereo_channels_and_last_short_part_produce_one_saved_description(self):
        self.finish()
        self.assertEqual(self.store.run['phase'],'complete');self.assertEqual(self.client.recognize.call_count,4)
        self.assertIn('Канал 1',self.store.run['transcript']);self.assertIn('Канал 2',self.store.run['transcript'])
        self.assertEqual(self.client.complete.call_count,1)
        for call in self.client.recognize.call_args_list:self.assertLessEqual(len(call.args[0]),800000)
        self.assertNotIn('synthetic-provider-url',str(self.client.recognize.call_args_list))
    def test_restart_after_a_completed_part_does_not_repeat_its_paid_request(self):
        self.worker.run_next();self.assertEqual(self.client.recognize.call_count,1)
        self.worker=self.new_worker();self.finish();self.assertEqual(self.client.recognize.call_count,4)
    def test_unknown_provider_result_stops_instead_of_retrying_after_restart(self):
        self.client.recognize.side_effect=YandexError('network')
        self.worker.run_next();self.worker=self.new_worker();self.worker.run_next()
        self.assertEqual(self.client.recognize.call_count,1);self.assertEqual(self.store.run['phase'],'needs_review')
        self.agent.deepseek.complete.assert_not_called();self.agent.mimo.recognize.assert_not_called()
    def test_receipt_committed_before_a_crash_is_not_replayed(self):
        self.store.records['asr:00000:0']={'status':'sent','operation':'asr:00000:0'}
        self.worker.run_next();self.client.recognize.assert_not_called()
        self.assertEqual(self.store.run['phase'],'needs_review')
    def test_pause_at_the_paid_boundary_does_not_send_audio(self):
        self.store.allow=False;self.worker.run_next();self.client.recognize.assert_not_called()
        self.assertEqual(self.store.records,{})
        self.store.allow=True;self.finish();self.assertEqual(self.store.run['phase'],'complete')
    def test_a_truncated_paid_summary_remains_reviewable_after_restart(self):
        self.client.complete.return_value['truncated']=True
        self.finish();self.assertEqual(self.store.run['phase'],'needs_review')
        self.worker=self.new_worker();self.worker.run_next();self.assertEqual(self.client.complete.call_count,1)
        self.assertIsNone(self.store.run['summary'])
    def test_longer_than_three_minutes_is_processed_without_dictation_truncation(self):
        self.cache.reader.return_value=wav(205)
        self.finish()
        self.assertEqual(self.client.recognize.call_count,18)
        self.assertIn('[03:20]',self.store.run['transcript']);self.assertEqual(self.store.run['phase'],'complete')
    def test_summary_receives_only_the_telephone_profile_and_no_role_invention_instruction(self):
        self.finish();options=self.client.complete.call_args.kwargs
        self.assertIn(self.store.run['profile']['prompt'],options['prompt'])
        self.assertIn(self.store.run['profile']['fixed_rules'],options['prompt'])
        self.assertFalse(options['rules']);self.assertIn('роль не подтверждена',self.client.complete.call_args.args[0])

    def test_grok_summarizes_in_background_using_telephone_profile_and_separate_asr(self):
        self.store.run['profile']['model']='grok-4.7'
        self.agent.grok=Mock();self.agent.grok.complete.return_value=self.client.complete.return_value
        self.worker.credentials=lambda job:{'yandex':'asr-key','grok':'grok-key','folder':'folder','grok_proxy':'proxy-object'}
        self.finish()
        self.assertEqual(self.store.run['phase'],'complete');self.assertEqual(self.client.recognize.call_count,4)
        self.agent.grok.complete.assert_called_once();options=self.agent.grok.complete.call_args.kwargs
        self.assertEqual(options['model'],'grok-4.7');self.assertEqual(options['api_key'],'grok-key')
        self.assertEqual(options['proxy'],'proxy-object')
        self.assertIn(self.store.run['profile']['fixed_rules'],options['prompt'])
        self.client.complete.assert_not_called();self.agent.mimo.complete.assert_not_called()
        self.assertGreater(self.store.records['summary:final:00000']['cost'],0)

    def test_grok_recognizes_all_parts_through_saved_proxy_and_keeps_summary_independent(self):
        from center_messenger import ai_grok
        self.store.run['profile']['recognition_provider']='grok'
        self.agent.grok=Mock()
        self.agent.grok.recognize.return_value={'text':'Не записывайте на завтра.'}
        self.worker.credentials=lambda job:{'grok':'asr-key','grok_proxy':'saved-proxy','yandex':'summary-key','folder':'folder'}
        self.finish()
        self.assertEqual(self.store.run['phase'],'complete')
        self.assertEqual(self.agent.grok.recognize.call_count,4)
        for call in self.agent.grok.recognize.call_args_list:
            self.assertEqual(call.kwargs,{'api_key':'asr-key','proxy':'saved-proxy','language':'ru'})
            self.assertLessEqual(len(call.args[0]),800000)
        self.assertIn('Не записывайте',self.store.run['transcript'])
        self.assertIn('Канал 2 (роль не подтверждена)',self.store.run['transcript'])
        self.client.recognize.assert_not_called();self.agent.mimo.recognize.assert_not_called()
        self.agent.grok.complete.assert_not_called();self.client.complete.assert_called_once()
        costs=[r['cost'] for op,r in self.store.records.items() if op.startswith('asr:')]
        self.assertEqual(costs,[ai_grok.audio_cost(25)]*2+[ai_grok.audio_cost(2)]*2)
        self.worker=self.new_worker();self.worker.run_next()
        self.assertEqual(self.agent.grok.recognize.call_count,4)

    def test_grok_asr_uncertain_result_is_never_retried_or_sent_to_another_gateway(self):
        self.store.run['profile']['recognition_provider']='grok'
        self.agent.grok=Mock();self.agent.grok.recognize.side_effect=YandexError('rate_limited',429)
        self.worker.credentials=lambda job:{'grok':'asr-key','yandex':'summary-key','folder':'folder'}
        self.worker.run_next();self.worker.run_next()
        self.agent.grok.recognize.assert_called_once()
        self.assertEqual(self.store.run['phase'],'needs_review')
        self.client.recognize.assert_not_called();self.agent.mimo.recognize.assert_not_called()
        self.client.complete.assert_not_called()

    def test_grok_asr_budget_pause_prevents_upload(self):
        self.store.run['profile']['recognition_provider']='grok'
        self.store.allow=False;self.agent.grok=Mock()
        self.worker.credentials=lambda job:{'grok':'asr-key','yandex':'summary-key','folder':'folder'}
        self.worker.run_next()
        self.agent.grok.recognize.assert_not_called();self.assertEqual(self.store.records,{})

    def test_grok_asr_silence_does_not_generate_a_paid_request(self):
        from unittest.mock import patch
        self.store.run['profile']['recognition_provider']='grok';self.agent.grok=Mock()
        self.worker.credentials=lambda job:{'grok':'asr-key','yandex':'summary-key','folder':'folder'}
        with patch('center_messenger.telephony_ai_worker.decode_part',return_value=[bytes(3200)]):
            self.worker.run_next()
        self.agent.grok.recognize.assert_not_called()
        self.assertEqual(self.store.records['asr:00000:0']['cost'],0)

    def test_mimo_accounts_keep_separate_routes_and_budget_modes(self):
        for provider in ('mimo','mimo_plan'):
            with self.subTest(provider=provider):
                self.store=MemoryStore();self.worker=self.new_worker();self.agent.mimo.reset_mock()
                self.store.run['profile']['recognition_provider']=provider
                self.agent.mimo.recognize.return_value={'text':'Обсудили занятие.'}
                self.worker.credentials=lambda job:{provider:'separate-key','gateways':{},'yandex':'summary','folder':'folder'}
                self.finish()
                self.assertEqual(self.store.run['phase'],'complete')
                self.assertEqual(self.agent.mimo.recognize.call_count,4)
                options=self.agent.mimo.recognize.call_args.kwargs
                self.assertEqual(options['plan'],provider=='mimo_plan');self.assertEqual(options['api_key'],'separate-key')
                cost=self.store.records['asr:00000:0']['cost']
                self.assertEqual(cost==0,provider=='mimo_plan')
        self.client.recognize.assert_not_called()

    def test_invalid_asr_profile_and_missing_grok_key_fail_before_receipt_or_network(self):
        from center_messenger.auth_security import AuthError
        self.agent._settings=Mock(return_value={})
        self.agent._credentials=Mock(return_value={'yandex':'summary-key','folder':'folder'})
        for provider in ('deepseek','invented','grok'):
            self.store.run['profile']['recognition_provider']=provider
            with self.subTest(provider=provider),self.assertRaises(AuthError):
                TelephoneWorker.credentials(self.worker,self.store.claim())
        self.assertEqual(self.store.records,{})
        self.client.recognize.assert_not_called();self.agent.mimo.recognize.assert_not_called()

    def test_grok_asr_alone_stamps_proxy_and_invalidates_on_connection_change(self):
        from center_messenger.auth_security import AuthError
        self.store.run['profile']['recognition_provider']='grok'
        row={'key_ciphertext':'summary','folder_id':'folder','gateway_config':{'grok':{
            'key_ciphertext':'asr','proxy_enabled':True,'proxy_ciphertext':'first','proxy_protocol':'socks5'}}}
        self.agent._settings=lambda *_:row
        self.agent._credentials=Mock(return_value={'grok':'asr-key','yandex':'summary-key','folder':'folder'})
        job=self.store.claim();TelephoneWorker.credentials(self.worker,job)
        self.assertEqual(self.agent._credentials.call_args.kwargs['selected'],{'yandex','grok'})
        row['gateway_config']['grok']['proxy_ciphertext']='replacement'
        with self.assertRaises(AuthError):TelephoneWorker.credentials(self.worker,job)


    def test_grok_key_change_invalidates_background_gateway_snapshot(self):
        from center_messenger.auth_security import AuthError
        self.store.run['profile']['model']='grok-4.7'
        row={'folder_id':'folder','key_ciphertext':'asr-cipher','gateway_config':{'grok':{'key_ciphertext':'first'}}}
        self.agent._settings=lambda *_:row
        self.agent._credentials=lambda *_args,**_kwargs:{'grok':'grok-key','yandex':'asr-key','folder':'folder'}
        job=self.store.claim()
        TelephoneWorker.credentials(self.worker,job)
        row['gateway_config']['grok']['checks']={'text':{'ok':True}}
        TelephoneWorker.credentials(self.worker,job)  # a connectivity check does not change credentials
        row['gateway_config']['grok']['key_ciphertext']='replacement'
        with self.assertRaises(AuthError) as failure:TelephoneWorker.credentials(self.worker,job)
        self.assertEqual(failure.exception.status,409)

    def test_grok_proxy_change_invalidates_background_gateway_snapshot_before_another_paid_call(self):
        from center_messenger.auth_security import AuthError
        self.store.run['profile']['model']='grok-4.7'
        row={'folder_id':'folder','key_ciphertext':'asr-cipher','gateway_config':{'grok':{'key_ciphertext':'key',
            'proxy_enabled':True,'proxy_ciphertext':'first-proxy','proxy_protocol':'socks5'}}}
        self.agent._settings=lambda *_:row
        self.agent._credentials=lambda *_args,**_kwargs:{'grok':'key','yandex':'asr','folder':'folder','grok_proxy':'proxy'}
        job=self.store.claim();TelephoneWorker.credentials(self.worker,job)
        row['gateway_config']['grok']['proxy_ciphertext']='replacement-proxy'
        with self.assertRaises(AuthError) as error:TelephoneWorker.credentials(self.worker,job)
        self.assertEqual(error.exception.status,409);self.client.complete.assert_not_called()

    def test_recording_wait_is_limited_to_twenty_four_hours_without_paid_requests(self):
        self.store.call['recording_ciphertext']=None
        self.worker.run_next();self.assertEqual(self.store.run['phase'],'waiting_recording')
        self.store.run['created_at']-=timedelta(hours=24)
        self.worker.run_next();self.assertEqual(self.store.run['phase'],'error')
        self.client.recognize.assert_not_called();self.cache.reader.assert_not_called()


if __name__=='__main__':unittest.main()
