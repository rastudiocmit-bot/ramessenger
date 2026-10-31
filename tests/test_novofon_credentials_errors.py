"""Two private credentials and precise, redacted provider errors; no live IO."""
import json
import unittest
from unittest.mock import Mock

import test_novofon_connection as fixture
from test_novofon_diagnostic import FakeHTTP, TOKEN, URL
from center_messenger.auth_security import AuthError
from center_messenger.novofon_client import Client, AuditError
from center_messenger.novofon_service import public_report

SUPPORT = 'synthetic-support-secret:17'


class CredentialRegressionTests(unittest.TestCase):
    setUp = fixture.NovofonServiceTests.setUp

    def save(self, key=TOKEN, support=SUPPORT, version=0):
        return self.service.save(self.actor, self.workspace, {'api_key':key,
            'support_secret':support, 'expected_version':version, 'sample_hours':24})

    def payload(self):
        row=self.db.rows[self.workspace]
        return self.vault.open(self.workspace,row['connection_id'],row['key_ciphertext'],purpose='novofon')

    def test_both_values_are_encrypted_and_not_returned(self):
        result=self.save()
        self.assertTrue(result['configured'])
        self.assertTrue(result['support_secret_configured'])
        self.assertEqual(result['support_secret_usage'],'not_verified')
        self.assertEqual(self.payload(),{'api_key':TOKEN,'support_secret':SUPPORT})
        for value in (TOKEN,SUPPORT):
            self.assertNotIn(value,json.dumps(result,default=str))
            self.assertNotIn(value,str(self.db.events))
            self.assertNotIn(value,self.db.rows[self.workspace]['key_ciphertext'])

    def test_empty_fields_preserve_both_values(self):
        self.save();self.save(key=None,support=None,version=1)
        self.assertEqual(self.payload(),{'api_key':TOKEN,'support_secret':SUPPORT})

    def test_replacing_one_value_preserves_the_other(self):
        self.save();self.save(key='replacement-synthetic-api',support=None,version=1)
        self.assertEqual(self.payload()['support_secret'],SUPPORT)
        self.save(key=None,support='replacement-support:18',version=2)
        self.assertEqual(self.payload(),{'api_key':'replacement-synthetic-api','support_secret':'replacement-support:18'})

    def test_legacy_saved_key_can_receive_support_secret(self):
        result=self.save(support=None)
        self.assertFalse(result['support_secret_configured'])
        self.save(key=None,version=1)
        self.assertEqual(self.payload(),{'api_key':TOKEN,'support_secret':SUPPORT})

    def test_support_secret_alone_does_not_claim_an_api_connection(self):
        with self.assertRaises(AuthError):self.save(key=None)
        self.assertEqual(self.db.rows,{})

    def test_disconnect_removes_both_values(self):
        self.save();result=self.service.disconnect(self.actor,self.workspace,1)
        self.assertFalse(result['configured']);self.assertFalse(result['support_secret_configured'])
        self.assertIsNone(self.db.rows[self.workspace]['key_ciphertext'])

    def test_unconfirmed_support_secret_is_not_sent_to_an_arbitrary_protocol(self):
        self.save();transport=FakeHTTP()
        self.service.client_factory=lambda key:Client(key,transport)
        result=self.service.verify(self.actor,self.workspace,1)
        self.assertEqual(result['status'],'api_verified')
        self.assertNotIn(SUPPORT,str(transport.calls))
        self.assertTrue(all(call[1]['params']['access_token']==TOKEN for call in transport.calls))
        self.assertEqual(result['support_secret_usage'],'not_verified')

    def test_stale_admin_cannot_overwrite_support_secret(self):
        self.save()
        with self.assertRaises(AuthError) as error:self.save(support='stale-value',version=0)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.payload()['support_secret'],SUPPORT)

    def test_invalid_support_secret_is_rejected_without_exposing_it(self):
        for secret in ('bad secret','bad\nsecret','x'*4097):
            with self.subTest(length=len(secret)),self.assertRaises(AuthError) as error:self.save(support=secret)
            self.assertNotIn(secret,str(error.exception))

    def test_rate_limit_explains_budget_and_keeps_saved_credentials(self):
        self.save();self.accounts.throttle=Mock(side_effect=AuthError(429,'generic',retry_after=60))
        with self.assertRaises(AuthError) as error:self.service.verify(self.actor,self.workspace,1)
        self.assertEqual(error.exception.retry_after,60)
        self.assertIn('четырёх',error.exception.detail)
        self.assertEqual(self.payload()['support_secret'],SUPPORT)

    def test_a_saved_old_error_is_explained_without_another_crm_request(self):
        self.save();self.db.rows[self.workspace].update(status='error',report={
            'account':{'status':'error','code':'api_rejected','rpc_code':-32003}})
        self.service.client_factory=Mock()
        result=self.service.settings(self.actor,self.workspace)
        self.assertIn('IP',result['checks']['account']['message'])
        self.assertIn('прав',result['checks']['account']['message'])
        self.service.client_factory.assert_not_called()


