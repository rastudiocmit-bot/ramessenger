"""Synthetic proxy transport contracts; no live proxy or API credentials."""
import io
import socket
import ssl
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

from center_messenger import grok_proxy_probe as p, grok_access as g
from center_messenger.auth_security import AuthError
import test_grok_access as access_fixture
from test_ai_voice import ACTOR, WS

SECRET = 'synthetic-proxy-password-only'
KEY = 'xai-synthetic-provider-key-only'
ADDRESS = '8.8.4.4:8080:synthetic-user:' + SECRET


class Socket:
    def __init__(self, incoming):
        self.incoming = io.BytesIO(incoming); self.sent = []; self.closed = False
    def recv(self, size): return self.incoming.read(size)
    def sendall(self, value): self.sent.append(value)
    def settimeout(self, value): pass
    def close(self): self.closed = True


def request(path='/language-models'):
    return urllib.request.Request('https://api.x.ai/v1' + path, headers={'Authorization': 'Bearer ' + KEY})


class ProxyTests(unittest.TestCase):
    def mocks(self, sock):
        context = MagicMock(); context.wrap_socket.side_effect = lambda value, **kwargs: value
        connection = MagicMock(); connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"models":[]}'
        return (patch.object(p.socket, 'create_connection', return_value=sock),
                patch.object(p.ssl, 'create_default_context', return_value=context),
                patch.object(p.http.client, 'HTTPSConnection', return_value=connection), context, connection)

    def test_public_ipv4_only_and_credentials_are_not_in_reprs_or_errors(self):
        config = p.parse(ADDRESS, 'socks5')
        self.assertNotIn(SECRET, repr(config)); self.assertNotIn('synthetic-user', repr(config))
        for value in ('127.0.0.1:8080:u:p', '10.0.0.1:8080:u:p', '169.254.169.254:80:u:p',
                '224.0.0.1:8080:u:p', 'example.test:8080:u:p', '8.8.4.4:0:u:p', '8.8.4.4:65536:u:p',
                ADDRESS + '\r\nsecret', '8.8.4.4:8080::p', SECRET, 'x'*1025):
            with self.assertRaises(AuthError) as error: p.parse(value)
            self.assertEqual(error.exception.status, 422)
            self.assertNotIn(SECRET, error.exception.detail)

    def test_http_connect_sends_proxy_auth_only_to_proxy_and_bearer_only_after_tls(self):
        sock = Socket(b'HTTP/1.1 200 Connection established\r\n\r\n')
        dial, tls, factory, context, connection = self.mocks(sock)
        opener = p.ProxyOpener(p.parse(ADDRESS, 'http'))
        with dial as dialing, tls as certificate_context, factory:
            with opener.open(request()) as response: self.assertEqual(response.read(100), b'{"models":[]}')
        dialing.assert_called_once_with(('8.8.4.4', 8080), timeout=7)
        certificate_context.assert_called_once_with()
        context.wrap_socket.assert_called_once_with(sock, server_hostname='api.x.ai')
        self.assertIn(b'CONNECT api.x.ai:443 HTTP/1.1', sock.sent[0])
        self.assertIn(b'Proxy-Authorization: Basic ', sock.sent[0]); self.assertNotIn(KEY.encode(), sock.sent[0])
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('GET', '/v1/language-models'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer '+KEY)
        self.assertNotIn('Proxy-Authorization', kwargs['headers'])
        connection.close.assert_called_once(); self.assertTrue(opener.public()['tls_verified'])
        self.assertNotIn(SECRET, str(opener.public()))

    def test_socks5_auth_and_fixed_remote_dns_target(self):
        sock = Socket(b'\x05\x02\x01\x00\x05\x00\x00\x01' + b'\x00'*6)
        dial, tls, factory, context, connection = self.mocks(sock)
        opener = p.ProxyOpener(p.parse(ADDRESS, 'socks5'))
        with dial, tls, factory:
            with opener.open(request()): pass
        self.assertEqual(sock.sent[0], b'\x05\x02\x00\x02')
        self.assertIn(SECRET.encode(), sock.sent[1]); self.assertNotIn(KEY.encode(), b''.join(sock.sent))
        self.assertEqual(sock.sent[2], b'\x05\x01\x00\x03\x08api.x.ai\x01\xbb')
        self.assertEqual(opener.public()['protocol'], 'socks5')

    def test_auto_negotiates_only_before_provider_request(self):
        first = Socket(b'HTTP/1.1 400 Bad request\r\n\r\n')
        second = Socket(b'HTTP/1.1 200 OK\r\n\r\n')
        dial, tls, factory, context, connection = self.mocks(second)
        opener = p.ProxyOpener(p.parse(ADDRESS))
        with patch.object(p.socket,'create_connection',side_effect=[first,second]) as dialing, tls, factory:
            with opener.open(request()):pass
        self.assertEqual(dialing.call_count,2);self.assertTrue(first.closed)
        self.assertEqual(opener.protocol,'http');connection.request.assert_called_once()

    def test_auth_failure_and_certificate_failure_never_send_provider_key(self):
        for protocol,incoming in (('http',b'HTTP/1.1 407 Authentication required\r\n\r\n'),
                ('socks5',b'\x05\x02\x01\x01'),('auto',b'\x05\xff')):
            sock=Socket(incoming);dial,tls,factory,context,connection=self.mocks(sock)
            opener=p.ProxyOpener(p.parse(ADDRESS,protocol))
            with dial,tls,factory:
                with self.assertRaises(p.ProxyFailure) as error:opener.open(request())
            self.assertEqual(error.exception.code,'proxy_auth');connection.request.assert_not_called()
            self.assertNotIn(SECRET,str(error.exception));self.assertTrue(sock.closed)
        sock=Socket(b'HTTP/1.1 200 OK\r\n\r\n');dial,tls,factory,context,connection=self.mocks(sock)
        context.wrap_socket.side_effect=ssl.SSLCertVerificationError('private error '+SECRET)
        opener=p.ProxyOpener(p.parse(ADDRESS,'http'))
        with dial,tls,factory:
            with self.assertRaises(p.ProxyFailure) as error:opener.open(request())
        self.assertEqual(error.exception.code,'proxy_tls');connection.request.assert_not_called()
        self.assertNotIn(SECRET,str(error.exception));self.assertFalse(opener.public()['tls_verified'])

    def test_private_target_redirect_and_post_cannot_be_sent(self):
        opener=p.ProxyOpener(p.parse(ADDRESS))
        with patch.object(p.socket,'create_connection') as dial:
            for req in (urllib.request.Request('https://other.test/'),
                    urllib.request.Request('https://api.x.ai/v1/responses',data=b'{}'),
                    urllib.request.Request('https://api.x.ai/v1/api-key',data=b'{}')):
                with self.assertRaises(ValueError):opener.open(req)
            dial.assert_not_called()

    def test_explicit_production_opt_in_allows_only_fixed_responses_post_with_original_body(self):
        sock=Socket(b'HTTP/1.1 200 OK\r\n\r\n');dial,tls,factory,context,connection=self.mocks(sock)
        config=p.parse(ADDRESS,'http');opener=p.ProxyOpener(config,allow_inference=True)
        req=urllib.request.Request('https://api.x.ai/v1/responses',data=b'{"store":false}',headers={'Authorization':'Bearer '+KEY})
        with dial,tls,factory:
            with opener.open(req,timeout=25):pass
        self.assertEqual(connection.request.call_args.args,('POST','/v1/responses'))
        self.assertEqual(connection.request.call_args.kwargs['body'],b'{"store":false}')
        self.assertEqual(connection.request.call_args.kwargs['headers']['Content-Type'],'application/json')
        self.assertNotIn(KEY.encode(),b''.join(sock.sent))
        with self.assertRaises(ValueError):opener.open(urllib.request.Request('https://other.test/v1/responses',data=b'{}'))

    def test_http_refusal_is_returned_without_following_redirects_or_paid_calls(self):
        sock=Socket(b'HTTP/1.1 200 OK\r\n\r\n');dial,tls,factory,context,connection=self.mocks(sock)
        connection.getresponse.return_value.status=302
        connection.getresponse.return_value.read.return_value=b'{}'
        opener=p.ProxyOpener(p.parse(ADDRESS,'http'))
        with dial,tls,factory:result=g.probe(KEY,opener.open)
        self.assertEqual(result['http_status'],302);self.assertEqual(result['code'],'redirect_refused')
        connection.request.assert_called_once()

    def test_authorized_route_uses_saved_key_and_does_not_save_proxy_or_usage(self):
        fixture=access_fixture.GrokAccessTests();fixture.setUp();before=fixture.db.settings.copy()
        fake=p.ProxyOpener(p.parse(ADDRESS,'socks5'))
        fake.open=fixture.opener;fake.protocol='socks5';fake.tunnel=fake.tls=True
        with patch.object(p,'ProxyOpener',return_value=fake):
            result=g.diagnose(fixture.ai,ACTOR,WS,proxy_data={'proxy':ADDRESS,'protocol':'socks5'})
        self.assertTrue(result['ok']);self.assertEqual(result['route'],'proxy')
        self.assertNotIn(SECRET,str(result));self.assertEqual(fixture.db.settings,before)
        self.assertEqual(fixture.db.usage,[])
        fixture.ai.accounts.role='operator'
        with patch.object(p,'ProxyOpener') as factory:
            with self.assertRaises(AuthError):g.diagnose(fixture.ai,ACTOR,WS,proxy_data={'proxy':ADDRESS})
            factory.assert_not_called()

    def test_proxy_model_masks_credentials_and_route_keeps_csrf_identity(self):
        from types import SimpleNamespace
        from center_messenger.ai_api import GrokProxyCheck,grok_proxy_diagnostic
        model=GrokProxyCheck(proxy=ADDRESS,protocol='socks5')
        self.assertNotIn(SECRET,repr(model));self.assertNotIn(SECRET,model.model_dump_json())
        req=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai_voice='service')))
        with patch('center_messenger.ai_api.identity',return_value=ACTOR) as identity,patch.object(g,'diagnose',return_value={'ok':False}) as diagnose:
            grok_proxy_diagnostic(WS,model,req)
        identity.assert_called_once_with(req,mutation=True)
        self.assertEqual(diagnose.call_args.kwargs['proxy_data']['proxy'],ADDRESS)

    def test_persistent_settings_route_masks_secrets_and_requires_csrf_identity(self):
        from types import SimpleNamespace
        from center_messenger.ai_api import GrokProxySettings,save_grok_proxy
        model=GrokProxySettings(proxy=ADDRESS,protocol='socks5',enabled=True)
        self.assertNotIn(SECRET,repr(model));self.assertNotIn(SECRET,model.model_dump_json())
        service=MagicMock();req=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai_voice=service)))
        with patch('center_messenger.ai_api.identity',return_value=ACTOR) as identity:save_grok_proxy(WS,model,req)
        identity.assert_called_once_with(req,mutation=True)
        service.gateways.grok.save_proxy.assert_called_once_with(ACTOR,WS,
            {'proxy':ADDRESS,'protocol':'socks5','enabled':True,'clear_proxy':False})


if __name__=='__main__':unittest.main()
