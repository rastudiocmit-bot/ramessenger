"""Read-only server-side Grok report: permissions, staleness and safe output."""
from contextlib import contextmanager
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_ai_grok as fixture
KEY=fixture.KEY
from test_ai_voice import ACTOR, WS
from center_messenger import grok_access as g
from center_messenger.auth_security import AuthError


class Response(io.BytesIO):
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class GrokAccessTests(unittest.TestCase):
    def setUp(self):
        self.setup=fixture.GatewayTests();self.setup.setUp();self.setup.save(api_key=KEY)
        self.ai,self.db=self.setup.ai,self.setup.db
        self.depth=0;old=self.ai.accounts.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            self.depth+=1
            try:
                with old(*args,**kwargs) as db:yield db
            finally:self.depth-=1
        self.ai.accounts.transaction=transaction
    def opener(self,request,timeout):
        self.assertEqual(self.depth,0,'provider read inside database transaction')
        self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.data)
        self.assertEqual(request.full_url,'https://api.x.ai/v1/language-models')
        self.assertEqual(request.get_header('Authorization'),'Bearer '+KEY);self.assertEqual(timeout,7)
        return Response(json.dumps({'models':[{'id':'grok-4.7','input_modalities':['text'],'output_modalities':['text']}]}).encode())
    def test_success_reads_catalogue_without_inference_usage_or_settings_writes(self):
        before=deepcopy(self.db.settings)
        result=g.diagnose(self.ai,ACTOR,WS,opener=self.opener)
        self.assertEqual(result['http_status'],200);self.assertTrue(result['ok'])
        self.assertFalse(result['inference_requested']);self.assertEqual(result['models'],['grok-4.7'])
        self.assertNotIn(KEY,str(result));self.assertEqual(before,self.db.settings)
        self.assertEqual(self.db.usage,[]);self.assertEqual(self.ai.grok.calls,[])
    def test_401_and_403_remain_distinct_and_raw_provider_text_is_never_returned(self):
        import urllib.error
        for status,code in ((401,'unauthorized'),(403,'forbidden')):
            calls=[]
            def opener(request,timeout):
                calls.append(request)
                raise urllib.error.HTTPError(request.full_url,status,'private',{},io.BytesIO(json.dumps({'error':KEY}).encode()))
            result=g.diagnose(self.ai,ACTOR,WS,opener=opener)
            self.assertEqual(result['http_status'],status);self.assertEqual(result['code'],code)
            self.assertNotIn(KEY,str(result));self.assertNotIn('private',str(result))
            self.assertEqual([call.full_url for call in calls],['https://api.x.ai/v1/language-models']
                + (['https://api.x.ai/v1/api-key'] if status==403 else []))
    def test_denial_classifies_only_bounded_json_signals(self):
        self.assertEqual(g.denial(403,b'{"error":"country is not supported"}'),'region_restricted')
        self.assertEqual(g.denial(403,b'{"error":"Your request was blocked as your origin is not allowed"}'),'origin_restricted')
        self.assertEqual(g.denial(403,b'{"error":"team does not have any credits"}'),'no_balance')
        self.assertEqual(g.denial(403,b'<html>private denial</html>'),'forbidden')
        self.assertEqual(g.denial(429,b'{}'),'rate_limited')
        self.assertEqual(g.denial(302,b'{}'),'redirect_refused')
        self.assertIsNone(g.NoRedirect().redirect_request(None,None,None,None,None,None))
    def test_second_read_projects_key_state_without_exposing_account_metadata(self):
        import urllib.error
        base={'team_blocked':False,'api_key_blocked':False,'api_key_disabled':False,
              'acls':['api-key:endpoint:*','api-key:model:*'],'name':KEY,'team_id':'private-team',
              'user_id':'private-user','redacted_api_key':KEY,'arbitrary_secret':KEY}
        for changed,code in (({},'recognized'),({'team_blocked':True},'team_blocked'),
                ({'api_key_blocked':True},'key_blocked'),({'api_key_disabled':True},'key_disabled'),
                ({'acls':[]},'no_permissions')):
            calls=[];before=deepcopy(self.db.settings)
            def opener(request,timeout):
                self.assertEqual(self.depth,0);self.assertEqual(timeout,7)
                self.assertEqual(request.get_header('Authorization'),'Bearer '+KEY)
                self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.data)
                calls.append(request.full_url)
                if len(calls)==1:
                    raise urllib.error.HTTPError(request.full_url,403,'private',{},io.BytesIO(b'{}'))
                self.assertEqual(request.full_url,'https://api.x.ai/v1/api-key')
                return Response(json.dumps({**base,**changed}).encode())
            result=g.diagnose(self.ai,ACTOR,WS,opener=opener)
            self.assertEqual(result['http_status'],403);self.assertFalse(result['ok'])
            self.assertEqual(result['account']['code'],code);self.assertEqual(result['account']['http_status'],200)
            for secret in (KEY,'private-team','private-user','api-key:endpoint:*'):
                self.assertNotIn(secret,str(result))
            self.assertEqual(self.db.settings,before);self.assertEqual(self.db.usage,[])
            self.assertEqual(self.ai.grok.calls,[]);self.assertEqual(len(calls),2)
    def test_second_read_failure_does_not_replace_initial_403(self):
        import urllib.error
        for raw in (b'bad',b'[]',b'{}',b'x'*2_000_001,
                b'{"team_blocked":"false","api_key_blocked":false,"api_key_disabled":false,"acls":[]}'):
            def opener(request,timeout):
                if request.full_url.endswith('/language-models'):
                    raise urllib.error.HTTPError(request.full_url,403,'private',{},io.BytesIO(b'{}'))
                return Response(raw)
            result=g.probe(KEY,opener)
            self.assertEqual(result['http_status'],403);self.assertEqual(result['account']['code'],'unavailable')
            self.assertFalse(result['ok'])
        def timeout(request,**kwargs):
            if request.full_url.endswith('/language-models'):
                raise urllib.error.HTTPError(request.full_url,403,'private',{},io.BytesIO(b'{}'))
            raise TimeoutError(KEY)
        result=g.probe(KEY,timeout)
        self.assertEqual(result['http_status'],403);self.assertIsNone(result['account']['http_status'])
        self.assertNotIn(KEY,str(result))
    def test_explicit_denial_does_not_request_account_or_retry(self):
        import urllib.error
        for status,body in ((401,b'{}'),(403,b'{"error":"unsupported country"}'),
                (403,b'{"error":"origin is not allowed"}'),(429,b'{}'),(302,b'{}')):
            calls=[]
            def opener(request,timeout):
                calls.append(request)
                raise urllib.error.HTTPError(request.full_url,status,'private',{},io.BytesIO(body))
            result=g.probe(KEY,opener)
            self.assertEqual(result['http_status'],status);self.assertNotIn('account',result)
            self.assertEqual(len(calls),1)
        with self.assertRaises(ValueError):
            g.read_access(KEY,'https://other.example.test/',lambda *_args,**_kw:self.fail('request'))
    def test_key_replacement_during_account_read_makes_the_entire_report_stale(self):
        import urllib.error
        def opener(request,timeout):
            if request.full_url.endswith('/language-models'):
                raise urllib.error.HTTPError(request.full_url,403,'private',{},io.BytesIO(b'{}'))
            self.setup.save(api_key=KEY+'replacement')
            return Response(b'{"team_blocked":false,"api_key_blocked":false,"api_key_disabled":false,"acls":[]}')
        result=g.diagnose(self.ai,ACTOR,WS,opener=opener)
        self.assertEqual(result['code'],'stale');self.assertNotIn('account',result)
    def test_revoked_role_and_missing_key_block_provider_read(self):
        for role in ('operator','observer'):
            self.ai.accounts.role=role
            with self.assertRaises(AuthError) as error:g.diagnose(self.ai,ACTOR,WS,opener=lambda *_args,**_kw:self.fail('request'))
            self.assertEqual(error.exception.status,403)
        self.ai.accounts.role='owner';self.setup.save(clear_key=True)
        with self.assertRaises(AuthError) as error:g.diagnose(self.ai,ACTOR,WS,opener=lambda *_args,**_kw:self.fail('request'))
        self.assertEqual(error.exception.status,409)
    def test_late_read_cannot_report_for_a_replaced_key(self):
        def opener(request,timeout):
            self.setup.save(api_key=KEY+'a')
            return self.opener(request,timeout)
        result=g.diagnose(self.ai,ACTOR,WS,opener=opener)
        self.assertEqual(result['code'],'stale');self.assertIsNone(result['http_status'])
        self.assertFalse(result['ok'])
    def test_permission_is_rechecked_after_provider_read(self):
        def opener(request,timeout):
            value=self.opener(request,timeout);self.ai.accounts.role='operator';return value
        with self.assertRaises(AuthError) as error:g.diagnose(self.ai,ACTOR,WS,opener=opener)
        self.assertEqual(error.exception.status,403)
    def test_bad_or_oversized_payload_does_not_turn_into_valid_access(self):
        for raw in (b'[]',b'{"models":{}}',b'bad',b'x'*2_000_001,b'{"error":"private","models":[]}'):
            result=g.probe(KEY,lambda *_args,**_kw:Response(raw))
            self.assertEqual(result['code'],'bad_response');self.assertNotIn('private',str(result))
        result=g.probe(KEY,lambda *_args,**_kw:Response(b'{"models":[]}'))
        self.assertEqual(result['code'],'model_unavailable');self.assertFalse(result['ok'])
    def test_timeout_reports_transport_failure_without_exposing_transport_details(self):
        def opener(*args,**kwargs):raise TimeoutError('secret transport value')
        result=g.probe(KEY,opener)
        self.assertEqual(result['code'],'network');self.assertIsNone(result['http_status'])
        self.assertNotIn('secret',str(result))
    def test_http_route_requires_mutation_identity_and_passes_authorized_actor(self):
        from center_messenger.ai_api import grok_access_diagnostic
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai_voice=self.ai)))
        with patch('center_messenger.ai_api.identity',return_value=ACTOR) as identity,patch.object(g,'probe',return_value=g.report('forbidden',403)):
            result=grok_access_diagnostic(WS,request)
        identity.assert_called_once_with(request,mutation=True)
        self.assertEqual(result['http_status'],403)


if __name__=='__main__':unittest.main()
