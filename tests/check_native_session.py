"""Synthetic native Chromium login, hidden-password page and rotated cookie check."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from playwright.sync_api import sync_playwright
from center_messenger.alfa_max_browser import AlfaMaxBrowser
from center_messenger.alfa_session import SECRET


def main():
    executable = os.environ['CM_TEST_CHROMIUM_EXECUTABLE']
    seen = []
    launches = []
    mode = 'history'
    history = ('<div hidden><form action="/auth/login"><input name="email"><input type="password"></form></div>'
               '<table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td>Sender</td><td>Synthetic</td>'
               '<td><time datetime="2026-10-26T12:00:00Z"></time></td></tr></tbody></table>')
    def route(request_route):
        request = request_route.request
        parts = urlsplit(request.url)
        assert parts.hostname == 'test.s20.online'
        seen.append({'method': request.method, 'path': parts.path})
        if parts.path == '/' and request.method == 'GET':
            request_route.fulfill(status=200, content_type='text/html', body=(
                '<form method="post" action="/auth/login"><input type="email" name="LoginForm[email]">'
                '<input type="password" name="LoginForm[password]"><button type="submit">Войти</button></form>'))
        elif parts.path == '/auth/login' and request.method == 'POST':
            request_route.fulfill(status=200, headers={'Content-Type': 'text/html',
                'Set-Cookie': 'native_session=rotated; Path=/; Secure; HttpOnly; SameSite=Lax'}, body=history)
        elif parts.path == '/company/1/max/receive':
            if mode == 'otp':
                request_route.fulfill(status=200, headers={'Content-Type': 'text/html', 'Set-Cookie': 'native_session=pending_otp; Path=/; Secure; HttpOnly'}, body='<form action="/auth/login"><input name="LoginForm[code]" autocomplete="one-time-code"></form>')
            else:
                request_route.fulfill(status=200, content_type='text/html', body=history)
        elif parts.path == '/settings/1/sms-template/index':
            request_route.fulfill(status=200, headers={'Content-Type': 'text/html', 'Set-Cookie': 'native_session=template_rotated; Path=/; Secure; HttpOnly'}, body='<div class="sortable1-item" data-id="1"><div class="sms-title"><small>Synthetic template</small></div><div class="sms-text"><small>Hello</small></div></div>')
        else:
            request_route.abort()

    class Manager:
        def __init__(self):
            self.native = sync_playwright().start()
            self.chromium = SimpleNamespace(launch=self.launch)
        def launch(self, **kwargs):
            launches.append(True)
            kwargs['executable_path'] = executable
            kwargs['args'] += json.loads(os.environ.get('CM_TEST_CHROMIUM_ARGS', '[]'))
            return self.native.chromium.launch(**kwargs)
        def stop(self):
            self.native.stop()

    with patch.object(AlfaMaxBrowser, '_playwright', return_value=lambda: SimpleNamespace(start=Manager)), \
         patch.object(AlfaMaxBrowser, '_route_factory', return_value=route), \
         patch('center_messenger.alfa_max_browser._public_dns'):
        browser = AlfaMaxBrowser()
        status, state = browser.login('https://test.s20.online', 'synthetic@example.test', 'synthetic-only')
        assert status == 'active'
        assert any(cookie['name'] == 'native_session' and cookie['value'] == 'rotated' for cookie in state['cookies'])
        state[SECRET] = {'enabled': True, 'password': 'synthetic-only'}
        renewed = browser.check_session('https://test.s20.online', state, 1)
        assert SECRET not in renewed, 'Server-only login data must never enter browser storage'
        assert any(cookie['name'] == 'native_session' for cookie in renewed['cookies'])
        before = len(launches)
        def templates(request, result, current):
            values = browser.read_sms_templates('https://test.s20.online', current, 1)
            assert values[0]['name'] == 'Synthetic template'
            assert any(c['value'] == 'template_rotated' for c in current['cookies'])
        _, renewed = browser.read_histories('https://test.s20.online', renewed, [(1, 'receive', 0)], on_result=templates)
        assert len(launches) == before + 1, 'Templates must share the active browser context'
        assert any(c['value'] == 'template_rotated' for c in renewed['cookies'])
        mode = 'otp'
        try:
            browser.check_session('https://test.s20.online', renewed, 1)
        except Exception as error:
            assert error.code == 'otp_required'
            assert any(c['value'] == 'pending_otp' for c in error.browser_state['cookies'])
            assert SECRET not in error.browser_state
        else:
            raise AssertionError('Pending OTP was not detected')
    assert sum(item['method'] == 'POST' for item in seen) == 1
    print(json.dumps({'native_browser_check': 'passed', 'login': status,
                      'hidden_password_did_not_expire_session': True,
                      'cookie_preserved_between_contexts': True,
                      'private_envelope_excluded_from_browser_storage': True,
                      'templates_share_context_and_keep_rotated_cookies': True,
                      'pending_otp_preserves_browser_state': True}))


if __name__ == '__main__':
    main()
