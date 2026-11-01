"""Synthetic xAI transport, sealed workspace settings and text-agent routing."""
import io
import json
from email import policy
from email.parser import BytesParser
import wave
import unittest
from copy import deepcopy
from uuid import uuid4
from unittest.mock import Mock, patch

# Installs the existing minimal account/SQL stand-ins in a bare local runtime.
from test_ai_voice import service, blank_settings, ACTOR, WS, FakeDB, Result
from test_ai_yandex import FakeOpener, http_error
from center_messenger import ai_grok as g, ai_gateways, ai_voice
from center_messenger.ai_yandex import YandexError, FIXED_RULES
from center_messenger.auth_security import AuthError
from center_messenger.connector_vault import ConnectorVault

KEY = 'xai-' + 'synthetic' * 5


def response(**changes):
    return {'status': 'completed', 'output': [
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'private reasoning'}]},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Готово.'}]}],
        'usage': {'prompt_tokens': 32, 'completion_tokens': 9, 'total_tokens': 151}} | changes


def complete(client, **options):
    return client.complete('Пример', prompt='Ответь кратко.', model='grok-4.7',
                           temperature=.1, api_key=KEY, **options)


class TransportTests(unittest.TestCase):
    def test_telephone_asr_uploads_bounded_wav_with_options_before_file(self):
        audio = b'\x01\x00' * 16000
        wire = FakeOpener({'text': 'Не записывайте на завтра.', 'duration': 1})
        result = g.GrokClient(wire).recognize(audio, api_key=KEY)
        request, timeout = wire.requests[0]
        self.assertEqual(request.full_url, g.BASE_URL + '/stt')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        form = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + request.get_header('Content-type') + '\r\n\r\n').encode() + request.data)
        parts = list(form.iter_parts())
        self.assertEqual([p.get_param('name', header='content-disposition') for p in parts],
                         ['model', 'language', 'format', 'filler_words', 'diarize', 'file'])
        self.assertEqual([p.get_content().strip() for p in parts[:-1]],
                         [g.ASR_MODEL, 'ru', 'false', 'true', 'false'])
        with wave.open(io.BytesIO(parts[-1].get_payload(decode=True)), 'rb') as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
            self.assertEqual(wav.readframes(16000), audio)
        self.assertEqual(result, {'text': 'Не записывайте на завтра.'})
        self.assertEqual(timeout, 25)
        self.assertLess(len(request.data), 810000)

    def test_asr_rejects_urls_invalid_pcm_and_language_before_network(self):
        wire = FakeOpener({'text': 'x'})
        for audio in ('https://private-recording.test/audio', None, b'', b'0' * 3199,
                      b'0' * 3201, b'0' * 800002):
            with self.subTest(kind=type(audio).__name__), self.assertRaises(YandexError):
                g.GrokClient(wire).recognize(audio, api_key=KEY)
        with self.assertRaises(YandexError):
            g.GrokClient(wire).recognize(b'0' * 3200, api_key=KEY, language='invented')
        self.assertEqual(wire.requests, [])

    def test_asr_accepts_empty_transcript_and_rejects_malformed_output(self):
        self.assertEqual(g.GrokClient(FakeOpener({'text': ''})).recognize(b'0' * 3200, api_key=KEY), {'text': ''})
        for payload in ({}, {'text': None}, {'text': 42}, {'text': 'x' * 20001}, b'x' * 2_000_001):
            wire = FakeOpener(payload)
            with self.subTest(kind=type(payload).__name__), self.assertRaises(YandexError):
                g.GrokClient(wire).recognize(b'0' * 3200, api_key=KEY)
            self.assertEqual(len(wire.requests), 1)

    def test_asr_uses_saved_proxy_without_direct_fallback_or_retry(self):
        from center_messenger import grok_proxy_probe as proxy
        config = proxy.parse('8.8.4.4:8080:u:synthetic-password', 'socks5')
        direct, wire = FakeOpener({}), FakeOpener({'text': 'Распознано'})
        with patch.object(proxy, 'ProxyOpener', return_value=Mock(open=wire)):
            self.assertEqual(g.GrokClient(direct).recognize(b'0' * 3200, api_key=KEY, proxy=config)['text'], 'Распознано')
        self.assertEqual(wire.requests[0][0].full_url, g.BASE_URL + '/stt')
        for error in (proxy.ProxyFailure('proxy_auth'), http_error(403), http_error(429), TimeoutError('secret')):
            transport = Mock(open=Mock(side_effect=error))
            with patch.object(proxy, 'ProxyOpener', return_value=transport):
                with self.assertRaises(YandexError):
                    g.GrokClient(direct).recognize(b'0' * 3200, api_key=KEY, proxy=config)
            transport.open.assert_called_once()
        self.assertEqual(direct.requests, [])

    def test_asr_budget_estimates_are_nonzero_and_round_short_fragments_up(self):
        self.assertAlmostEqual(g.audio_cost(3600), 9)
        self.assertEqual(g.audio_cost(.1), g.audio_cost(1))
        self.assertGreater(g.audio_cost(25), 0)

    def test_responses_uses_bearer_fixed_origin_no_storage_or_tools(self):
        wire = FakeOpener(response()); result = complete(g.GrokClient(wire), rules=False)
        request, timeout = wire.requests[0]; body = json.loads(request.data)
        self.assertEqual(request.full_url, g.BASE_URL + '/responses')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        self.assertFalse(body['store']); self.assertFalse(body['stream'])
        self.assertEqual(body['input'][0], {'role': 'system', 'content': 'Ответь кратко.'})
        self.assertEqual(body['max_output_tokens'], 1500)
        self.assertEqual(body['reasoning'], {'effort': 'low'})
        self.assertNotIn('tools', body); self.assertNotIn('thinking', body)
        self.assertEqual(result['text'], 'Готово.'); self.assertNotIn('private', result['text'])
        self.assertEqual((result['input_tokens'], result['output_tokens']), (32, 119))
        self.assertEqual(timeout, 25)

    def test_dictation_rules_and_requested_timeout_are_preserved(self):
        wire = FakeOpener(response()); complete(g.GrokClient(wire), timeout=8)
        self.assertIn(FIXED_RULES, json.loads(wire.requests[0][0].data)['input'][0]['content'])
        self.assertEqual(wire.requests[0][1], 8)

    def test_supported_text_models_are_intersected_with_accessible_ids_and_aliases(self):
        payload = {'models': [
            {'id': 'versioned', 'aliases': ['grok-4.7'], 'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
            {'id': 'grok-4.6', 'input_modalities': ['text'], 'output_modalities': ['text']},
            {'id': 'grok-imagine-image', 'input_modalities': ['text'], 'output_modalities': ['image']}]}
        wire = FakeOpener(payload)
        self.assertEqual(g.GrokClient(wire).models(api_key=KEY), ['grok-4.7', 'grok-4.6'])
        self.assertEqual(wire.requests[0][0].get_method(), 'GET')
        self.assertEqual(wire.requests[0][0].full_url, g.BASE_URL + '/language-models')
        self.assertEqual(wire.requests[0][1], 10)

    def test_missing_models_and_malformed_catalogues_are_distinct(self):
        self.assertEqual(g.GrokClient(FakeOpener({'models': []})).models(api_key=KEY), [])
        for payload in ({}, {'models': {}}, {'models': [None]}):
            with self.subTest(payload=payload), self.assertRaises(YandexError):
                g.GrokClient(FakeOpener(payload)).models(api_key=KEY)

    def test_http_failures_are_sanitized_and_never_retried_or_redirected(self):
        for status, code in ((401, 'unauthorized'), (403, 'forbidden'), (402, 'no_balance'),
                             (429, 'rate_limited'), (404, 'model_unknown'), (400, 'bad_request'),
                             (503, 'unavailable'), (302, 'unavailable')):
            with self.subTest(status=status):
                wire = FakeOpener(error=http_error(status))
                with self.assertRaises(YandexError) as failure: complete(g.GrokClient(wire))
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn(KEY, str(failure.exception)); self.assertEqual(len(wire.requests), 1)
        self.assertIsNone(g.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_network_failure_is_safe_and_one_attempt(self):
        wire = FakeOpener(error=TimeoutError('sensitive transport detail'))
        with self.assertRaises(YandexError) as failure: complete(g.GrokClient(wire))
        self.assertEqual(failure.exception.code, 'network'); self.assertEqual(len(wire.requests), 1)
        self.assertNotIn('sensitive', str(failure.exception))

    def test_incomplete_response_keeps_billable_tokens_but_is_not_accepted_as_complete(self):
        result = complete(g.GrokClient(FakeOpener(response(status='incomplete', output=[]))))
        self.assertTrue(result['truncated']); self.assertEqual(result['output_tokens'], 119)

    def test_standard_responses_token_names_are_supported(self):
        result = complete(g.GrokClient(FakeOpener(response(usage={'input_tokens': 3, 'output_tokens': 8}))))
        self.assertEqual((result['input_tokens'], result['output_tokens']), (3, 8))

    def test_oversized_malformed_empty_and_failed_responses_fail_closed(self):
        for payload in (b'x' * 2_000_001, b'not json', [], {'error': {'message': KEY}},
                        response(status='failed'), response(output=[]), response(usage={}),
                        response(usage={'input_tokens': -1, 'output_tokens': 2})):
            with self.subTest(kind=type(payload).__name__), self.assertRaises(YandexError):
                complete(g.GrokClient(FakeOpener(payload)))

    def test_unknown_model_is_rejected_before_network(self):
        wire = FakeOpener(response())
        with self.assertRaises(YandexError):
            g.GrokClient(wire).complete('x', prompt='x', model='grok-imagine-image', temperature=0, api_key=KEY)
        self.assertEqual(wire.requests, [])

    def test_proxy_is_scoped_to_one_request_and_never_falls_back_to_direct(self):
        from center_messenger import grok_proxy_probe as proxy
        config=proxy.parse('8.8.4.4:8080:synthetic-user:synthetic-password','socks5')
        direct,wire=FakeOpener(response()),FakeOpener(response())
        transport=Mock(open=wire)
        with patch.object(proxy,'ProxyOpener',return_value=transport) as factory:
            result=complete(g.GrokClient(direct),proxy=config,rules=False)
        factory.assert_called_once_with(config,allow_inference=True)
        self.assertEqual(result['text'],'Готово.');self.assertEqual(direct.requests,[])
        self.assertEqual(json.loads(wire.requests[0][0].data)['model'],'grok-4.7')
        transport=Mock();transport.open.side_effect=proxy.ProxyFailure('proxy_auth')
        with patch.object(proxy,'ProxyOpener',return_value=transport):
            with self.assertRaises(YandexError) as error:complete(g.GrokClient(direct),proxy=config)
        self.assertEqual(error.exception.code,'proxy_auth');transport.open.assert_called_once()
        self.assertEqual(direct.requests,[])

    def test_proxy_http_status_is_checked_even_when_transport_does_not_raise_http_error(self):
        from test_ai_yandex import FakeResponse
        reply=FakeResponse(b'{"error":"private-provider-text"}');reply.status=403
        with self.assertRaises(YandexError) as error:complete(g.GrokClient(lambda *_args,**_kw:reply))
        self.assertEqual(error.exception.code,'forbidden');self.assertNotIn('private',str(error.exception))


class GatewayDB(FakeDB):
    def execute(self, sql, params=None):
        sql, params = str(sql), params or {}
        if sql.startswith('INSERT INTO ai_settings(workspace_id)'):
            self.settings = self.settings or blank_settings()
            return Result()
        if sql.startswith('INSERT INTO ai_settings(workspace_id,gateway_config'):
            self.settings = (self.settings or blank_settings()) | {'gateway_config': json.loads(params['g'])}
            return Result()
        if "ORDER BY (task='tasks')" in sql:
            return Result(self.agents[:1])
        return super().execute(sql, params)


class FakeGrok:
    def __init__(self, accounts): self.accounts, self.calls, self.callback, self.failure = accounts, [], None, None
    def models(self, **kwargs):
        self.calls.append(('models', kwargs))
        if self.failure: raise YandexError(self.failure)
        return ['grok-4.7']
    def complete(self, content, **kwargs):
        self.calls.append(('complete', kwargs))
        if self.callback: self.callback()
        return {'text': 'Готово.', 'input_tokens': 4, 'output_tokens': 6, 'truncated': False}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.ai, old = service(role='owner')
        self.db = GatewayDB(); self.db.settings = deepcopy(old.settings)
        self.ai.accounts.db = self.db
        self.ai.vault = ConnectorVault(None); self.ai.vault._key = lambda: b'g' * 32
        self.ai.grok = FakeGrok(self.ai.accounts)

    def save(self, **data): return self.ai.gateways.save(ACTOR, WS, 'grok', data)
    def verify(self): return self.ai.gateways.verify(ACTOR, WS, 'grok', 'text')
    def save_proxy(self,**data):
        return self.ai.gateways.grok.save_proxy(ACTOR,WS,{'protocol':'socks5','enabled':True,**data})

    def test_save_is_encrypted_private_and_has_no_provider_requests(self):
        self.db.settings['gateway_config'] = {'telephony_agent': {'model': 'yandexgpt-5.1'}, 'mimo': {'key_hint': 'other'}}
        before = deepcopy(self.db.settings)
        result = self.save(api_key='  ' + KEY + '  ', base_url=g.BASE_URL, protocol='responses')
        encoded = json.dumps(result)
        self.assertNotIn(KEY, encoded); self.assertNotIn('key_ciphertext', encoded)
        self.assertNotIn(KEY, json.dumps(g.stored(self.db.settings)))
        self.assertEqual(result['gateways']['grok']['key_hint'], KEY[-4:])
        self.assertEqual(result['gateways']['grok']['status'], 'not_configured')
        for key in ('key_ciphertext', 'deepseek_key_ciphertext', 'voice_enabled'):
            self.assertEqual(self.db.settings[key], before[key])
        self.assertEqual(self.db.settings['gateway_config']['telephony_agent'], before['gateway_config']['telephony_agent'])
        self.assertEqual(self.ai.grok.calls, [])

    def test_blank_preserves_key_and_checks_replacement_and_deletion_reset_them(self):
        self.save(api_key=KEY); self.verify(); old = deepcopy(g.stored(self.db.settings))
        self.save(api_key=None); self.assertEqual(g.stored(self.db.settings), old)
        self.save(api_key=KEY + 'a'); self.assertNotIn('checks', g.stored(self.db.settings))
        self.save(clear_key=True)
        self.assertFalse(g.public(self.db.settings)['configured']); self.assertTrue(self.db.settings['voice_enabled'])

    def test_invalid_keys_conflicting_actions_and_other_origins_are_rejected_without_write(self):
        for data in ({'api_key': 'sk-' + 'a' * 40}, {'api_key': KEY + '\nsecret'},
                     {'api_key': KEY, 'clear_key': True}, {'api_key': KEY, 'base_url': 'https://evil.test'},
                     {'api_key': KEY, 'protocol': 'chat'}):
            before = deepcopy(self.db.settings)
            with self.subTest(data=list(data)), self.assertRaises(AuthError): self.save(**data)
            self.assertEqual(before, self.db.settings)

    def test_permissions_and_missing_key_block_network(self):
        self.ai.accounts.role = 'operator'
        with self.assertRaises(AuthError) as failure: self.save(api_key=KEY)
        self.assertEqual(failure.exception.status, 403)
        with self.assertRaises(AuthError): self.verify()
        self.ai.accounts.role = 'owner'
        with self.assertRaises(AuthError) as failure: self.verify()
        self.assertEqual(failure.exception.status, 409); self.assertEqual(self.ai.grok.calls, [])

    def test_verify_persists_models_and_cost_and_reports_failure_then_recovery(self):
        self.save(api_key=KEY)
        result = self.verify()
        self.assertTrue(result['ok']); self.assertEqual(result['gateways']['grok']['status'], 'active')
        self.assertEqual(result['gateways']['grok']['models'], ['grok-4.7'])
        self.assertGreater(self.db.usage[-1]['c'], 0)
        self.ai.grok.failure = 'no_balance'; result = self.verify()
        self.assertFalse(result['ok']); self.assertEqual(result['gateways']['grok']['checks']['text']['error_code'], 'no_balance')
        self.ai.grok.failure = None; self.assertTrue(self.verify()['ok'])

    def test_budget_and_audio_capabilities_block_requests(self):
        self.save(api_key=KEY); self.db.settings['monthly_limit_rub'] = 0
        with self.assertRaises(AuthError) as failure: self.verify()
        self.assertEqual(failure.exception.status, 429)
        for capability in ('recognition', 'synthesis'):
            with self.assertRaises(AuthError) as failure: self.ai.gateways.verify(ACTOR, WS, 'grok', capability)
            self.assertEqual(failure.exception.status, 422)
        self.assertEqual(self.ai.grok.calls, [])

    def test_late_check_cannot_verify_a_replaced_or_deleted_key(self):
        for data in ({'api_key': KEY + 'a'}, {'clear_key': True}):
            self.save(api_key=KEY)
            self.ai.grok.callback = lambda: self.save(**data)
            result = self.verify()
            self.assertTrue(result['stale']); self.assertFalse(result['ok'])
            self.assertNotIn('checks', g.stored(self.db.settings))

    def test_workspace_identity_and_provider_marker_are_bound_to_ciphertext(self):
        self.save(api_key=KEY); cipher = g.stored(self.db.settings)['key_ciphertext']
        self.assertEqual(g.credentials(self.ai, self.db.settings, WS), KEY)
        for workspace, identity in ((uuid4(), g.SECRET_ID), (WS, uuid4())):
            with self.assertRaises(AuthError): self.ai.vault.open(workspace, identity, cipher, purpose=g.PURPOSE)
        wrong = self.ai.vault.seal(WS, g.SECRET_ID, {'api_key': KEY, 'provider': 'other'}, purpose=g.PURPOSE)
        self.db.settings['gateway_config']['grok']['key_ciphertext'] = wrong
        with self.assertRaises(AuthError): g.credentials(self.ai, self.db.settings, WS)

    def test_selected_grok_key_never_decrypts_unrelated_broken_keys(self):
        self.save(api_key=KEY)
        self.db.settings.update(key_ciphertext='broken', deepseek_key_ciphertext='broken', mimo_key_ciphertext='broken')
        creds = self.ai._credentials(self.db, WS, yandex=False, selected={'grok'})
        self.assertEqual(creds['grok'], KEY)
        self.assertIsNone(creds['yandex']); self.assertIsNone(creds['deepseek']); self.assertIsNone(creds['mimo'])
        self.db.settings['gateway_config']['grok']['key_ciphertext'] = 'broken'
        self.assertIsNone(g.credentials(self.ai, self.db.settings, WS, {'yandex'}))

    def test_generic_agent_routes_exact_model_and_key_without_other_provider_calls(self):
        self.save(api_key=KEY)
        creds = self.ai._credentials(self.db, WS, yandex=False, selected={'grok'})
        agent = {'model': 'grok-4.6', 'prompt': 'Summarize.', 'temperature': .1, 'task': 'text_processing'}
        result = self.ai._normalize('Some text', agent, creds)
        self.assertFalse(result[3]); self.assertGreater(result[2], 0)
        self.assertEqual(self.ai.grok.calls[-1][1]['model'], 'grok-4.6')
        self.assertEqual(self.ai.grok.calls[-1][1]['api_key'], KEY)
        self.assertFalse(self.ai.grok.calls[-1][1]['rules'])
        self.assertEqual(self.ai.client.calls, []); self.assertEqual(self.ai.deepseek.calls, [])
        self.assertEqual(ai_voice.provider_of('grok-4.6'), 'grok')
        self.assertIn('grok-4.6', ai_voice.CATALOG)

    def test_saving_empty_initial_settings_uses_existing_json_namespace(self):
        self.db.settings = None
        self.assertTrue(self.save(api_key=KEY)['gateways']['grok']['configured'])
        self.assertFalse(self.db.settings['voice_enabled'])

    def test_all_generic_tasks_accept_grok_and_task_assist_uses_it(self):
        self.save(api_key=KEY)
        for task in ai_voice.TASKS:
            value=ai_voice._clean_agent({'name':'Grok agent','prompt':'Instruction','model':'grok-4.7','task':task})
            self.assertEqual(value['model'],'grok-4.7')
        self.db.agents=[{'id':uuid4(),'name':'Grok tasks','task':'tasks','prompt':'Studio rules',
                         'model':'grok-4.7','temperature':.1}]
        self.assertEqual(self.ai.assist(ACTOR,WS,'Task instruction','Conversation'),'Готово.')
        params=self.ai.grok.calls[-1][1]
        self.assertEqual(params['model'],'grok-4.7');self.assertEqual(params['api_key'],KEY)
        self.assertIn('Studio rules',params['prompt']);self.assertFalse(params['rules'])

    def test_proxy_is_encrypted_and_isolated_from_other_keys_and_phone_profile(self):
        self.save(api_key=KEY)
        self.db.settings['gateway_config']['telephony_agent']={'model':'grok-4.7','version':4}
        result=self.save_proxy(proxy='8.8.4.4:8080:synthetic-user:synthetic-password')
        item=g.stored(self.db.settings);public=result['gateways']['grok']['proxy']
        self.assertTrue(public['enabled']);self.assertEqual(public['endpoint'],'8.8.4.4:8080')
        for value in (str(result),json.dumps(self.db.settings['gateway_config'])):
            self.assertNotIn('synthetic-password',value);self.assertNotIn('synthetic-user',value)
        self.assertNotIn('proxy_ciphertext',str(result))
        creds=self.ai._credentials(self.db,WS,yandex=False,selected={'grok'})
        self.assertEqual(creds['grok_proxy'].protocol,'socks5');self.assertEqual(creds['grok_proxy'].password,'synthetic-password')
        self.assertEqual(self.db.settings['gateway_config']['telephony_agent']['version'],4)
        self.assertEqual(self.ai.grok.calls,[]);self.assertEqual(self.db.usage,[])
        cipher=item['proxy_ciphertext']
        for workspace,identity in ((uuid4(),g.PROXY_SECRET_ID),(WS,g.SECRET_ID)):
            with self.assertRaises(AuthError):self.ai.vault.open(workspace,identity,cipher,purpose=g.PURPOSE)

    def test_proxy_changes_invalidate_checks_blank_preserves_and_key_replacement_keeps_proxy(self):
        self.save(api_key=KEY);self.save_proxy(proxy='8.8.4.4:8080:u:synthetic-password');self.verify()
        item=deepcopy(g.stored(self.db.settings));self.assertTrue(item['checks']['text']['ok'])
        self.save_proxy(proxy=None);self.assertEqual(g.stored(self.db.settings),item)
        self.save_proxy(enabled=False);self.assertNotIn('checks',g.stored(self.db.settings))
        self.assertIsNone(g.proxy_credentials(self.ai,self.db.settings,WS,{'grok'}))
        self.save_proxy(enabled=True);self.verify()
        proxy_cipher=g.stored(self.db.settings)['proxy_ciphertext']
        self.save(api_key=KEY+'b');self.assertEqual(g.stored(self.db.settings)['proxy_ciphertext'],proxy_cipher)
        self.assertNotIn('checks',g.stored(self.db.settings))
        self.save_proxy(enabled=False,clear_proxy=True)
        self.assertFalse(g.public(self.db.settings)['proxy']['configured'])
        self.assertEqual(g.credentials(self.ai,self.db.settings,WS),KEY+'b')

    def test_broken_proxy_is_fail_closed_only_for_selected_grok(self):
        self.save(api_key=KEY);self.save_proxy(proxy='8.8.4.4:8080:u:synthetic-password')
        self.db.settings['gateway_config']['grok']['proxy_ciphertext']='broken'
        with self.assertRaises(AuthError):self.ai._credentials(self.db,WS,yandex=False,selected={'grok'})
        self.assertIsNone(g.proxy_credentials(self.ai,self.db.settings,WS,{'yandex'}))
        self.assertEqual(self.ai.grok.calls,[])

    def test_proxy_settings_require_admin_and_reject_unsafe_targets_and_conflicting_actions(self):
        self.save(api_key=KEY)
        for options in ({'proxy':'127.0.0.1:8080:u:p'},{'proxy':'8.8.4.4:8080:u:p','protocol':'auto'},
                {'proxy':'8.8.4.4:8080:u:p','clear_proxy':True},{'proxy':None}):
            before=deepcopy(self.db.settings)
            with self.assertRaises(AuthError):self.save_proxy(**options)
            self.assertEqual(self.db.settings,before)
        self.ai.accounts.role='operator'
        with self.assertRaises(AuthError) as error:self.save_proxy(proxy='8.8.4.4:8080:u:p')
        self.assertEqual(error.exception.status,403)

    def test_generic_text_and_task_agents_use_the_saved_proxy_without_other_provider_calls(self):
        self.save(api_key=KEY);self.save_proxy(proxy='8.8.4.4:8080:u:synthetic-password')
        creds=self.ai._credentials(self.db,WS,yandex=False,selected={'grok'})
        for task in ai_voice.TASKS:
            self.ai._normalize('Пример',{'model':'grok-4.7','task':task,'prompt':'Инструкция','temperature':.1},creds)
            self.assertEqual(self.ai.grok.calls[-1][1]['proxy'],creds['grok_proxy'])
        self.db.agents=[{'id':uuid4(),'name':'Tasks','task':'tasks','prompt':'Rules','model':'grok-4.7','temperature':.1}]
        self.ai.assist(ACTOR,WS,'Task instruction','Conversation')
        self.assertEqual(self.ai.grok.calls[-1][1]['proxy'],creds['grok_proxy'])
        self.assertEqual(self.ai.client.calls,[]);self.assertEqual(self.ai.deepseek.calls,[])

    def test_late_check_cannot_verify_a_switched_proxy_route(self):
        self.save(api_key=KEY);self.save_proxy(proxy='8.8.4.4:8080:u:synthetic-password')
        self.ai.grok.callback=lambda:self.save_proxy(enabled=False)
        result=self.verify()
        self.assertTrue(result['stale']);self.assertNotIn('checks',g.stored(self.db.settings))
        self.assertEqual(len(self.ai.grok.calls),2)


if __name__ == '__main__': unittest.main()
