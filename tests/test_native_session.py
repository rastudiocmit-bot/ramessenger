"""Regression coverage for shared native CRM authorization and operator sends."""
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from sqlalchemy import text

import test_message_sync as fixture
from center_messenger.alfa_max_browser import AlfaBrowserError, parse_history_html
from center_messenger.alfa_session import SECRET, control, merge_state
from center_messenger.auth_security import AuthError, can_send_messages
from center_messenger.connector_vault import ConnectorVault
from center_messenger.messaging import Messaging
from center_messenger.chat_api import SendInput
from pydantic import SecretStr


class SessionPolicyTests(unittest.TestCase):
    def test_operator_and_manager_need_messages_observer_never_sends(self):
        for role in ['operator', 'branch_manager']:
            self.assertTrue(can_send_messages(role, ['messages']))
            self.assertFalse(can_send_messages(role, ['overview']))
        self.assertTrue(can_send_messages('owner', []))
        self.assertTrue(can_send_messages('admin', []))
        self.assertFalse(can_send_messages('observer', ['messages']))
        self.assertFalse(can_send_messages('unknown', ['messages']))

    def test_hidden_password_in_authenticated_history_is_not_logout(self):
        html = ('<div hidden><form action="/auth/login"><input name="email"><input type="password"></form></div>'
                '<table class="crm-table"><tbody><tr data-key="1"><td></td><td></td><td>Sender</td>'
                '<td>Message</td><td><time datetime="2026-10-26T12:00:00Z"></time></td></tr></tbody></table>')
        self.assertEqual(len(parse_history_html(html, origin='https://test.s20.online', branch=1, direction='receive')['messages']), 1)

    def test_real_login_form_is_logout_but_unrecognized_html_is_not(self):
        html = '<form action="/auth/login"><input name="LoginForm[email]"><input type="password" name="LoginForm[password]"></form>'
        with self.assertRaises(AlfaBrowserError) as result:
            parse_history_html(html, origin='https://test.s20.online', branch=1, direction='receive')
        self.assertEqual(result.exception.code, 'reauth_required')
        with self.assertRaises(AlfaBrowserError) as result:
            parse_history_html('<input type="password">', origin='https://test.s20.online', branch=1, direction='receive')
        self.assertEqual(result.exception.code, 'invalid_response')

    def test_browser_state_merge_preserves_private_credentials(self):
        old = {'cookies': [], SECRET: {'enabled': True, 'password': 'synthetic-only'}}
        result = merge_state(old, {'cookies': [{'name': 'fresh'}], 'origins': []})
        self.assertEqual(result[SECRET], old[SECRET])
        self.assertEqual(result['cookies'], [{'name': 'fresh'}])


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'), 'isolated PostgreSQL URL required')
class NativeSessionTests(unittest.TestCase):
    setUpClass = classmethod(fixture.MessageSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.MessageSyncTests.tearDownClass.__func__)
    tearDown = fixture.MessageSyncTests.tearDown
    add_customer = fixture.MessageSyncTests.add_customer
    project = fixture.MessageSyncTests.project
    row = fixture.MessageSyncTests.row
    conversation = fixture.MessageSyncTests.conversation

    def setUp(self):
        fixture.MessageSyncTests.setUp(self)
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        key = Path(root.name) / 'key'
        key.write_text('k' * 64)
        self.service.vault = ConnectorVault(key)
        self.native = {'cookies': [], 'origins': [], SECRET: {'email': 'synthetic@example.test', 'password': 'synthetic-only', 'enabled': True}}
        self.db.execute(text('UPDATE alfa_max_settings SET session_ciphertext=:s WHERE workspace_id=:w'),
                        {'w': self.workspace, 's': self.service.vault.seal_browser_state(self.workspace, self.channel, self.native)})
        self.service.browser.check_session.return_value = {'cookies': [], 'origins': []}
        self.service.browser.login.return_value = ('active', {'cookies': [], 'origins': []})

    def setting(self):
        return dict(self.service._row(self.db, self.workspace))

    def pending(self):
        old = self.setting()
        self.assertTrue(self.service.sessions.suspect(self.db, self.workspace, old, 'test'))
        return old

    def test_only_one_browser_operation_and_stolen_lease_cannot_save(self):
        first = self.service.sessions.claim(self.workspace, self.channel)
        with self.assertRaises(AlfaBrowserError) as error:
            self.service.sessions.claim(self.workspace, self.channel)
        self.assertEqual(error.exception.code, 'browser_busy')
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,"
                             "'{session_control,until}',to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE workspace_id=:w"), {'w': self.workspace})
        second = self.service.sessions.claim(self.workspace, self.channel)
        with self.assertRaises(AlfaBrowserError):
            self.service.sessions.finish(first, self.native, state='active')
        self.service.sessions.release(first)
        self.assertEqual(control(self.setting())['lease'], second['token'])
        self.service.sessions.release(second)

    def test_stale_error_cannot_invalidate_new_login_or_new_cookies(self):
        old = self.setting()
        self.db.execute(text('UPDATE alfa_max_settings SET version=version+1 WHERE workspace_id=:w'), {'w': self.workspace})
        self.assertFalse(self.service.sessions.suspect(self.db, self.workspace, old, 'old_send'))
        old = self.setting()
        self.db.execute(text('UPDATE alfa_max_settings SET session_ciphertext=:s WHERE workspace_id=:w'),
                        {'w': self.workspace, 's': self.service.vault.seal_browser_state(self.workspace, self.channel, self.native)})
        self.assertFalse(self.service.sessions.suspect(self.db, self.workspace, old, 'old_read'))
        self.assertEqual(self.setting()['state'], 'active')

    def test_second_check_recovers_false_alarm_without_login(self):
        self.pending()
        self.assertEqual(self.service.sessions.recover(self.workspace, self.channel), 0)
        self.service.browser.login.assert_not_called()
        row = self.setting()
        self.assertEqual(row['state'], 'active')
        self.assertFalse(control(row)['needs_check'])
        stored = self.service.vault.open_browser_state(self.workspace, self.channel, row['session_ciphertext'])
        self.assertEqual(stored[SECRET]['password'], 'synthetic-only')

    def test_confirmed_logout_automatically_logs_in_and_checks_result(self):
        self.pending()
        self.service.browser.check_session.side_effect = [AlfaBrowserError('reauth_required'), {'cookies': [], 'origins': []}]
        self.assertEqual(self.service.sessions.recover(self.workspace, self.channel), 0)
        self.service.browser.login.assert_called_once()
        self.assertEqual(self.service.browser.check_session.call_count, 2)
        self.assertEqual(self.setting()['state'], 'active')
        public = self.service.public(self.setting())
        self.assertNotIn('synthetic-only', json.dumps(public, default=str))
        self.assertNotIn('session_ciphertext', public)

    def test_missing_saved_password_requests_one_time_setup(self):
        self.native.pop(SECRET)
        self.db.execute(text('UPDATE alfa_max_settings SET session_ciphertext=:s WHERE workspace_id=:w'),
                        {'w': self.workspace, 's': self.service.vault.seal_browser_state(self.workspace, self.channel, self.native)})
        self.pending()
        self.service.browser.check_session.side_effect = AlfaBrowserError('reauth_required')
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.login.assert_not_called()
        self.assertEqual(control(self.setting())['phase'], 'credentials_required')

    def test_otp_is_preserved_without_repeated_login(self):
        self.pending()
        self.service.browser.check_session.side_effect = AlfaBrowserError('reauth_required')
        self.service.browser.login.return_value = ('otp_required', {'cookies': [], 'origins': []})
        self.service.sessions.recover(self.workspace, self.channel)
        self.assertEqual(self.setting()['state'], 'otp_required')
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.login.assert_called_once()
        stored = self.service.vault.open_browser_state(self.workspace, self.channel, self.setting()['session_ciphertext'])
        self.assertIn(SECRET, stored)

    def test_network_failure_keeps_active_session_and_waits_without_password_submission(self):
        self.pending()
        self.service.browser.check_session.side_effect = AlfaBrowserError('crm_unavailable')
        self.assertEqual(self.service.sessions.recover(self.workspace, self.channel), 60)
        self.assertEqual(self.setting()['state'], 'active')
        self.assertTrue(control(self.setting())['needs_check'])
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.check_session.assert_called_once()
        self.service.browser.login.assert_not_called()

    def test_bad_credentials_stop_automatic_attempts(self):
        self.pending()
        self.service.browser.check_session.side_effect = AlfaBrowserError('reauth_required')
        self.service.browser.login.side_effect = AlfaBrowserError('credentials_rejected')
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.login.assert_called_once()
        self.assertEqual(control(self.setting())['phase'], 'credentials_rejected')

    def test_manual_disconnect_wins_over_an_inflight_login(self):
        self.pending()
        self.service.browser.check_session.side_effect = [AlfaBrowserError('reauth_required'), {'cookies': [], 'origins': []}]
        def login(*args):
            self.service.disconnect(SimpleNamespace(user_id=self.user), self.workspace)
            return 'active', {'cookies': [], 'origins': []}
        self.service.browser.login.side_effect = login
        self.service.sessions.recover(self.workspace, self.channel)
        self.assertEqual(self.setting()['state'], 'not_configured')
        self.assertIsNone(self.setting()['session_ciphertext'])

    def test_manual_setup_encrypts_password_and_otp_retains_it(self):
        self.service._crm = Mock(return_value=({'origin': 'https://test.s20.online', 'connection_id': self.crm}, [1]))
        self.service.status = Mock(return_value={})
        self.service.browser.login.return_value = ('otp_required', {'cookies': [], 'origins': []})
        actor = SimpleNamespace(user_id=self.user)
        self.service.connect(actor, self.workspace, 'synthetic@example.test', SecretStr('synthetic-password'))
        pending = self.setting()
        self.assertNotIn('synthetic-password', pending['session_ciphertext'])
        self.assertEqual(pending['state'], 'otp_required')
        self.service.browser.verify_otp.return_value = ('active', {'cookies': [], 'origins': []})
        self.service.verify_otp(actor, self.workspace, '123456')
        row = self.setting()
        self.assertEqual(row['state'], 'active')
        decrypted = self.service.vault.open_browser_state(self.workspace, self.channel, row['session_ciphertext'])
        self.assertEqual(decrypted[SECRET]['password'], 'synthetic-password')
        self.assertEqual(control(row)['phase'], 'checking')

    def test_external_password_rejection_does_not_expire_messenger_login(self):
        self.service._crm = Mock(return_value=({'origin': 'https://test.s20.online', 'connection_id': self.crm}, [1]))
        self.service.browser.login.side_effect = AlfaBrowserError('credentials_rejected')
        with self.assertRaises(AuthError) as raised:
            self.service.connect(SimpleNamespace(user_id=self.user), self.workspace, 'synthetic@example.test', SecretStr('bad-synthetic'))
        self.assertEqual(raised.exception.status, 422)

    def queue_operator_reply(self):
        self.project(self.row())
        self.membership.update(role='operator', sections=['messages'])
        self.db.execute(text("INSERT INTO workspace_memberships(workspace_id,user_id,role,sections) VALUES(:w,:u,'operator',ARRAY['messages'])"),
                        {'w': self.workspace, 'u': self.user})
        messaging = Messaging(self.reader.max, self.service)
        self.reader.messaging = messaging
        conversation = self.conversation()
        checkpoint = self.reader.history(None, self.workspace, conversation['id'])['conversation']['seen_checkpoint']
        result = self.reader.send(SimpleNamespace(user_id=self.user), self.workspace, conversation['id'], SendInput(
            request_id=uuid4(), text='Synthetic operator reply', seen_inbound_seq=conversation['inbound_seq'], seen_checkpoint=checkpoint))
        self.service.browser.send.return_value = {'accepted': True, 'browser_state': {'cookies': [], 'origins': []}}
        return messaging, result

    def test_operator_goes_through_real_queue_without_admin_role(self):
        messaging, command = self.queue_operator_reply()
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()
        state = self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'), {'id': command['command_id']}).scalar_one()
        self.assertEqual(state, 'accepted')
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()

    def test_revoked_operator_is_cancelled_without_touching_crm_session(self):
        messaging, command = self.queue_operator_reply()
        old = self.setting()['session_ciphertext']
        self.db.execute(text('UPDATE workspace_memberships SET active=false WHERE workspace_id=:w AND user_id=:u'), {'w': self.workspace, 'u': self.user})
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_not_called()
        self.assertEqual(self.setting()['state'], 'active')
        self.assertEqual(self.setting()['session_ciphertext'], old)
        self.assertEqual(self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'), {'id': command['command_id']}).scalar_one(), 'cancelled')

    def test_unknown_submission_is_not_automatically_repeated(self):
        messaging, command = self.queue_operator_reply()
        self.service.browser.send.side_effect = AlfaBrowserError('send_unknown')
        messaging.run_next('send', 'alfa_max_browser')
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()
        self.assertEqual(self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'), {'id': command['command_id']}).scalar_one(), 'unknown')
