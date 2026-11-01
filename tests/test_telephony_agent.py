"""Telephone-agent isolation and billing contracts; no provider calls."""
from contextlib import contextmanager
from copy import deepcopy
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from center_messenger.ai_voice import SECRET_ID, PURPOSE
from center_messenger.ai_yandex import YandexError
from center_messenger.auth_security import AuthError
from center_messenger.auth_security import csrf_token
from center_messenger.telephony_agent import TelephonyAgent, defaults, DEFAULT_PROMPT, FIXED_RULES
from center_messenger.telephony_api import PhoneAgentInput


class Result:
    def __init__(self, row=None, scalar=0): self.row, self.scalar = row, scalar
    def mappings(self): return self
    def one_or_none(self): return deepcopy(self.row)
    def scalar_one(self): return self.scalar


class Database:
    def __init__(self): self.rows, self.usage, self.sql, self.spent = {}, [], [], 0
    def execute(self, query, params=None):
        sql, p = str(query), params or {}
        self.sql.append(sql)
        if sql.startswith('SELECT * FROM ai_settings'): return Result(self.rows.get(p['w']))
        if sql.startswith('INSERT INTO ai_settings'):
            self.rows.setdefault(p['w'], {'gateway_config': {}, 'monthly_limit_rub': None, 'folder_id': 'synthetic-folder'})
        elif sql.startswith('UPDATE ai_settings SET gateway_config=jsonb_set'):
            self.rows[p['w']]['gateway_config']['telephony_agent'] = json.loads(p['value'])
        elif sql.startswith('INSERT INTO auth_events'): pass
        elif sql.startswith('INSERT INTO ai_usage'): self.usage.append(p)
        elif 'sum(u.cost_rub)' in sql: return Result(scalar=self.spent)
        else: raise AssertionError('Unexpected SQL (generic agents and MAX are forbidden): ' + sql[:100])
        return Result()


class Accounts:
    def __init__(self, db, workspace): self.db, self.workspace, self.role, self.depth = db, workspace, 'owner', 0
    @contextmanager
    def transaction(self, actor, **_):
        before = deepcopy(self.db.rows)
        self.depth += 1
        try: yield self.db
        except BaseException:
            self.db.rows = before
            raise
        finally: self.depth -= 1
    def _workspace(self, db, actor, workspace, **_):
        if workspace != self.workspace: raise AuthError(404, 'Not found')
        if self.role not in ('owner','admin'): raise AuthError(403, 'Forbidden')
    def _workspace_chat(self, db, actor, workspace): self._workspace(db, actor, workspace)
    def throttle(self, *args, **kwargs): pass


