"""Operator feedback never confuses transport uncertainty with a failed send."""
from datetime import UTC, datetime
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.chat_api import ChatReader
from center_messenger.messaging import Messaging


class SendFeedbackTests(unittest.TestCase):
    def row(self, state, code='browser_busy', **payload):
        return dict(id='message', payload=payload, direction='out', actor_type='human',
                    text='Reply', occurred_at=datetime.now(UTC), version=1,
                    send_state=state, last_error_code=code, client_request_id='request')

    def test_waiting_and_success_keep_diagnostics_without_public_errors(self):
        for state in ('queued', 'sending', 'unknown', 'accepted', 'delivered', 'read'):
            for code in ('browser_busy', 'settings_changed', 'unknown', 'crm_unavailable'):
                with self.subTest(state=state, code=code):
                    result = ChatReader._message(self.row(state, code))
                    self.assertIsNone(result['error'])
                    self.assertEqual(result['error_code'], code)
                    self.assertEqual(result['state'], state)

    def test_only_proven_terminal_failure_has_one_plain_message(self):
        for state, payload in (('failed', {}), ('cancelled', {}), (None, {'failed': True})):
            result = ChatReader._message(self.row(state, 'crm_rejected', **payload))
            self.assertEqual(result['error'], 'Сообщение не отправлено')

    def test_unknown_wakes_source_reader_without_replaying_the_send(self):
        crm, db = Mock(), Mock()
        db.execute.return_value.scalar_one.return_value = 'message'
        service = Messaging(SimpleNamespace(accounts=Mock()), crm)
        job = dict(workspace_id='w', connection_id='c', channel_kind='alfa_max_browser')
        command = dict(id='command', conversation_id='conversation')
        with patch('center_messenger.messaging.changed'), patch('center_messenger.messaging.refresh_unread'):
            service._state(db, job, command, 'unknown', 'unknown')
        crm._schedule.assert_called_once_with(db, 'w', 'c')
        crm.send_native.assert_not_called()

    def test_bot_and_terminal_states_do_not_wake_native_browser(self):
        for channel, state in (('max_bot', 'unknown'), ('alfa_max_browser', 'accepted'), ('alfa_max_browser', 'failed')):
            crm, db = Mock(), Mock()
            service = Messaging(SimpleNamespace(accounts=Mock()), crm)
            with patch('center_messenger.messaging.changed'), patch('center_messenger.messaging.refresh_unread'):
                service._state(db, dict(workspace_id='w', connection_id='c', channel_kind=channel),
                               dict(id='command', conversation_id='conversation'), state)
            crm._schedule.assert_not_called()
