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

    def test_css_hidden_login_dialog_does_not_override_customer_card(self):
        from center_messenger.alfa_max_browser import _customer_card_compose_url
        html = '<div class="modal"><form action="/auth/login"><input name="email"><input type="password"></form></div>'
        html += '<a href="/company/1/max/bulk?related_class=Customer&amp;related_ids%5B%5D=42&amp;maxUserId=77">MAX</a>'
        self.assertIn('maxUserId=77', _customer_card_compose_url(html, origin='https://test.s20.online', branch=1, customer=42))

    def test_pending_otp_page_is_distinct_from_password_login(self):
        html = '<form action="/auth/login"><input name="LoginForm[code]" autocomplete="one-time-code"></form>'
        with self.assertRaises(AlfaBrowserError) as raised:
            parse_history_html(html, origin='https://test.s20.online', branch=1, direction='receive')
        self.assertEqual(raised.exception.code, 'otp_required')

    def test_resource_http_errors_do_not_all_become_logout(self):
        from center_messenger.alfa_max_browser import AlfaMaxBrowser
        for status, expected in [(401, 'reauth_required'), (403, 'crm_access_denied'), (429, 'crm_rate_limited'), (503, 'crm_unavailable')]:
            page = Mock(url='https://test.s20.online/company/1/max/receive')
            page.goto.return_value = SimpleNamespace(status=status)
            with self.assertRaises(AlfaBrowserError) as raised:
                AlfaMaxBrowser._read_history_page(page, 'https://test.s20.online', 1, 'receive', 0)
            self.assertEqual(raised.exception.code, expected)
            self.assertEqual(raised.exception.http_status, status)

    def test_known_acceptance_survives_browser_cookie_export_failure(self):
        from center_messenger.alfa_max_browser import AlfaMaxBrowser
        page, context = Mock(), Mock()
        page.url = 'https://test.s20.online/company/1/customer/view?id=42'
        bulk = 'https://test.s20.online/company/1/max/bulk?related_class=Customer&related_ids%5B%5D=42&maxUserId=77'
        form = ('<form action="' + bulk.replace('&', '&amp;') + '"><input name="_csrf" value="synthetic">'
                '<input name="MaxComposeBulkForm[related_ids][]" value="42"><textarea name="MaxComposeBulkForm[text]"></textarea></form>')
        page.goto.return_value = SimpleNamespace(status=200)
        page.content.return_value = '<a href="' + bulk.replace('&', '&amp;') + '">MAX</a>'
        page.evaluate.side_effect = [{'status': 200, 'url': bulk, 'text': form}, {'status': 200, 'url': bulk, 'text': '{"success":true}'}]
        context.storage_state.side_effect = RuntimeError('synthetic export failure')
        with patch.object(AlfaMaxBrowser, '_open', return_value=(Mock(), Mock(), context, page)):
            result = AlfaMaxBrowser()._send_inline('https://test.s20.online', {'cookies': []}, 1, 42, 'Synthetic reply')
        self.assertTrue(result['accepted'])
        self.assertNotIn('browser_state', result)

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
        self.service._crm = Mock(return_value=({'origin': 'https://test.s20.online', 'connection_id': self.crm, 'version': 1}, [1]))
        self.service.alfa._row = Mock(return_value=self.service._crm.return_value[0])
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
        self.service._crm = Mock(return_value=({'origin': 'https://test.s20.online', 'connection_id': self.crm, 'version': 1}, [1]))
        old = self.setting()
        self.service.browser.login.side_effect = AlfaBrowserError('credentials_rejected')
        with self.assertRaises(AuthError) as raised:
            self.service.connect(SimpleNamespace(user_id=self.user), self.workspace, 'synthetic@example.test', SecretStr('bad-synthetic'))
        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.setting()['state'], old['state'])
        self.assertEqual(self.setting()['session_ciphertext'], old['session_ciphertext'])
        self.assertEqual(self.setting()['version'], old['version'])

    def retry_now(self):
        self.db.execute(text("UPDATE alfa_max_settings SET history_cursor=jsonb_set(history_cursor,"
                             "'{session_control,after}','null'::jsonb) WHERE workspace_id=:w"), {'w': self.workspace})

    def test_successful_login_survives_network_failure_of_confirmation_read(self):
        self.pending()
        fresh = {'cookies': [{'name': 'fresh', 'value': 'rotated'}], 'origins': []}
        self.service.browser.login.return_value = ('active', fresh)
        self.service.browser.check_session.side_effect = [AlfaBrowserError('reauth_required'), AlfaBrowserError('crm_unavailable'), fresh]
        self.service.sessions.recover(self.workspace, self.channel)
        row = self.setting()
        stored = self.service.vault.open_browser_state(self.workspace, self.channel, row['session_ciphertext'])
        self.assertEqual(stored['cookies'], fresh['cookies'])
        self.assertTrue(control(row)['needs_check'])
        self.retry_now()
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.login.assert_called_once()
        self.assertEqual(self.setting()['state'], 'active')
        self.assertEqual(self.service.browser.check_session.call_args.args[1]['cookies'], fresh['cookies'])

    def test_partial_login_result_is_checked_before_repeating_password(self):
        self.pending()
        fresh = {'cookies': [{'name': 'fresh', 'value': 'partial'}], 'origins': []}
        error = AlfaBrowserError('login_failed')
        error.browser_state = fresh
        self.service.browser.login.side_effect = error
        self.service.browser.check_session.side_effect = [AlfaBrowserError('reauth_required'), fresh]
        self.service.sessions.recover(self.workspace, self.channel)
        self.retry_now()
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.login.assert_called_once()
        self.assertEqual(self.service.browser.check_session.call_args.args[1]['cookies'], fresh['cookies'])
        self.assertEqual(self.setting()['state'], 'active')

    def test_pending_code_detected_by_check_does_not_reissue_login(self):
        self.pending()
        error = AlfaBrowserError('otp_required')
        error.browser_state = {'cookies': [{'name': 'pending', 'value': 'code'}]}
        self.service.browser.check_session.side_effect = error
        self.service.sessions.recover(self.workspace, self.channel)
        self.assertEqual(self.setting()['state'], 'otp_required')
        self.service.browser.login.assert_not_called()
        self.service.sessions.recover(self.workspace, self.channel)
        self.service.browser.check_session.assert_called_once()

    def test_otp_network_error_preserves_new_cookies_and_checks_first(self):
        self.db.execute(text("UPDATE alfa_max_settings SET state='otp_required' WHERE workspace_id=:w"), {'w': self.workspace})
        error = AlfaBrowserError('crm_unavailable')
        error.browser_state = {'cookies': [{'name': 'otp', 'value': 'accepted'}]}
        self.service.browser.verify_otp.side_effect = error
        with self.assertRaises(AuthError):
            self.service.verify_otp(SimpleNamespace(user_id=self.user), self.workspace, '123456')
        self.assertTrue(control(self.setting())['needs_check'])
        self.service.sessions.recover(self.workspace, self.channel)
        self.assertEqual(self.service.browser.check_session.call_args.args[1]['cookies'], error.browser_state['cookies'])
        self.service.browser.verify_otp.assert_called_once()
        self.service.browser.login.assert_not_called()

    def test_new_crm_configuration_rejects_old_session_results(self):
        old = self.setting()
        self.db.execute(text('UPDATE alfa_settings SET version=version+1 WHERE workspace_id=:w'), {'w': self.workspace})
        self.assertFalse(self.service.sessions.suspect(self.db, self.workspace, old, 'old_provider'))
        self.assertEqual(self.setting()['state'], 'active')

    def test_branch_access_denial_falls_back_without_relogin(self):
        self.pending()
        self.service.browser.check_session.side_effect = [AlfaBrowserError('crm_access_denied'), {'cookies': []}]
        self.service.sessions.recover(self.workspace, self.channel)
        self.assertEqual([call.args[2] for call in self.service.browser.check_session.call_args_list], [1, 2])
        self.service.browser.login.assert_not_called()
        self.assertEqual(self.setting()['state'], 'active')

    def test_release_failure_does_not_change_accepted_submission(self):
        from sqlalchemy.exc import SQLAlchemyError
        messaging, command = self.queue_operator_reply()
        with patch.object(self.service.sessions, '_release', side_effect=SQLAlchemyError('synthetic unavailable')):
            messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()
        self.assertEqual(self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'), {'id': command['command_id']}).scalar_one(), 'accepted')

    def test_backoff_is_one_five_then_fifteen_minutes(self):
        self.pending()
        self.service.browser.check_session.side_effect = AlfaBrowserError('crm_unavailable')
        delays = []
        for _ in range(4):
            self.retry_now()
            delays.append(self.service.sessions.recover(self.workspace, self.channel))
        self.assertEqual(delays, [60, 300, 900, 900])
        self.service.browser.login.assert_not_called()

    def test_cookie_save_does_not_clear_history_gap(self):
        self.db.execute(text("UPDATE alfa_max_settings SET last_error_code='history_gap' WHERE workspace_id=:w"), {'w': self.workspace})
        with self.service.sessions.operation(self.workspace, self.channel) as operation:
            self.service.sessions.finish(operation, self.native, state='active')
        self.assertEqual(self.setting()['last_error_code'], 'history_gap')

    def test_disabled_automatic_login_is_preserved_and_does_not_submit_password(self):
        self.service._crm = Mock(return_value=({'origin': 'https://test.s20.online', 'connection_id': self.crm, 'version': 1}, [1]))
        self.service.alfa._row = Mock(return_value=self.service._crm.return_value[0])
        self.service.status = Mock(return_value={})
        self.service.connect(SimpleNamespace(user_id=self.user), self.workspace, 'synthetic@example.test', SecretStr('synthetic-only'), False)
        self.service.browser.login.reset_mock()
        public = self.service.public(self.setting())
        self.assertTrue(public['recovery']['configured'])
        self.assertFalse(public['recovery']['enabled'])
        self.pending()
        self.service.browser.check_session.side_effect = AlfaBrowserError('reauth_required')
        self.service.sessions.recover(self.workspace, self.channel)
        self.assertEqual(control(self.setting())['phase'], 'automatic_disabled')
        self.service.browser.login.assert_not_called()

    def test_manual_login_keeps_new_cookie_if_confirmation_network_fails(self):
        crm = {'origin': 'https://test.s20.online', 'connection_id': self.crm, 'version': 1}
        self.service._crm = Mock(return_value=(crm, [1]))
        self.service.alfa._row = Mock(return_value=crm)
        self.service.status = Mock(return_value={})
        fresh = {'cookies': [{'name': 'candidate', 'value': 'new'}], 'origins': []}
        self.service.browser.login.return_value = ('active', fresh)
        self.service.browser.check_session.side_effect = AlfaBrowserError('crm_unavailable')
        self.service.connect(SimpleNamespace(user_id=self.user), self.workspace, 'synthetic@example.test', SecretStr('synthetic-only'))
        row = self.setting()
        self.assertTrue(control(row)['needs_check'])
        self.assertEqual(self.service.vault.open_browser_state(self.workspace, self.channel, row['session_ciphertext'])['cookies'], fresh['cookies'])

    def test_transition_history_is_bounded_safe_and_contains_generations(self):
        with self.service.sessions.operation(self.workspace, self.channel) as operation:
            for _ in range(12):
                self.service.sessions.finish(operation, self.native, state='active', new_generation=True)
        recent = self.service.public(self.setting())['recovery']['recent']
        self.assertEqual(len(recent), 10)
        self.assertTrue(all(item['generation_after'] == item['generation'] + 1 for item in recent))
        self.assertNotIn('synthetic-only', json.dumps(recent))

    def test_invitation_accepted_by_another_user_leaves_shared_session_and_owner_login_intact(self):
        from center_messenger.accounts import Accounts
        from center_messenger.auth_security import InvitationInput, AcceptInvitationInput
        self.db.execute(text("INSERT INTO workspace_memberships(workspace_id,user_id,role) VALUES(:w,:u,'owner')"), {'w': self.workspace, 'u': self.user})
        owner_session = uuid4()
        self.db.execute(text("INSERT INTO auth_sessions(id,user_id,token_hash,auth_epoch,device_label,expires_at) VALUES(:id,:u,:hash,0,'Owner device',now()+interval '12 hours')"),
                        {'id': owner_session, 'u': self.user, 'hash': uuid4().hex * 2})
        ops = Accounts.__new__(Accounts)
        ops.transaction = self.accounts.transaction
        ops.throttle = Mock()
        ops.settings = SimpleNamespace(public_url='https://messenger.example.test', session_hours=12)
        ops.passwords = Mock()
        ops.passwords.hash.return_value = 'synthetic-not-a-real-password-hash'
        old = self.setting()
        invite = ops.invite(SimpleNamespace(user_id=self.user), self.workspace, InvitationInput(email='new-operator@example.com', role='operator'))
        raw = invite['url'].split('#invite=')[1]
        ops.accept_invitation(AcceptInvitationInput(token=raw, display_name='Synthetic operator', password='synthetic-long-password'), None, '127.0.0.1', 'Other device')
        self.assertEqual(self.setting()['session_ciphertext'], old['session_ciphertext'])
        self.assertEqual(self.setting()['version'], old['version'])
        self.assertIsNone(self.db.execute(text('SELECT revoked_at FROM auth_sessions WHERE id=:id'), {'id': owner_session}).scalar_one())
        self.assertEqual(self.db.execute(text("SELECT role FROM workspace_memberships m JOIN auth_users u ON u.id=m.user_id WHERE u.email='new-operator@example.com'")).scalar_one(), 'operator')

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
        def send(*args, **options):
            options['heartbeat']()
            options['before_post']()
            result = self.service.browser.send.return_value
            options['on_accepted'](result)
            return result
        self.service.browser.send.side_effect = send
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

    def test_history_browser_lease_defers_send_without_claiming_logout(self):
        messaging, command = self.queue_operator_reply()
        operation = self.service.sessions.claim(self.workspace, self.channel)
        try:
            messaging.run_next('send', 'alfa_max_browser')
            row = self.db.execute(text('SELECT state,last_error_code FROM send_commands WHERE id=:id'),
                                  {'id': command['command_id']}).mappings().one()
            self.assertEqual((row['state'], row['last_error_code']), ('queued', 'browser_busy'))
            self.service.browser.send.assert_not_called()
            self.assertFalse(control(self.setting()).get('needs_check'))
            message = self.reader.history(None, self.workspace, self.conversation()['id'])['messages'][-1]
            self.assertIn('Браузер AlfaCRM занят', message['error'])
            self.assertNotIn('авторизац', message['error'])
        finally:
            self.service.sessions.release(operation)
        self.db.execute(text("UPDATE message_jobs SET due_at=now()-interval '1 second' WHERE subject_id=:id"),
                        {'id': command['command_id']})
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()
        self.assertEqual(self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'),
                                        {'id': command['command_id']}).scalar_one(), 'accepted')

    def test_changed_snapshot_defers_send_with_its_actual_reason(self):
        messaging, command = self.queue_operator_reply()
        with patch.object(self.service, 'send_native', side_effect=AlfaBrowserError('settings_changed')):
            messaging.run_next('send', 'alfa_max_browser')
        row = self.db.execute(text('SELECT state,last_error_code FROM send_commands WHERE id=:id'),
                              {'id': command['command_id']}).mappings().one()
        self.assertEqual((row['state'], row['last_error_code']), ('queued', 'settings_changed'))
        self.assertFalse(control(self.setting()).get('needs_check'))
        self.service.browser.send.assert_not_called()

    def test_existing_negative_infinity_wait_is_claimed_and_sent_once(self):
        messaging, command = self.queue_operator_reply()
        self.db.execute(text("UPDATE message_jobs SET lease_until='-infinity',lease_owner=NULL "
                             "WHERE subject_id=:id"), {'id': command['command_id']})
        self.db.execute(text("UPDATE send_commands SET last_error_code='session_check_pending' "
                             "WHERE id=:id"), {'id': command['command_id']})
        messaging.run_next('send', 'alfa_max_browser')
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()
        self.assertEqual(self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'),
                                        {'id': command['command_id']}).scalar_one(), 'accepted')

    def test_actual_authorization_check_retains_auth_wait_reason(self):
        messaging, command = self.queue_operator_reply()
        self.pending()
        messaging.run_next('send', 'alfa_max_browser')
        row = self.db.execute(text('SELECT state,last_error_code FROM send_commands WHERE id=:id'),
                              {'id': command['command_id']}).mappings().one()
        self.assertEqual((row['state'], row['last_error_code']), ('queued', 'session_check_pending'))
        self.assertTrue(control(self.setting()).get('needs_check'))
        self.service.browser.send.assert_not_called()

    def test_unknown_submission_is_not_automatically_repeated(self):
        messaging, command = self.queue_operator_reply()
        self.service.browser.send.side_effect = AlfaBrowserError('send_unknown')
        messaging.run_next('send', 'alfa_max_browser')
        messaging.run_next('send', 'alfa_max_browser')
        self.service.browser.send.assert_called_once()
        self.assertEqual(self.db.execute(text('SELECT state FROM send_commands WHERE id=:id'), {'id': command['command_id']}).scalar_one(), 'unknown')

    def native_command(self, command):
        return self.db.execute(text('SELECT * FROM send_commands WHERE id=:id'),
                               {'id': command['command_id']}).mappings().one()

    def ready_native_job(self, command):
        self.db.execute(text("UPDATE message_jobs SET due_at=clock_timestamp()-interval '1 second',"
                             "lease_until=clock_timestamp()-interval '1 second' WHERE subject_id=:id"),
                        {'id': command['command_id']})

    def test_prepare_timeout_is_retryable_and_keeps_the_same_command(self):
        messaging, command = self.queue_operator_reply()
        success = self.service.browser.send.side_effect
        self.service.browser.send.side_effect = AlfaBrowserError('send_prepare_timeout')
        messaging.run_next('send', 'alfa_max_browser')
        row = self.native_command(command)
        self.assertEqual((row['state'], row['last_error_code']), ('queued', 'send_prepare_timeout'))
        self.assertEqual(row['payload']['native_delivery']['phase'], 'waiting')
        self.ready_native_job(command)
        self.service.browser.send.side_effect = success
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(self.native_command(command)['state'], 'accepted')
        self.assertEqual(self.db.execute(text('SELECT count(*) FROM send_commands')).scalar_one(), 1)

    def test_restart_in_preparation_can_resume_but_restart_after_intent_cannot(self):
        for post_started in (False, True):
            with self.subTest(post_started=post_started):
                # Resume the same command after preparation; never replay its POST.
                messaging, command = self.queue_operator_reply() if not post_started else (messaging, command)
                success = self.service.browser.send.side_effect
                def crash(*args, **options):
                    if post_started:
                        options['before_post']()
                    raise SystemExit('synthetic process death')
                self.service.browser.send.side_effect = crash
                with self.assertRaises(SystemExit):
                    messaging.run_next('send', 'alfa_max_browser')
                row = self.native_command(command)
                self.assertEqual(row['state'], 'sending' if post_started else 'queued')
                self.assertEqual(row['payload']['native_delivery']['phase'], 'post_started' if post_started else 'preparing')
                self.ready_native_job(command)
                self.service.browser.send.side_effect = success
                if not post_started:
                    # Resume into a second simulated crash, now after durable POST intent.
                    continue
                calls = self.service.browser.send.call_count
                messaging.run_next('send', 'alfa_max_browser')
                self.assertEqual(self.native_command(command)['state'], 'unknown')
                self.assertEqual(self.service.browser.send.call_count, calls)

    def test_revocation_while_preparing_is_checked_again_before_post(self):
        messaging, command = self.queue_operator_reply()
        posted = []
        def send(*args, **options):
            self.db.execute(text('UPDATE workspace_memberships SET active=false WHERE workspace_id=:w AND user_id=:u'),
                            {'w': self.workspace, 'u': self.user})
            options['before_post']()
            posted.append(True)
        self.service.browser.send.side_effect = send
        original = self.setting()['session_ciphertext']
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(posted, [])
        self.assertEqual(self.native_command(command)['state'], 'cancelled')
        self.assertEqual(self.native_command(command)['last_error_code'], 'access_changed')
        self.assertEqual(self.setting()['session_ciphertext'], original)
        self.assertEqual(self.setting()['state'], 'active')

    def test_session_generation_change_prevents_post_and_preserves_new_session(self):
        messaging, command = self.queue_operator_reply()
        posted = []
        def send(*args, **options):
            self.db.execute(text('UPDATE alfa_max_settings SET version=version+1 WHERE workspace_id=:w'), {'w': self.workspace})
            options['before_post']()
            posted.append(True)
        self.service.browser.send.side_effect = send
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(posted, [])
        self.assertEqual(self.native_command(command)['last_error_code'], 'settings_changed')
        self.assertEqual(self.native_command(command)['state'], 'queued')

    def test_heartbeat_renews_both_leases_and_intent_is_durable_before_submit(self):
        messaging, command = self.queue_operator_reply()
        def send(*args, **options):
            meta = control(self.setting())
            meta['until'] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
            self.service.sessions._write_control(self.db, self.workspace, meta)
            self.db.execute(text("UPDATE message_jobs SET lease_until=clock_timestamp()+interval '10 seconds' WHERE subject_id=:id"),
                            {'id': command['command_id']})
            options['heartbeat']()
            from center_messenger.alfa_session import stamp
            self.assertGreater((stamp(control(self.setting())['until']) - datetime.now(UTC)).total_seconds(), 150)
            until = self.db.execute(text('SELECT lease_until FROM message_jobs WHERE subject_id=:id'),
                                    {'id': command['command_id']}).scalar_one()
            self.assertGreater((until - datetime.now(UTC)).total_seconds(), 150)
            self.assertEqual(self.native_command(command)['state'], 'queued')
            self.db.execute(text("UPDATE messages SET occurred_at=clock_timestamp()-interval '1 hour' "
                                'WHERE send_command_id=:id'), {'id': command['command_id']})
            options['before_post']()
            row = self.native_command(command)
            self.assertEqual(row['state'], 'sending')
            self.assertEqual(row['payload']['native_delivery']['phase'], 'post_started')
            posted_at = self.db.execute(text('SELECT occurred_at FROM messages WHERE send_command_id=:id'),
                                       {'id': command['command_id']}).scalar_one()
            self.assertLess((datetime.now(UTC) - posted_at).total_seconds(), 3)
            receipt = {'accepted': True}
            options['on_accepted'](receipt)
            self.assertEqual(self.native_command(command)['state'], 'accepted')
            return receipt
        self.service.browser.send.side_effect = send
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(self.native_command(command)['state'], 'accepted')

    def test_expired_browser_lease_cannot_be_renewed_or_submit(self):
        operation = self.service.sessions.claim(self.workspace, self.channel)
        meta = control(self.setting())
        meta['until'] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.service.sessions._write_control(self.db, self.workspace, meta)
        with self.assertRaises(AlfaBrowserError) as raised:
            self.service.sessions.renew(self.db, operation, force=True)
        self.assertEqual(raised.exception.code, 'settings_changed')
        self.service.sessions.release(operation)

    def test_late_receipt_survives_reclaimed_job_and_does_not_replace_new_cookies(self):
        messaging, command = self.queue_operator_reply()
        renewed = self.service.vault.seal_browser_state(self.workspace, self.channel, {'cookies': [{'name': 'new-generation'}]})
        def send(*args, **options):
            options['before_post']()
            self.ready_native_job(command)
            messaging.run_next('send', 'alfa_max_browser')
            self.assertEqual(self.native_command(command)['state'], 'unknown')
            self.db.execute(text('UPDATE alfa_max_settings SET version=version+1,session_ciphertext=:s WHERE workspace_id=:w'),
                            {'w': self.workspace, 's': renewed})
            receipt = {'accepted': True, 'browser_state': {'cookies': [{'name': 'stale-generation'}]}}
            options['on_accepted'](receipt)
            return receipt
        self.service.browser.send.side_effect = send
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(self.native_command(command)['state'], 'accepted')
        self.assertEqual(self.setting()['session_ciphertext'], renewed)
        self.service.browser.send.assert_called_once()

    def test_receipt_for_another_attempt_cannot_overwrite_command(self):
        messaging, command = self.queue_operator_reply()
        job = {'workspace_id': self.workspace, 'subject_id': command['command_id']}
        self.assertFalse(messaging._native_receipt(job, str(uuid4()), {'accepted': True}))
        self.assertEqual(self.native_command(command)['state'], 'queued')

    def test_unknown_after_real_submit_intent_is_never_retried(self):
        messaging, command = self.queue_operator_reply()
        def send(*args, **options):
            options['before_post']()
            raise AlfaBrowserError('send_unknown')
        self.service.browser.send.side_effect = send
        messaging.run_next('send', 'alfa_max_browser')
        messaging.run_next('send', 'alfa_max_browser')
        self.assertEqual(self.native_command(command)['state'], 'unknown')
        self.service.browser.send.assert_called_once()