class ProviderErrorRegressionTests(unittest.TestCase):
    @staticmethod
    def error(mnemonic, *, null_id=False, ip='83.222.25.109'):
        def response(payload):
            return 200,{},json.dumps({'jsonrpc':'2.0','id':None if null_id else payload['id'],
                'error':{'code':-32003,'message':TOKEN+SUPPORT+URL,
                'data':{'mnemonic':mnemonic,'value':TOKEN,'params':{'ip':ip,'secret':SUPPORT}}}}).encode()
        return FakeHTTP({'get.account':response})

    def report(self, mnemonic, **kwargs):
        from center_messenger.novofon_client import audit
        return public_report(audit(Client(TOKEN,self.error(mnemonic,**kwargs))))['account']

    def test_ip_and_permissions_are_distinct_and_raw_errors_are_not_exposed(self):
        denied=self.report('forbidden');ip=self.report('ip_not_whitelisted')
        self.assertEqual(denied['mnemonic'],'forbidden')
        self.assertEqual(ip['observed_ip'],'83.222.25.109')
        self.assertNotEqual(denied['message'],ip['message'])
        for value in (TOKEN,SUPPORT,URL):self.assertNotIn(value,str((denied,ip)))

    def test_null_rpc_id_error_keeps_the_actual_provider_reason(self):
        result=self.report('forbidden',null_id=True)
        self.assertEqual(result['code'],'api_rejected')
        self.assertEqual(result['mnemonic'],'forbidden')

    def test_unknown_mnemonic_cannot_echo_a_secret(self):
        result=self.report(SUPPORT)
        self.assertNotIn('mnemonic',result)
        self.assertNotIn(SUPPORT,str(result))

    def test_only_public_ip_literals_can_be_shown(self):
        for value in (SUPPORT,'127.0.0.1','https://example.test','10.0.0.1'):
            with self.subTest(value=value):self.assertNotIn('observed_ip',self.report('ip_not_whitelisted',ip=value))

    def test_non_null_mismatched_rpc_id_is_still_rejected(self):
        def response(payload):
            return 200,{},json.dumps({'jsonrpc':'2.0','id':'wrong','error':{'code':-32003,'data':{'mnemonic':'forbidden'}}}).encode()
        with self.assertRaisesRegex(AuditError,'api_response_mismatch'):
            Client(TOKEN,FakeHTTP({'get.account':response})).read('get.account')


class CredentialHttpRegressionTests(unittest.TestCase):
    setUp = fixture.NovofonHttpTests.setUp

    def test_support_secret_is_unwrapped_only_at_the_server_service_boundary(self):
        response=self.client.put(self.path,headers=self.headers,json={
            'expected_version':0,'api_key':TOKEN,'support_secret':SUPPORT})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.app.state.novofon.save.call_args.args[2]['support_secret'],SUPPORT)
        self.assertNotIn(SUPPORT,response.text)

    def test_invalid_support_input_is_never_echoed(self):
        response=self.client.put(self.path,headers=self.headers,json={
            'expected_version':0,'api_key':TOKEN,'support_secret':SUPPORT*170})
        self.assertEqual(response.status_code,422)
        self.assertNotIn(SUPPORT,response.text)


if __name__=='__main__':unittest.main()
