"""Employee SIP profiles. No registration, media, dial or provider API calls."""
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.auth_security import AuthError

TOPIC = 'novofon-sip-profiles-v1'
KEY = 'workspace'
MAX_LINES = 100


class SipSecret:
    """Use the connection master key with separate, member-bound AEAD data."""
    def __init__(self, vault):
        self.vault = vault

    @staticmethod
    def aad(workspace, line, member):
        return f'center-messenger:sip-line:v1:{workspace}:{line}:{member}'.encode()

    def seal(self, workspace, line, member, password):
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.vault._key()).encrypt(nonce, password.encode(), self.aad(workspace, line, member))
        return 'sl1.' + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def open(self, workspace, line, member, ciphertext):
        key = self.vault._key()
        try:
            if not isinstance(ciphertext, str) or not ciphertext.startswith('sl1.') or len(ciphertext) > 7000:
                raise ValueError
            raw = base64.b64decode(ciphertext[4:], altchars=b'-_', validate=True)
            password = AESGCM(key).decrypt(raw[:12], raw[12:], self.aad(workspace, line, member)).decode()
            if not 1 <= len(password) <= 1024:
                raise ValueError
            return password
        except (ValueError, TypeError, UnicodeError, InvalidTag):
            raise AuthError(503, 'Не удалось прочитать защищённый пароль SIP-линии.') from None


