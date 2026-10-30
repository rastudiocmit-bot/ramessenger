"""MAX process ownership and private control boundary; no live CRM/DB."""
import asyncio
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'app/backend/src')]
from center_messenger.config import Settings
from center_messenger.alfa_max_api import ConnectInput, OTPInput
from center_messenger.auth_security import AuthError
from center_messenger.alfa_max_browser import AlfaBrowserError
from center_messenger.loader.bootstrap import build_loader
from center_messenger.max_worker_control import BrowserDisabled, WorkerControlAuth, control_key, forward_control, NoRedirects


class RoleTests(unittest.TestCase):
    def services(self):
        return SimpleNamespace(accounts=Mock(), alfa=Mock(), alfa_sync=Mock(), alfa_max=Mock(), messaging=Mock())

    def test_api_has_no_native_reader_or_sender_but_keeps_crm_cards_and_bots(self):
        loader = build_loader(self.services(), Settings(runtime_role='api', crm_sync_worker=True, max_worker=True))
        self.assertFalse(any(k.startswith('alfa_max.') for k in loader.steps))
        self.assertIn('max_bot.outgoing', loader.steps)
        self.assertIn('crm_cards.card', loader.steps)
        self.assertIn('alfa_directory.page', loader.steps)
        self.assertIn('chat_projection.batch', loader.steps)

    def test_worker_has_only_max_reader_sender_and_no_cards_projection_or_other_channels(self):
        services = self.services()
        loader = build_loader(services, Settings(runtime_role='max', crm_sync_worker=True, max_worker=True))
        self.assertEqual(set(loader.steps), {'alfa_max.history', 'alfa_max.outgoing'})
        self.assertFalse(loader.registration_errors)

    def test_api_rejects_any_accidental_browser_call(self):
        browser = BrowserDisabled()
        for method in ['login', 'verify_otp', 'check_session', 'send', 'read_histories', 'read_sms_templates']:
            with self.subTest(method=method), self.assertRaises(AlfaBrowserError) as error:
                getattr(browser, method)('synthetic')
            self.assertEqual(error.exception.code, 'browser_unavailable')

    def test_database_pools_remain_bounded_per_process(self):
        from center_messenger.database import make_engine
        config = Settings(runtime_role='max', db_pool_size=2, db_max_overflow=1, db_password='synthetic')
        with patch('center_messenger.database.create_engine') as create:
            make_engine(config)
        self.assertEqual((create.call_args.kwargs['pool_size'], create.call_args.kwargs['max_overflow']), (2, 1))

    def test_worker_control_outage_cannot_prevent_api_construction_or_enable_local_browser(self):
        from center_messenger.main import create_app
        app = create_app(Settings(runtime_role='api'), engine_factory=lambda _: None)
        self.assertIn('/health/ready', {getattr(r, 'path', None) for r in app.routes})
        with self.assertRaises(ValueError): create_app(Settings(runtime_role='max'))

    def test_readiness_rejects_missing_steps_or_a_dead_scheduler_thread(self):
        import threading
        from center_messenger.max_worker import loader_ready
        loader = SimpleNamespace(steps={'alfa_max.history': None, 'alfa_max.outgoing': None},
                                 registration_errors={}, stopping=threading.Event(), _threads=[Mock()])
        loader._threads[0].is_alive.return_value = True
        self.assertTrue(loader_ready(loader))
        loader._threads[0].is_alive.return_value = False
        self.assertFalse(loader_ready(loader))
        loader._threads[0].is_alive.return_value = True
        loader.registration_errors = {'alfa_max': 'failed'}
        self.assertFalse(loader_ready(loader))

    def test_api_lifespan_installs_the_disabled_browser_in_the_real_service_wiring(self):
        from center_messenger import main
        loader = Mock()
        loader.stop.return_value = True
        engine = Mock()
        config = Settings(runtime_role='api')
        with patch.object(main, 'build_loader', return_value=loader), \
             patch.object(main, 'Accounts'), patch.object(main, 'AlfaConnections'), \
             patch.object(main, 'AlfaMaxConnections') as native, \
             patch.object(main, 'AiVoice'), patch.object(main, 'Tasks'):
            app = main.create_app(config, engine_factory=lambda _: engine)
            async def check():
                async with app.router.lifespan_context(app):
                    self.assertIsInstance(native.call_args.kwargs['browser'], BrowserDisabled)
            asyncio.run(check())
        loader.stop.assert_called_once()
        engine.dispose.assert_called_once()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'worker-key'
        path.write_text('a' * 64)
        self.config = Settings(runtime_role='api', max_worker_key_file=path, public_url='https://messenger.example.test')
        self.accounts = Mock(settings=self.config)
        self.actor = SimpleNamespace(raw_session='b' * 43)
        self.accounts.authenticate.return_value = self.actor
        self.db = Mock()
        @contextmanager
        def transaction(*args, **kwargs): yield self.db
        self.accounts.transaction = transaction
        from center_messenger.auth_security import csrf_token
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accounts=self.accounts)),
                                       cookies={self.config.cookie_name: self.actor.raw_session},
                                       headers={'x-csrf-token': csrf_token(self.actor.raw_session)})
        self.workspace = uuid4()

    def response(self, payload=None):
        reply = Mock(status=200)
        reply.read.return_value = json.dumps(payload or {'state': 'active'}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=reply)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        return opener

    def test_login_is_forwarded_once_with_session_csrf_and_separate_service_key(self):
        opener = self.response()
        model = ConnectInput(email='synthetic@example.com', password='candidate-password')
        with patch('center_messenger.max_worker_control.build_opener', return_value=opener):
            self.assertEqual(forward_control(self.request, self.workspace, 'connect', model), {'state': 'active'})
        self.accounts._workspace.assert_called_once_with(self.db, self.actor, self.workspace, manage=True)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('http://max-worker:8001/api/workspaces/'))
        self.assertEqual(json.loads(request.data)['password'], 'candidate-password')
        self.assertEqual(request.get_header('X-cm-max-worker-key'), 'a' * 64)
        self.assertIn(self.actor.raw_session, request.get_header('Cookie'))

    def test_revoked_user_or_workspace_access_never_contacts_worker(self):
        for location in ['session', 'workspace']:
            self.accounts.authenticate.side_effect = AuthError(401, 'No access') if location == 'session' else None
            self.accounts._workspace.side_effect = AuthError(403, 'No access') if location == 'workspace' else None
            with patch('center_messenger.max_worker_control.build_opener') as opener:
                with self.assertRaises(AuthError):
                    forward_control(self.request, self.workspace, 'verify', OTPInput(code='123456'))
            opener.assert_not_called()

    def test_csrf_failure_never_forwards_password(self):
        self.request.headers['x-csrf-token'] = 'bad'
        with patch('center_messenger.max_worker_control.build_opener') as opener:
            with self.assertRaises(AuthError):
                forward_control(self.request, self.workspace, 'connect', ConnectInput(email='synthetic@example.com', password='secret'))
        opener.assert_not_called()

    def test_unknown_control_result_is_not_retried_and_does_not_leak_password(self):
        opener = self.response()
        opener.open.side_effect = URLError('candidate-password synthetic internal diagnostics')
        with patch('center_messenger.max_worker_control.build_opener', return_value=opener):
            with self.assertRaises(AuthError) as error:
                forward_control(self.request, self.workspace, 'connect', ConnectInput(email='synthetic@example.com', password='candidate-password'))
        self.assertEqual(error.exception.status, 503)
        self.assertNotIn('candidate-password', error.exception.detail)
        opener.open.assert_called_once()

    def test_otp_error_is_preserved_and_redirects_are_never_followed(self):
        opener = self.response()
        opener.open.side_effect = HTTPError('http://max-worker', 422, '', {}, io.BytesIO(b'{"detail":"Incorrect OTP"}'))
        with patch('center_messenger.max_worker_control.build_opener', return_value=opener):
            with self.assertRaises(AuthError) as error:
                forward_control(self.request, self.workspace, 'verify', OTPInput(code='123456'))
        self.assertEqual((error.exception.status, error.exception.detail), (422, 'Incorrect OTP'))
        self.assertIsNone(NoRedirects().redirect_request(None, None, 307, '', {}, 'https://elsewhere.example'))

    def test_invalid_or_missing_worker_key_never_falls_back_to_local_browser(self):
        self.config.max_worker_key_file.write_text('invalid')
        with self.assertRaises(ValueError): control_key(self.config)
        with patch('center_messenger.max_worker_control.build_opener') as opener:
            with self.assertRaises(AuthError):
                forward_control(self.request, self.workspace, 'verify', OTPInput(code='123456'))
        opener.assert_not_called()

    def test_internal_auth_rejects_missing_header_and_passes_lifespan(self):
        called = []
        async def app(scope, receive, send): called.append(scope['type'])
        auth = WorkerControlAuth(app, self.config)
        async def check():
            messages = []
            async def send(value): messages.append(value)
            async def receive(): return {'type': 'http.request', 'body': b''}
            await auth({'type': 'http', 'path': '/api/private', 'headers': []}, receive, send)
            self.assertEqual(messages[0]['status'], 403)
            self.assertEqual(called, [])
            await auth({'type': 'lifespan'}, receive, send)
            await auth({'type': 'http', 'path': '/health/ready', 'headers': []}, receive, send)
            await auth({'type': 'http', 'path': '/api/private', 'headers': [(b'x-cm-max-worker-key', b'a'*64)]}, receive, send)
        asyncio.run(check())
        self.assertEqual(called, ['lifespan', 'http', 'http'])

    def test_worker_http_keeps_session_csrf_and_has_no_chat_or_ai_routes(self):
        from center_messenger.max_worker import create_worker
        config = self.config.model_copy(update={'runtime_role': 'max'})
        app = create_worker(config)
        self.accounts.settings = config
        app.state.accounts = self.accounts
        app.state.alfa_max = Mock()
        app.state.alfa_max.connect.return_value = {'state': 'active'}
        path = f'/api/workspaces/{self.workspace}/integrations/alfa-max/connect'
        async def request(path, method='POST', key=True, csrf=True):
            payload = b'{"email":"synthetic@example.com","password":"synthetic-only"}'
            headers = [(b'content-type', b'application/json'), (b'content-length', str(len(payload)).encode()),
                       (b'origin', self.config.public_url.encode()),
                       (b'cookie', f'{config.cookie_name}={self.actor.raw_session}'.encode())]
            if key: headers.append((b'x-cm-max-worker-key', b'a'*64))
            if csrf: headers.append((b'x-csrf-token', self.request.headers['x-csrf-token'].encode()))
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
                     'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
                     'headers': headers, 'client': ('internal', 1), 'server': ('max-worker', 8001)}
            messages = []
            async def receive(): return {'type': 'http.request', 'body': payload, 'more_body': False}
            async def send(message): messages.append(message)
            await app(scope, receive, send)
            return next(m['status'] for m in messages if m['type'] == 'http.response.start')
        async def check():
            self.assertEqual(await request(path, key=False), 403)
            self.assertEqual(await request(path, csrf=False), 403)
            self.assertEqual(await request(path), 200)
            for forbidden in ['/api/system/info', '/api/auth/login', '/assets/chat.js', '/api/ai/agents']:
                self.assertEqual(await request(forbidden, method='GET'), 404)
        asyncio.run(check())
        app.state.alfa_max.connect.assert_called_once()


if __name__ == '__main__': unittest.main()
