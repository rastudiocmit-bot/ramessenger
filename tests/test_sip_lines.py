"""Synthetic SIP profiles; no provider, registration or calls."""
import asyncio
import copy
import json
import secrets
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from center_messenger.auth_security import AuthError, csrf_token
from center_messenger.connector_vault import ConnectorVault
from center_messenger.sip_lines import SipLines, TOPIC

PASSWORD = 'synthetic-SIP-password-91'


class Result:
    def __init__(self, value=None): self.value = value
    def scalar_one_or_none(self): return copy.deepcopy(self.value)
    def mappings(self): return self
    def all(self): return self.value


class MemoryDB:
    def __init__(self): self.rows, self.members, self.events, self.locks = {}, {}, [], 0
    def execute(self, sql, params=None):
        sql, p = str(sql), params or {}
        if sql.startswith('SELECT pg_advisory_xact_lock'):
            self.locks += 1; return Result()
        if sql.startswith('SELECT payload FROM outbox_events'):
            assert p['t'] == TOPIC
            return Result(self.rows.get(p['w']))
        if sql.startswith('SELECT m.user_id,u.display_name'):
            return Result(list(self.members.get(p['w'], {}).values()))
        if sql.startswith('INSERT INTO outbox_events'):
            assert p['t'] == TOPIC and "'published'" in sql
            self.rows[p['w']] = json.loads(p['p']); return Result()
        if sql.startswith('INSERT INTO auth_events'):
            self.events.append(dict(p)); return Result()
        raise AssertionError('Unexpected SQL')


class Accounts:
    def __init__(self, db, workspace): self.db, self.workspace, self.role = db, workspace, 'owner'
    @contextmanager
    def transaction(self, actor, **kwargs):
        before = copy.deepcopy(self.db.rows)
        try: yield self.db
        except Exception:
            self.db.rows = before; raise
    def _workspace(self, db, actor, workspace, manage=False):
        if workspace != self.workspace: raise AuthError(404, 'Недоступно.')
        if manage and self.role not in ('owner', 'admin'): raise AuthError(403, 'Нужны права администратора.')
        return {'role': self.role}
    def _workspace_chat(self, db, actor, workspace): return self._workspace(db, actor, workspace)


class SipLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / 'key'; self.key.write_text(secrets.token_urlsafe(48))
        self.vault = ConnectorVault(self.key); self.workspace, self.user, self.other = uuid4(), uuid4(), uuid4()
        self.actor = SimpleNamespace(user_id=self.user)
        self.db = MemoryDB(); self.db.members[self.workspace] = {str(u): {'user_id': u, 'display_name': 'Тёзка', 'email': f'staff-{i}@example.test', 'role': 'operator', 'sections': ['messages']} for i, u in enumerate((self.user, self.other))}
        self.accounts = Accounts(self.db, self.workspace); self.service = SipLines(self.accounts, self.vault)

    def data(self, **values):
        return {'expected_version': 0, 'line_id': None, 'member_id': self.user, 'sip_number': '0001234', 'extension': '001', 'server': 'sip.novofon.ru', 'port': 5060, 'password': PASSWORD, 'enabled': True, 'primary': True} | values

    def save(self, **values): return self.service.save(self.actor, self.workspace, self.data(**values))

    def test_distinct_employee_profiles_keep_zeros_and_only_ciphertext_is_persisted(self):
        first = self.save(); second = self.save(expected_version=1, member_id=self.other, sip_number='0005678', password='synthetic-second-password')
        self.assertEqual([line['sip_number'] for line in second['lines']], ['0001234', '0005678'])
        self.assertEqual(first['lines'][0]['extension'], '001')
        for value in (first, second, self.db.rows, self.db.events):
            self.assertNotIn(PASSWORD, str(value)); self.assertNotIn('synthetic-second-password', str(value))
        self.assertNotIn('password_ciphertext', str(second)); self.assertFalse(second['browser_call_ready'])
        self.assertTrue(all(line['registration_status'] == 'not_checked' for line in second['lines']))
        self.assertEqual(self.db.locks, 2)

    def test_cipher_is_bound_to_workspace_line_and_employee_and_detects_corruption(self):
        self.save(); line = self.db.rows[self.workspace]['lines'][0]
        params = [self.workspace, line['id'], line['member_id'], line['password_ciphertext']]
        self.assertEqual(self.service.secrets.open(*params), PASSWORD)
        for i in range(3):
            wrong = params[:]; wrong[i] = uuid4()
            with self.assertRaises(AuthError): self.service.secrets.open(*wrong)
        wrong = params[:]; wrong[3] = wrong[3][:-3] + 'xxx'
        with self.assertRaises(AuthError): self.service.secrets.open(*wrong)

    def test_staff_can_read_only_own_profiles_and_cannot_manage_any(self):
        self.save(); self.save(expected_version=1, member_id=self.other, sip_number='0005678')
        self.accounts.role = 'operator'; mine = self.service.mine(self.actor, self.workspace)
        self.assertEqual(len(mine['lines']), 1); self.assertNotIn('0005678', str(mine)); self.assertNotIn('members', mine)
        self.assertNotIn('password_ciphertext', str(mine)); self.assertTrue(mine['may_call'])
        self.assertNotIn('@example.test', str(mine))
        for action in (lambda: self.service.settings(self.actor, self.workspace), lambda: self.save(expected_version=2), lambda: self.service.delete(self.actor, self.workspace, uuid4(), 2)):
            with self.assertRaises(AuthError) as error: action()
            self.assertEqual(error.exception.status, 403)
        self.accounts.role = 'observer'; self.assertFalse(self.service.mine(self.actor, self.workspace)['may_call'])

    def test_foreign_workspace_or_nonmember_assignment_is_rejected(self):
        with self.assertRaises(AuthError): self.service.settings(self.actor, uuid4())
        with self.assertRaises(AuthError): self.save(member_id=uuid4())
        self.assertEqual(self.db.rows, {})

    def test_stale_save_and_delete_do_not_overwrite_newer_credentials(self):
        first = self.save(); line = first['lines'][0]; before = copy.deepcopy(self.db.rows)
        with self.assertRaises(AuthError) as error: self.save(expected_version=0, line_id=line['id'], password='synthetic-new-password')
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(AuthError): self.service.delete(self.actor, self.workspace, line['id'], 0)
        self.assertEqual(self.db.rows, before)

    def test_blank_password_keeps_existing_secret_but_identity_changes_require_reentry(self):
        line = self.save()['lines'][0]; cipher = self.db.rows[self.workspace]['lines'][0]['password_ciphertext']
        result = self.save(expected_version=1, line_id=line['id'], password=None, extension='002')
        self.assertEqual(self.db.rows[self.workspace]['lines'][0]['password_ciphertext'], cipher)
        for changed in ({'sip_number': '0000009'}, {'member_id': self.other}, {'port': 5061}):
            with self.assertRaises(AuthError): self.save(expected_version=2, line_id=line['id'], password=None, **changed)
        self.assertEqual(result['version'], 2)

    def test_duplicate_account_cannot_be_assigned_to_a_different_employee_or_port(self):
        self.save()
        for changed in ({'member_id': self.other}, {'port': 5061}):
            with self.assertRaises(AuthError) as error: self.save(expected_version=1, **changed)
            self.assertEqual(error.exception.status, 409)

    def test_primary_switch_disable_and_delete_keep_one_enabled_default(self):
        a = self.save()['lines'][0]['id']; result = self.save(expected_version=1, sip_number='0005678'); b = result['lines'][-1]['id']
        self.assertEqual([x['id'] for x in result['lines'] if x['primary']], [b])
        result = self.save(expected_version=2, line_id=b, sip_number='0005678', password=None, enabled=False, primary=False)
        self.assertEqual([x['id'] for x in result['lines'] if x['primary']], [a])
        result = self.service.delete(self.actor, self.workspace, a, 3)
        self.assertFalse(any(x['primary'] for x in result['lines']))
        self.assertEqual(result['version'], 4)

    def test_initial_or_changed_line_requires_password_and_validation_does_not_echo_it(self):
        for changed in ({'password': None}, {'password': PASSWORD + '\n'}, {'server': 'private.example'}, {'sip_number': 'sip:user@example'}, {'port': True}):
            with self.assertRaises(AuthError) as error: self.save(**changed)
            self.assertNotIn(PASSWORD, error.exception.detail)
        self.assertEqual(self.db.rows, {})

    def test_only_five_lines_per_employee_and_disconnected_members_are_visible_to_admin(self):
        for n in range(5): self.save(expected_version=n, sip_number='000' + str(n))
        with self.assertRaises(AuthError): self.save(expected_version=5, sip_number='0005')
        self.db.members[self.workspace].pop(str(self.user))
        result = self.service.settings(self.actor, self.workspace)
        self.assertTrue(all(not x['member_active'] for x in result['lines']))

    def test_admin_can_distinguish_same_name_members_by_email(self):
        members = self.service.settings(self.actor, self.workspace)['members']
        self.assertEqual([m['name'] for m in members], ['Тёзка', 'Тёзка'])
        self.assertEqual({m['email'] for m in members}, {'staff-0@example.test', 'staff-1@example.test'})
        self.assertEqual({m['id'] for m in members}, {str(self.user), str(self.other)})

    def test_member_query_excludes_disabled_users_and_inactive_memberships(self):
        from sqlalchemy import create_engine, text
        engine = create_engine('sqlite://'); self.addCleanup(engine.dispose)
        with engine.begin() as db:
            db.execute(text('CREATE TABLE auth_users(id text, display_name text, email text, active boolean)'))
            db.execute(text('CREATE TABLE workspace_memberships(user_id text, workspace_id text, role text, sections text, active boolean)'))
            for i, (user_active, member_active) in enumerate(((True, True), (False, True), (True, False))):
                db.execute(text('INSERT INTO auth_users VALUES(:id,:name,:email,:active)'), {'id': str(i), 'name': 'Тёзка', 'email': f'{i}@example.test', 'active': user_active})
                db.execute(text('INSERT INTO workspace_memberships VALUES(:id,:w,\'operator\',\'messages\',:active)'), {'id': str(i), 'w': 'test-workspace', 'active': member_active})
            self.assertEqual(set(self.service._members(db, 'test-workspace')), {'0'})

    def test_malformed_stored_ids_fail_safely_without_leaking_credentials(self):
        self.save(); original = copy.deepcopy(self.db.rows[self.workspace])
        for key in ('id', 'member_id'):
            for invalid in (123, None, [], {}, 'not-a-uuid'):
                with self.subTest(key=key, invalid=invalid):
                    value = copy.deepcopy(original); value['lines'][0][key] = invalid
                    self.db.rows[self.workspace] = value
                    with self.assertRaises(AuthError) as error:
                        self.service.settings(self.actor, self.workspace)
                    self.assertEqual(error.exception.status, 503)
                    self.assertNotIn(PASSWORD, error.exception.detail)
                    self.assertNotIn(original['lines'][0]['password_ciphertext'], error.exception.detail)