class SipLines:
    def __init__(self, accounts, vault):
        self.accounts, self.vault = accounts, vault
        self.secrets = SipSecret(vault)

    @staticmethod
    def _load(db, workspace, lock=False):
        if lock:
            # Also serializes the first insert when no settings row exists yet.
            key = int.from_bytes(hashlib.sha256(f'sip-profiles:{workspace}'.encode()).digest()[:8], 'big', signed=True)
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
        value = db.execute(text('SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND event_key=:k' + (' FOR UPDATE' if lock else '')),
                           {'w': workspace, 't': TOPIC, 'k': KEY}).scalar_one_or_none()
        if value is None:
            return {'version': 0, 'lines': []}
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] < 0
                or not isinstance(value.get('lines'), list) or len(value['lines']) > MAX_LINES):
            raise AuthError(503, 'Настройки SIP-линий требуют проверки администратора.')
        for line in value['lines']:
            try:
                if not isinstance(line, dict): raise ValueError
                UUID(line['id']); UUID(line['member_id'])
                if not all(isinstance(line[k], str) for k in ('sip_number', 'extension', 'server', 'password_ciphertext')): raise ValueError
                if type(line['enabled']) is not bool or type(line['primary']) is not bool or type(line['port']) is not int: raise ValueError
            except (KeyError, TypeError, ValueError):
                raise AuthError(503, 'Настройки SIP-линий требуют проверки администратора.') from None
        return value

    @staticmethod
    def _members(db, workspace, lock=False):
        rows = db.execute(text('SELECT m.user_id,u.display_name,m.role,m.sections FROM workspace_memberships m '
                               'JOIN auth_users u ON u.id=m.user_id WHERE m.workspace_id=:w AND m.active '
                               'ORDER BY u.display_name,m.user_id' + (' FOR SHARE OF m' if lock else '')),
                          {'w': workspace}).mappings().all()
        return {str(row['user_id']): dict(row) for row in rows}

    def _public(self, stored, members, *, own=None):
        lines = []
        for line in stored['lines']:
            if own is not None and line['member_id'] != str(own):
                continue
            member = members.get(line['member_id'])
            # Credentials never leave the server, including owner/admin responses.
            row = {k: line.get(k) for k in ('id', 'member_id', 'sip_number', 'extension', 'server', 'port', 'enabled', 'primary', 'updated_at')}
            row.update(member_name=member['display_name'] if member else 'Сотрудник отключён',
                       member_active=bool(member), password_configured=bool(line['password_ciphertext']),
                       registration_status='not_checked')
            lines.append(row)
        result = {'version': stored['version'], 'lines': lines, 'vault_ready': self.vault.ready, 'browser_call_ready': False}
        if own is None:
            result['members'] = [{'id': key, 'name': value['display_name']} for key, value in members.items()]
        return result

    def settings(self, actor, workspace):
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            return self._public(self._load(db, workspace), self._members(db, workspace))

    def mine(self, actor, workspace):
        with self.accounts.transaction(actor, snapshot=True) as db:
            member = self.accounts._workspace_chat(db, actor, workspace)
            stored = self._load(db, workspace)
            # A staff user receives only their own profiles, never another member's secret or metadata.
            members = {str(actor.user_id): {'display_name': 'Моя линия'}}
            result = self._public(stored, members, own=actor.user_id)
            result['may_call'] = member['role'] != 'observer'
            return result

    @staticmethod
    def _validate(data):
        sip = data['sip_number'].strip()
        extension = data.get('extension', '').strip()
        server = data.get('server', 'sip.novofon.ru').strip().lower().rstrip('.')
        password = data.get('password') or ''
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', sip):
            raise AuthError(422, 'Введите SIP-номер из настроек Новофона. Начальные нули сохраняются.')
        if extension and not re.fullmatch(r'[0-9]{1,12}', extension):
            raise AuthError(422, 'Внутренний номер должен содержать только цифры.')
        if server != 'sip.novofon.ru':
            raise AuthError(422, 'Для этого подключения используется SIP-сервер sip.novofon.ru.')
        if type(data['port']) is not int or not 1 <= data['port'] <= 65535:
            raise AuthError(422, 'Укажите корректный SIP-порт.')
        if password and (not 1 <= len(password) <= 1024 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in password)):
            raise AuthError(422, 'Введите SIP-пароль без пробелов и переводов строк.')
        return sip, extension, server, password

    @staticmethod
    def _primary(lines, member, preferred=None):
        group = [line for line in lines if line['member_id'] == member]
        chosen = next((line for line in group if line['id'] == preferred and line['enabled']), None)
        chosen = chosen or next((line for line in group if line['primary'] and line['enabled']), None)
        chosen = chosen or next((line for line in group if line['enabled']), None)
        for line in group:
            line['primary'] = line is chosen

    @staticmethod
    def _write(db, workspace, stored):
        stored = {'version': stored['version'] + 1, 'lines': stored['lines']}
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                        "VALUES(:w,:id,:t,:k,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
                        "DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
                   {'w': workspace, 'id': uuid4(), 't': TOPIC, 'k': KEY, 'p': json.dumps(stored)})
        return stored

    def save(self, actor, workspace, data):
        sip, extension, server, password = self._validate(data)
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            stored = self._load(db, workspace, lock=True)
            if stored['version'] != data['expected_version']:
                raise AuthError(409, 'SIP-линии изменились. Обновите список перед сохранением.')
            members = self._members(db, workspace, lock=True)
            member = str(data['member_id'])
            if member not in members:
                raise AuthError(422, 'Выберите действующего сотрудника этого центра.')
            lines = [dict(line) for line in stored['lines']]
            line_id = str(data['line_id']) if data.get('line_id') else str(uuid4())
            old = next((line for line in lines if line['id'] == line_id), None)
            if data.get('line_id') and old is None:
                raise AuthError(404, 'SIP-линия не найдена.')
            if old is None and len(lines) >= MAX_LINES:
                raise AuthError(422, 'Достигнут предел: 100 SIP-линий на центр.')
            if any(line['id'] != line_id and line['sip_number'] == sip and line['server'] == server for line in lines):
                raise AuthError(409, 'Этот SIP-номер уже добавлен. Отредактируйте существующую линию.')
            if sum(line['member_id'] == member and line['id'] != line_id for line in lines) >= 5:
                raise AuthError(422, 'Сотруднику можно назначить до пяти SIP-линий.')
            changed_identity = old is None or any(old[k] != value for k, value in [('member_id', member), ('sip_number', sip), ('server', server), ('port', data['port'])])
            if not password and changed_identity:
                raise AuthError(422, 'Введите SIP-пароль для новой линии или изменённых реквизитов.')
            cipher = self.secrets.seal(workspace, line_id, member, password) if password else old['password_ciphertext']
            row = {'id': line_id, 'member_id': member, 'sip_number': sip, 'extension': extension,
                   'server': server, 'port': data['port'], 'password_ciphertext': cipher,
                   'enabled': data['enabled'], 'primary': data['primary'], 'updated_at': datetime.now(timezone.utc).isoformat()}
            lines = [line for line in lines if line['id'] != line_id] + [row]
            if old and old['member_id'] != member:
                self._primary(lines, old['member_id'])
            self._primary(lines, member, line_id if row['primary'] else None)
            stored = self._write(db, workspace, stored | {'lines': lines})
            audit(db, 'sip_line_saved', user_id=actor.user_id, workspace_id=workspace, subject_id=UUID(line_id))
            return self._public(stored, members)

    def delete(self, actor, workspace, line_id, expected_version):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            stored = self._load(db, workspace, lock=True)
            if stored['version'] != expected_version:
                raise AuthError(409, 'SIP-линии изменились. Обновите список перед удалением.')
            old = next((line for line in stored['lines'] if line['id'] == str(line_id)), None)
            if old is None:
                raise AuthError(404, 'SIP-линия не найдена.')
            lines = [dict(line) for line in stored['lines'] if line['id'] != str(line_id)]
            self._primary(lines, old['member_id'])
            stored = self._write(db, workspace, stored | {'lines': lines})
            audit(db, 'sip_line_deleted', user_id=actor.user_id, workspace_id=workspace, subject_id=line_id)
            return self._public(stored, self._members(db, workspace))
