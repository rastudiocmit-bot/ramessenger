"""No live key or provider calls: bounded read-only diagnostic and redaction."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
import urllib.error

spec = importlib.util.spec_from_file_location('grok_diag', Path(__file__).parents[1]/'scripts/diagnose_grok_connection.py')
diag = importlib.util.module_from_spec(spec); spec.loader.exec_module(diag)


class Response(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class GrokDiagnosticTests(unittest.TestCase):
    def test_error_output_never_contains_body_or_reflected_credentials(self):
        secret = 'xai-SYNTHETIC-DO-NOT-PRINT'
        raw = json.dumps({'error': {'message': secret+' blocked in this country', 'code': secret}}).encode()
        result = diag.refusal(403, raw)
        self.assertTrue(result['reason_signals']['region']); self.assertTrue(result['reason_signals']['permission'])
        self.assertIsNone(result['provider_code']); self.assertNotIn(secret, json.dumps(result))
    def test_401_and_403_remain_distinct_even_with_empty_body(self):
        for status in (401,403): self.assertEqual(diag.refusal(status,b'{}')['http_status'],status)
    def test_non_json_denial_is_reported_without_printing_html(self):
        result = diag.refusal(403,b'<html>private denial detail</html>')
        self.assertFalse(result['json_response']); self.assertNotIn('private',str(result))
    def test_known_balance_code_and_signals_are_allowed(self):
        result = diag.refusal(403,b'{"error":{"code":"insufficient_credits","message":"No credits"}}')
        self.assertEqual(result['provider_code'],'insufficient_credits'); self.assertTrue(result['reason_signals']['balance'])
    def test_catalogue_uses_get_fixed_origin_and_supported_model_projection(self):
        calls=[]
        def opener(request, timeout):
            calls.append((request,timeout))
            return Response(json.dumps({'models':[{'id':'grok-4.7'},{'id':'unrelated-secret'}]}).encode())
        result=diag.probe('/language-models','synthetic',opener)
        self.assertEqual(result,{'http_status':200,'supported_models':['grok-4.7']})
        request,timeout=calls[0];self.assertEqual(timeout,10);self.assertEqual(request.get_method(),'GET')
        self.assertIsNone(request.data);self.assertEqual(request.full_url,'https://api.x.ai/v1/language-models')
        self.assertNotIn('unrelated-secret',str(result))
    def test_http_error_is_one_attempt_and_preserves_status(self):
        calls=[]
        def opener(request,timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url,403,'x',{},io.BytesIO(b'{}'))
        result=diag.probe('/models','synthetic',opener)
        self.assertEqual(result['http_status'],403);self.assertEqual(len(calls),1)
    def test_oversize_bad_catalogue_and_network_error_are_bounded(self):
        for raw in (b'x'*(diag.LIMIT+1),b'no',b'{}'):
            result=diag.probe('/models','synthetic',lambda *_args,**_kw:Response(raw))
            self.assertEqual(result['http_status'],200);self.assertNotIn(raw[:40].decode(),str(result))
        def failed(*args,**kwargs):raise TimeoutError('private transport detail')
        self.assertEqual(diag.probe('/models','synthetic',failed),{'transport_error':'TimeoutError'})
    def test_other_endpoints_and_redirects_are_forbidden(self):
        for path in ('/responses','https://elsewhere.test','/models/../responses'):
            with self.assertRaises(ValueError):diag.probe(path,'synthetic')
        self.assertIsNone(diag.NoRedirect().redirect_request(None,None,None,None,None,None))


if __name__ == '__main__': unittest.main()