class TelephonyAgentTests(unittest.TestCase):
    def setUp(self):
        self.workspace, self.actor = uuid4(), SimpleNamespace(user_id=uuid4())
        self.db = Database(); self.accounts = Accounts(self.db, self.workspace)
        self.client = Mock(); self.client.complete.side_effect = self.answer
        self.vault = Mock(open=Mock(return_value={'api_key':'synthetic-key'}))
        self.service = TelephonyAgent(self.accounts,self.vault,yandex=self.client,deepseek=Mock(),mimo=Mock())
    def answer(self, *args, **kwargs):
        self.assertEqual(self.accounts.depth, 0, 'network call inside DB transaction')
        return {'text':'Обсудили перенос занятия. Новое время ещё не согласовано.',
                'input_tokens':25,'output_tokens':15,'truncated':False}
    def save(self, version=0, **changes):
        value = {k:v for k,v in defaults(self.workspace).items() if k in ('name','model','recognition_provider','prompt','temperature')}
        return self.service.save(self.actor,self.workspace,value | {'expected_version':version} | changes)
    def ready(self):
        self.save()
        self.db.rows[self.workspace] |= {'key_ciphertext':'synthetic-cipher','deepseek_key_ciphertext':None}
    def test_opening_configuration_does_not_create_an_agent_or_start_paid_work(self):
        value=self.service.settings(self.actor,self.workspace)
        self.assertEqual(value['version'],0); self.assertTrue(value['processing_available'])
        self.assertEqual(self.db.rows,{}); self.client.complete.assert_not_called()
    def test_separate_identity_and_task_never_use_generic_agents(self):
        value=self.save(); self.assertEqual(value['task'],'telephone_conversations')
        self.assertNotEqual(value['id'],defaults(uuid4())['id'])
        self.assertFalse(any('FROM ai_agents' in sql for sql in self.db.sql))
    def test_saving_preserves_other_ai_routes_and_keeps_immutable_versions(self):
        self.db.rows[self.workspace]={'gateway_config':{'recognition_provider':'mimo','yandex':{'checks':{'text':{'ok':True}}}},'monthly_limit_rub':None}
        self.save(); value=self.save(version=1,prompt='Новая инструкция для телефонных разговоров.')
        config=self.db.rows[self.workspace]['gateway_config']
        self.assertEqual(config['recognition_provider'],'mimo'); self.assertTrue(config['yandex']['checks']['text']['ok'])
        self.assertEqual(value['version'],2); self.assertEqual(value['history'][0]['prompt'],DEFAULT_PROMPT)
        self.client.complete.assert_not_called()
    def test_stale_save_cannot_overwrite_telephone_agent(self):
        self.save(); before=deepcopy(self.db.rows)
        with self.assertRaises(AuthError) as error: self.save(prompt='stale')
        self.assertEqual(error.exception.status,409); self.assertEqual(self.db.rows,before)
    def test_owner_only_and_cross_workspace_access_fail_before_work(self):
        for role in ('operator','observer'):
            self.accounts.role=role
            with self.assertRaises(AuthError): self.service.settings(self.actor,self.workspace)
            with self.assertRaises(AuthError): self.save()
        self.accounts.role='owner'
        with self.assertRaises(AuthError): self.service.settings(self.actor,uuid4())
        self.client.complete.assert_not_called()
    def test_audio_only_recognizer_and_known_model_are_validated(self):
        for values in ({'recognition_provider':'deepseek'},{'model':'invented-model'},{'prompt':'   '}):
            with self.assertRaises(AuthError): self.save(**values)
        self.assertEqual(self.db.rows,{})
    def test_unsaved_or_changed_version_cannot_trigger_a_paid_test(self):
        for version in (1,2):
            with self.assertRaises(AuthError): self.service.test(self.actor,self.workspace,{'expected_version':version,'text':'sample'})
        self.client.complete.assert_not_called()
    def test_description_uses_only_the_dedicated_instruction_and_selected_model(self):
        self.ready(); result=self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'Предложили вторник, клиент пока не согласовал.'})
        params=self.client.complete.call_args.kwargs
        self.assertIn(DEFAULT_PROMPT,params['prompt']); self.assertIn(FIXED_RULES,params['prompt'])
        self.assertFalse(params['rules']); self.assertFalse(result['saved_to_call'])
        self.assertEqual(self.db.usage[0]['a'],defaults(self.workspace)['id'])
        self.vault.open.assert_called_once_with(self.workspace,SECRET_ID,'synthetic-cipher',purpose=PURPOSE)

    def test_grok_is_available_to_telephone_agent_with_separate_recognizer(self):
        from center_messenger import ai_grok
        self.save(model='grok-4.6')
        row=self.db.rows[self.workspace]
        row.update(key_ciphertext=None,deepseek_key_ciphertext=None)
        row['gateway_config']['grok']={'key_ciphertext':'grok-sealed'}
        self.vault.open.return_value={'api_key':'grok-synthetic-key','provider':'grok'}
        self.service.grok=Mock();self.service.grok.complete.side_effect=self.answer
        public=self.service.settings(self.actor,self.workspace)
        self.assertIn('grok-4.6',[m['id'] for m in public['models']])
        self.assertNotIn('grok',[m['id'] for m in public['recognizers']])
        result=self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'Перенос занятия.'})
        options=self.service.grok.complete.call_args.kwargs
        self.assertEqual(options['model'],'grok-4.6');self.assertEqual(options['api_key'],'grok-synthetic-key')
        self.assertIn(FIXED_RULES,options['prompt']);self.assertFalse(options['rules'])
        self.assertGreater(result['cost_rub'],0);self.client.complete.assert_not_called()
        self.service.deepseek.complete.assert_not_called();self.service.mimo.complete.assert_not_called()
        self.vault.open.assert_called_once_with(self.workspace,ai_grok.SECRET_ID,'grok-sealed',purpose=ai_grok.PURPOSE)
    def test_budget_and_missing_key_prevent_provider_requests(self):
        self.ready(); row=self.db.rows[self.workspace]; row['monthly_limit_rub']=1; self.db.spent=1
        with self.assertRaises(AuthError) as error: self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'sample'})
        self.assertEqual(error.exception.status,429); self.client.complete.assert_not_called()
        self.db.spent=0; self.db.rows[self.workspace]['key_ciphertext']=None
        with self.assertRaises(AuthError): self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'sample'})
        self.client.complete.assert_not_called()

    def test_telephone_grok_test_passes_the_saved_proxy_separately_from_asr(self):
        from center_messenger import ai_grok
        self.save(model='grok-4.7');row=self.db.rows[self.workspace]
        row.update(key_ciphertext=None,deepseek_key_ciphertext=None)
        row['gateway_config']['grok']={'key_ciphertext':'key-cipher','proxy_ciphertext':'proxy-cipher',
            'proxy_enabled':True,'proxy_protocol':'socks5'}
        def opened(workspace,identity,cipher,**kwargs):
            return ({'api_key':'grok-key','provider':'grok'} if identity==ai_grok.SECRET_ID else
                {'provider':'grok_proxy','api_key':'8.8.4.4:8080:u:synthetic-password','protocol':'socks5'})
        self.vault.open.side_effect=opened
        self.service.grok=Mock();self.service.grok.complete.side_effect=self.answer
        self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'Перенос занятия.'})
        self.assertEqual(self.service.grok.complete.call_args.kwargs['proxy'].protocol,'socks5')
        self.client.complete.assert_not_called()
    def test_provider_failure_does_not_fallback_retry_or_return_raw_sample(self):
        self.ready(); self.client.complete.side_effect=YandexError('network')
        with self.assertRaises(AuthError) as error: self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'private conversation sample'})
        self.assertNotIn('private conversation sample',error.exception.detail)
        self.assertEqual(self.client.complete.call_count,1)
        self.service.deepseek.complete.assert_not_called(); self.service.mimo.complete.assert_not_called()
        self.assertEqual(self.db.usage[0]['o'],'error')
    def test_late_result_is_charged_but_not_applied_to_a_changed_profile(self):
        self.ready()
        def late(*args,**kwargs):
            self.db.rows[self.workspace]['gateway_config']['telephony_agent']['version']=2
            return self.answer(*args,**kwargs)
        self.client.complete.side_effect=late
        with self.assertRaises(AuthError) as error: self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'sample'})
        self.assertEqual(error.exception.status,409); self.assertEqual(len(self.db.usage),1)
    def test_partial_answer_is_billed_but_never_presented_as_a_complete_description(self):
        self.ready(); self.client.complete.side_effect=None
        self.client.complete.return_value=self.answer() | {'truncated':True}
        with self.assertRaises(AuthError) as error: self.service.test(self.actor,self.workspace,{'expected_version':1,'text':'sample'})
        self.assertEqual(error.exception.status,502); self.assertEqual(self.db.usage[0]['e'],'unusable')
    def test_http_body_rejects_bare_api_keys_and_auto_activation(self):
        from pydantic import ValidationError
        data={k:v for k,v in defaults(self.workspace).items() if k in ('name','model','recognition_provider','prompt','temperature')}
        for extra in ({'api_key':'synthetic-key'},{'enabled':True}):
            with self.assertRaises(ValidationError): PhoneAgentInput.model_validate(data | {'expected_version':0} | extra)

    def request(self,method,path,data=None,csrf=False,query=''):
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from unittest.mock import patch
        from center_messenger import telephony_api
        app=FastAPI();app.include_router(telephony_api.router)
        app.state.telephony=self.service
        self.actor.raw_session='synthetic-session'
        app.state.accounts=SimpleNamespace(settings=SimpleNamespace(cookie_name='session'),authenticate=lambda _:self.actor)
        @app.exception_handler(AuthError)
        async def denied(request,error):return JSONResponse({'detail':error.detail},status_code=error.status)
        async def run():
            body=json.dumps(data or {}).encode();headers=[(b'content-type',b'application/json'),(b'cookie',b'session=synthetic-session')]
            if csrf:headers.append((b'x-csrf-token',csrf_token(self.actor.raw_session).encode()))
            scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
                   'method':method,'scheme':'https','path':path,'raw_path':path.encode(),'query_string':query.encode(),
                   'headers':headers,'client':('test',1),'server':('test',443)}
            messages=[]
            async def receive():return {'type':'http.request','body':body,'more_body':False}
            async def send(message):messages.append(message)
            await asyncio.wait_for(app(scope,receive,send),5)
            return next(m['status'] for m in messages if m['type']=='http.response.start'),json.loads(b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body'))
        with patch.object(telephony_api,'agent_service',return_value=self.service):return asyncio.run(run())

    def test_actual_http_save_requires_csrf_and_uses_the_independent_resource(self):
        path=f'/api/workspaces/{self.workspace}/integrations/novofon/agent'
        data={k:v for k,v in defaults(self.workspace).items() if k in ('name','model','recognition_provider','prompt','temperature')}
        status,_=self.request('PUT',path,data | {'expected_version':0})
        self.assertEqual(status,403);self.assertEqual(self.db.rows,{})
        status,value=self.request('PUT',path,data | {'expected_version':0},csrf=True)
        self.assertEqual(status,200);self.assertEqual(value['task'],'telephone_conversations')
        self.assertEqual(value['version'],1);self.client.complete.assert_not_called()

    def test_actual_http_test_checks_csrf_and_foreign_tenant_before_model_call(self):
        self.ready();path=f'/api/workspaces/{self.workspace}/integrations/novofon/agent/test'
        data={'expected_version':1,'text':'Пример разговора'}
        status,_=self.request('POST',path,data);self.assertEqual(status,403)
        status,_=self.request('POST',path.replace(str(self.workspace),str(uuid4())),data,csrf=True)
        self.assertEqual(status,404);self.client.complete.assert_not_called()
        status,value=self.request('POST',path,data,csrf=True)
        self.assertEqual(status,200);self.assertFalse(value['saved_to_call'])

    def test_night_archive_control_is_a_csrf_protected_explicit_action(self):
        from unittest.mock import patch
        controller=Mock();controller.configure_archive.return_value={'archive':{'enabled':True}}
        path=f'/api/workspaces/{self.workspace}/integrations/novofon/archive'
        with patch('center_messenger.telephony_api.Processing',return_value=controller):
            status,_=self.request('PUT',path,{'expected_version':0,'enabled':True})
            self.assertEqual(status,403);controller.configure_archive.assert_not_called()
            status,_=self.request('PUT',path,{'expected_version':0,'enabled':True},csrf=True)
            self.assertEqual(status,200)
        self.assertEqual(controller.configure_archive.call_args.args[2],{'expected_version':0,'enabled':True})
        self.client.recognize.assert_not_called();self.client.complete.assert_not_called()

    def test_call_ai_request_retains_a_stable_request_id_and_transcript_cursor_is_bounded(self):
        from unittest.mock import patch
        controller=Mock();controller.request.return_value={'run_id':str(uuid4()),'phase':'queued'}
        call,subject,request=uuid4(),uuid4(),uuid4()
        path=f'/api/workspaces/{self.workspace}/chats/{subject}/calls/{call}'
        with patch('center_messenger.telephony_api.Processing',return_value=controller):
            status,_=self.request('POST',path+'/ai',{'request_id':str(request)},csrf=True)
            self.assertEqual(status,200);self.assertEqual(controller.request.call_args.args[4]['request_id'],request)
            status,_=self.request('GET',path+'/transcript',query='after=..%2F..%2Fbad')
            self.assertEqual(status,422);controller.transcript.assert_not_called()
        self.client.recognize.assert_not_called()


if __name__=='__main__': unittest.main()