class SipHttpTests(unittest.TestCase):
    setUp = SipLineTests.setUp
    data = SipLineTests.data
    def request(self, payload=None, csrf=True, *, method='PUT', suffix=''):
        from center_messenger.main import create_app
        from center_messenger.config import Settings
        app = create_app(Settings(public_url='https://cabinet.example.test'))
        self.actor.raw_session = 'synthetic-session'
        self.accounts.settings = SimpleNamespace(cookie_name='session')
        self.accounts.authenticate = lambda raw: self.actor
        app.state.accounts = self.accounts; app.state.alfa = SimpleNamespace(vault=self.vault)
        body = json.dumps(payload).encode(); path = f'/api/workspaces/{self.workspace}/integrations/novofon/sip-lines{suffix}'
        async def run():
            headers = [(b'content-type', b'application/json'), (b'origin', b'https://cabinet.example.test')]
            if csrf: headers.append((b'x-csrf-token', csrf_token(self.actor.raw_session).encode()))
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1', 'method': method, 'scheme': 'https', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'headers': headers, 'client': ('test', 1), 'server': ('cabinet.example.test', 443)}
            messages = []; delivered = False
            async def receive():
                nonlocal delivered
                if delivered: await asyncio.Event().wait()
                delivered = True; return {'type': 'http.request', 'body': body, 'more_body': False}
            async def send(message): messages.append(message)
            await asyncio.wait_for(app(scope, receive, send), 5)
            return next(m['status'] for m in messages if m['type'] == 'http.response.start'), b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body').decode()
        return asyncio.run(run())

    def payload(self, **kwargs):
        return json.loads(json.dumps(self.data(**kwargs), default=str))

    def test_real_http_mount_validation_and_csrf_hide_passwords(self):
        status, body = self.request(self.payload(), csrf=False)
        self.assertEqual(status, 403); self.assertNotIn(PASSWORD, body); self.assertEqual(self.db.rows, {})
        status, body = self.request(self.payload(port=True))
        self.assertEqual(status, 422); self.assertNotIn(PASSWORD, body)
        status, body = self.request(self.payload())
        self.assertEqual(status, 200); self.assertNotIn(PASSWORD, body); self.assertNotIn('password_ciphertext', body)
        self.assertEqual(json.loads(body)['lines'][0]['sip_number'], '0001234')

    def test_real_http_staff_read_and_delete_cannot_access_other_employee_profiles(self):
        self.request(self.payload())
        status, body = self.request(self.payload(expected_version=1, member_id=self.other, sip_number='0005678'))
        self.assertEqual(status, 200); other_line = json.loads(body)['lines'][-1]['id']
        self.accounts.role = 'operator'
        status, body = self.request(method='GET')
        self.assertEqual(status, 403)
        status, body = self.request(method='GET', suffix='/mine')
        self.assertEqual(status, 200); self.assertEqual(len(json.loads(body)['lines']), 1)
        for excluded in ('0005678', str(self.other), '@example.test', PASSWORD, 'password_ciphertext'):
            self.assertNotIn(excluded, body)
        before = copy.deepcopy(self.db.rows)
        status, body = self.request({'expected_version': 2}, method='DELETE', suffix='/' + other_line)
        self.assertEqual(status, 403); self.assertEqual(self.db.rows, before)

    def test_real_http_delete_checks_csrf_and_version_before_removing_line(self):
        status, body = self.request(self.payload()); line = json.loads(body)['lines'][0]['id']
        before = copy.deepcopy(self.db.rows)
        for payload, csrf, expected in (({'expected_version': 1}, False, 403), ({'expected_version': 0}, True, 409), ({'expected_version': True}, True, 422)):
            status, body = self.request(payload, csrf, method='DELETE', suffix='/' + line)
            self.assertEqual(status, expected); self.assertEqual(self.db.rows, before)
        status, body = self.request({'expected_version': 1}, method='DELETE', suffix='/' + line)
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)['lines'], [])
        self.assertEqual(json.loads(body)['version'], 2); self.assertNotIn(PASSWORD, body)
