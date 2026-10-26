"""MiMo wire contracts, bounded audio, account isolation and safe failures."""
import base64
import io
import json
import unittest
import urllib.error
import wave

from center_messenger import ai_mimo as m, ai_gateways as g
from center_messenger.ai_yandex import silence, YandexError
from test_ai_yandex import FakeOpener, http_error
from test_ai_voice import service, blank_settings, ACTOR, WS


def reply(text='Ready', finish='stop'):
    return {'choices':[{'message':{'content':text},'finish_reason':finish}],
            'usage':{'prompt_tokens':4,'completion_tokens':2}}


def complete(client, **kwargs):
    return client.complete('Hello', prompt='Reply.', model='mimo-v2.6-flash', temperature=0,
                           api_key='sk-'+'x'*30, **kwargs)


class MimoTests(unittest.TestCase):
    def test_chat_contract_disables_thinking_and_uses_completion_limit(self):
        wire=FakeOpener(reply());result=complete(m.MimoClient(wire))
        request,timeout=wire.requests[0];body=json.loads(request.data)
        self.assertEqual(request.full_url,m.PAYG_URL+'/chat/completions')
        self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(body['max_completion_tokens'],1500)
        self.assertNotIn('max_tokens',body)
        self.assertEqual((result['input_tokens'],result['output_tokens']),(4,2))

    def test_plan_alias_uses_own_key_and_never_retries_payg(self):
        wire=FakeOpener(error=http_error(402));client=m.MimoClient(wire)
        with self.assertRaises(YandexError) as error:
            client.complete('Hello',prompt='Reply',model='mimo-plan-v2.6-pro',temperature=0,
                api_key='tp-'+'x'*30,base_url=m.PLAN_URL)
        self.assertEqual(error.exception.code,'no_balance')
        self.assertEqual(len(wire.requests),1)
        request=wire.requests[0][0]
        self.assertEqual(request.full_url,m.PLAN_URL+'/chat/completions')
        self.assertEqual(json.loads(request.data)['model'],'mimo-v2.6-pro')

    def test_responses_contract_and_incomplete_result(self):
        wire=FakeOpener({'status':'incomplete','output':[{'type':'message','content':[{'type':'output_text','text':'Hello'}]}],
                         'usage':{'input_tokens':3,'output_tokens':1}})
        result=complete(m.MimoClient(wire),protocol='responses',rules=False)
        body=json.loads(wire.requests[0][0].data)
        self.assertTrue(result['truncated']);self.assertEqual(body['reasoning'],{'effort':'none'})
        self.assertEqual(body['instructions'],'Reply.')
        self.assertNotIn('previous_response_id',body)

    def test_asr_wraps_all_three_minutes_as_one_wav_without_dropping_tail(self):
        wire=FakeOpener(reply('Hello'));audio=silence(180)
        m.MimoClient(wire).recognize(audio,api_key='fake',base_url=m.PLAN_URL,plan=True,language='en')
        body=json.loads(wire.requests[0][0].data)
        self.assertEqual(body['model'],'mimo-v2.5-asr')
        data=body['messages'][0]['content'][0]['input_audio']['data']
        self.assertLess(len(data),10_000_000)
        with wave.open(io.BytesIO(base64.b64decode(data.split(',',1)[1])),'rb') as wav:
            self.assertEqual(wav.getframerate(),16000);self.assertEqual(wav.getnframes(),180*16000)
            self.assertEqual(wav.readframes(wav.getnframes()),audio)
        self.assertEqual(body['asr_options'],{'language':'en'})

    def test_russian_asr_and_unknown_hosts_are_rejected_before_sending_a_key(self):
        wire=FakeOpener(reply());client=m.MimoClient(wire)
        for value in ('https://evil.example/v1',m.PLAN_URL+'/../v1',m.PAYG_URL+'?x=1'):
            with self.assertRaises(YandexError):complete(client,base_url=value)
        with self.assertRaises(YandexError) as error:
            client.recognize(silence(1),api_key='fake',language='ru')
        self.assertEqual(error.exception.code,'speech_language');self.assertEqual(wire.requests,[])
        self.assertIsNone(m.NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_tts_requires_valid_wav_and_uses_assistant_text(self):
        output=io.BytesIO()
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(silence(1))
        payload={'choices':[{'message':{'audio':{'data':base64.b64encode(output.getvalue()).decode()}},'finish_reason':'stop'}]}
        wire=FakeOpener(payload);result=m.MimoClient(wire).synthesize('Hello',api_key='fake')
        body=json.loads(wire.requests[0][0].data)
        self.assertEqual(body['messages'],[{'role':'assistant','content':'Hello'}])
        self.assertEqual(body['audio'],{'format':'wav','voice':'Mia'})
        self.assertEqual(result['seconds'],1)
        payload['choices'][0]['message']['audio']['data']=base64.b64encode(b'not audio').decode()
        with self.assertRaises(YandexError):m.MimoClient(FakeOpener(payload)).synthesize('Hello',api_key='fake')

    def test_only_rate_limit_is_retried_with_bounded_backoff(self):
        wire=FakeOpener(error=http_error(429));waits=[]
        with self.assertRaises(YandexError) as error:complete(m.MimoClient(wire,sleep=waits.append))
        self.assertEqual(error.exception.code,'rate_limited');self.assertEqual(waits,[1]);self.assertEqual(len(wire.requests),2)

    def test_malformed_payload_and_unsafe_finish_reasons_fail_closed(self):
        for payload in (b'no',[],{'choices':[]},reply(finish='tool_calls'),reply(finish='content_filter')):
            with self.assertRaises(YandexError):complete(m.MimoClient(FakeOpener(payload)))
        self.assertTrue(complete(m.MimoClient(FakeOpener(reply(finish='length'))))['truncated'])

    def test_gateway_public_metadata_never_contains_ciphertexts(self):
        row=blank_settings()|{'mimo_key_ciphertext':'sealed-secret','gateway_config':{'mimo':{'key_hint':'abcd'}}}
        result=g.public(row)
        self.assertTrue(result['mimo']['configured']);self.assertNotIn('sealed-secret',json.dumps(result))
        row.update(voice_enabled=True,gateway_config={'recognition_provider':'mimo'})
        self.assertTrue(g.ready(row));row['mimo_key_ciphertext']=None;self.assertFalse(g.ready(row))

    def test_normalization_routes_plan_only_to_plan_key_and_protocol(self):
        ai,db=service();wire=FakeOpener(reply('Hello'));ai.mimo=m.MimoClient(wire)
        agent={'model':'mimo-plan-v2.6-pro','prompt':'Reply.','temperature':0,'task':'text_processing'}
        creds={'mimo_plan':'tp-test','mimo':'sk-other','folder':'','gateways':{'mimo_plan':{'base_url':m.PLAN_URL,'protocol':'chat'}}}
        result=ai._normalize('Hello',agent,creds)
        request=wire.requests[0][0]
        self.assertEqual(request.get_header('Api-key'),'tp-test');self.assertEqual(result[2],0)
        self.assertNotIn('расшифровка',json.loads(request.data)['messages'][0]['content'])
        self.assertIn('mimo',__import__('center_messenger.ai_voice',fromlist=['CATALOG']).CATALOG['mimo-v2.6-pro']['provider'])

    def test_sealed_gateway_key_cannot_be_decrypted_for_another_account_or_workspace(self):
        from center_messenger.connector_vault import ConnectorVault
        from center_messenger.auth_security import AuthError
        from uuid import uuid5,NAMESPACE_URL,uuid4
        vault=ConnectorVault(None);vault._key=lambda:b't'*32
        identity=uuid5(NAMESPACE_URL,'center-messenger:mimo_plan')
        cipher=vault.seal(WS,identity,{'api_key':'tp-synthetic'},purpose='mimo_plan')
        self.assertNotIn('tp-synthetic',cipher)
        self.assertEqual(vault.open(WS,identity,cipher,purpose='mimo_plan')['api_key'],'tp-synthetic')
        for workspace,purpose in ((uuid4(),'mimo_plan'),(WS,'mimo')):
            with self.assertRaises(AuthError):vault.open(workspace,identity,cipher,purpose=purpose)

    def test_selected_plan_key_does_not_decrypt_unrelated_broken_credentials(self):
        from center_messenger.connector_vault import ConnectorVault
        from uuid import uuid5,NAMESPACE_URL
        ai,db=service();ai.vault=ConnectorVault(None);ai.vault._key=lambda:b't'*32
        db.settings=blank_settings()|{'key_ciphertext':'broken-yandex','deepseek_key_ciphertext':'broken-deepseek',
            'mimo_key_ciphertext':'broken-payg','mimo_plan_key_ciphertext':ai.vault.seal(WS,
                uuid5(NAMESPACE_URL,'center-messenger:mimo_plan'),{'api_key':'tp-synthetic'},purpose='mimo_plan')}
        result=ai._credentials(db,WS,yandex=False,selected={'mimo_plan'})
        self.assertEqual(result['mimo_plan'],'tp-synthetic');self.assertIsNone(result['mimo']);self.assertIsNone(result['yandex'])

    def test_voice_transcription_uses_selected_mimo_asr_without_yandex_credentials(self):
        from center_messenger.connector_vault import ConnectorVault
        from uuid import uuid5,NAMESPACE_URL
        ai,db=service(agent=False);ai.vault=ConnectorVault(None);ai.vault._key=lambda:b't'*32
        db.settings=blank_settings()|{'voice_enabled':True,'key_ciphertext':'unrelated-invalid-key',
            'mimo_plan_key_ciphertext':ai.vault.seal(WS,uuid5(NAMESPACE_URL,'center-messenger:mimo_plan'),
                {'api_key':'tp-synthetic'},purpose='mimo_plan'),
            'gateway_config':{'recognition_provider':'mimo_plan','recognition_language':'en'}}
        wire=FakeOpener(reply('Recognized speech'));ai.mimo=m.MimoClient(wire)
        result=ai.transcribe(ACTOR,WS,silence(1))
        self.assertEqual(result['text'],'Recognized speech');self.assertEqual(ai.client.calls,[])
        self.assertEqual(wire.requests[0][0].full_url,m.PLAN_URL+'/chat/completions')
        self.assertEqual(db.usage[-1]['c'],0)

    def test_gateway_routes_require_admin_and_do_not_accept_russian_mimo(self):
        ai,db=service()
        with self.assertRaises(Exception) as denied:
            ai.gateways.routes(ACTOR,WS,{'recognition_provider':'mimo','recognition_language':'ru','synthesis_provider':'disabled','synthesis_voice':'Mia'})
        self.assertEqual(denied.exception.status,403)
        ai.accounts.role='owner';db.settings=blank_settings()
        with self.assertRaises(Exception) as denied:
            ai.gateways.routes(ACTOR,WS,{'recognition_provider':'mimo','recognition_language':'ru','synthesis_provider':'disabled','synthesis_voice':'Mia'})
        self.assertEqual(denied.exception.status,422)

if __name__=='__main__':unittest.main()
