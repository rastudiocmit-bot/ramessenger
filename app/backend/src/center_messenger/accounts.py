"""Trusted account operations; request-supplied workspace IDs never grant access."""

import hmac
import re
import secrets
from contextlib import contextmanager
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from center_messenger.auth_security import (
    AcceptInvitationInput,
    AuthError,
    BootstrapInput,
    Identity,
    InvitationInput,
    LoginInput,
    MEMBER_SECTIONS,
    MemberRoleInput,
    MemberSectionsInput,
    PasswordInput,
    Passwords,
    digest,
    new_token,
    valid_token,
)
from center_messenger.config import Settings

UNAUTHORIZED = "Неверная почта или пароль."
INVALID_INVITE = "Приглашение недействительно или срок его действия истёк."


def context(db, **values):
    # Keys are internal constants, values come from checked server operations.
    for key, value in values.items():
        db.execute(
            text("SELECT set_config(:key, :value, true)"),
            {"key": f"app.{key}", "value": str(value)},
        )


def audit(db, action, *, user_id=None, workspace_id=None, subject_id=None):
    db.execute(
        text(
            "INSERT INTO auth_events(id, user_id, workspace_id, action, subject_id) "
            "VALUES (:id, :user, :workspace, :action, :subject)"
        ),
        {
            "id": uuid4(),
            "user": user_id,
            "workspace": workspace_id,
            "action": action,
            "subject": subject_id,
        },
    )


class Accounts:
    def __init__(self, engine: Engine, settings: Settings, passwords: Passwords | None = None):
        self.engine = engine
        self.settings = settings
        self.passwords = passwords or Passwords()

    @contextmanager
    def transaction(self, identity: Identity | None = None, **scopes):
        with self.engine.begin() as db:
            if identity is not None:
                context(db, user_id=identity.user_id)
                valid = db.execute(
                    text(
                        "SELECT s.id FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id "
                        "WHERE s.id=:s AND s.user_id=:u AND s.revoked_at IS NULL "
                        "AND s.expires_at>now() AND u.active AND u.auth_epoch=:epoch "
                        "AND s.auth_epoch=u.auth_epoch AND s.last_seen_at > now() "
                        "- (:idle * interval '1 minute')"
                    ),
                    {
                        "s": identity.session_id,
                        "u": identity.user_id,
                        "epoch": identity.auth_epoch,
                        "idle": self.settings.session_idle_minutes,
                    },
                ).scalar_one_or_none()
                if valid is None:
                    raise AuthError(401, "Сессия завершена. Войдите снова.")
            context(db, **scopes)
            yield db

    def _setup_secret(self) -> str | None:
        path = self.settings.bootstrap_token_file
        try:
            if path is None or not path.is_file() or not 32 <= path.stat().st_size <= 256:
                return None
            value = path.read_text().strip()
        except (OSError, UnicodeError):
            return None
        return value if re.fullmatch(r"[A-Za-z0-9_-]{48,128}", value) else None

    def status(self) -> dict:
        with self.transaction() as db:
            claimed = db.execute(text("SELECT claimed_at FROM auth_bootstrap")).scalar_one()
        return {
            "setup_required": claimed is None,
            "setup_enabled": self._setup_secret() is not None,
        }

    def throttle(self, label: str, *, limit: int, minutes: int = 15):
        # Committed separately so failed credentials/transactions cannot reset the budget.
        with self.transaction() as db:
            attempts = db.execute(
                text(
                    "INSERT INTO auth_throttles(bucket_hash) VALUES (:key) "
                    "ON CONFLICT (bucket_hash) DO UPDATE SET "
                    "attempts = CASE WHEN auth_throttles.window_at < "
                    "now() - (:minutes * interval '1 minute') THEN 1 "
                    "ELSE auth_throttles.attempts + 1 END, "
                    "window_at = CASE WHEN auth_throttles.window_at < "
                    "now() - (:minutes * interval '1 minute') THEN now() "
                    "ELSE auth_throttles.window_at END RETURNING attempts"
                ),
                {"key": digest(label), "minutes": minutes},
            ).scalar_one()
            if secrets.randbelow(20) == 0:
                db.execute(
                    text(
                        "DELETE FROM auth_throttles WHERE bucket_hash IN "
                        "(SELECT bucket_hash FROM auth_throttles "
                        "WHERE window_at < now() - interval '1 day' LIMIT 100)"
                    )
                )
        if attempts > limit:
            raise AuthError(
                429, "Слишком много попыток. Повторите позже.", retry_after=minutes * 60
            )

    def _issue(self, db, user, device: str) -> str:
        raw = new_token()
        context(db, user_id=user["id"])
        db.execute(
            text(
                "UPDATE auth_sessions SET revoked_at = now() WHERE id IN "
                "(SELECT id FROM auth_sessions WHERE user_id = :user "
                "AND revoked_at IS NULL AND expires_at > now() "
                "ORDER BY last_seen_at DESC, created_at DESC OFFSET 19)"
            ),
            {"user": user["id"]},
        )
        db.execute(
            text(
                "INSERT INTO auth_sessions(id, user_id, token_hash, auth_epoch, "
                "device_label, expires_at) VALUES (:id, :user, :token, :epoch, :device, "
                "now() + (:hours * interval '1 hour'))"
            ),
            {
                "id": uuid4(),
                "user": user["id"],
                "token": digest(raw),
                "epoch": user["auth_epoch"],
                "device": device[:200],
                "hours": self.settings.session_hours,
            },
        )
        return raw

    def bootstrap(self, model: BootstrapInput, ip: str, device: str) -> str:
        self.throttle(f"setup-ip:{ip}", limit=10)
        secret = self._setup_secret()
        if secret is None:
            raise AuthError(
                503, "Первичная настройка пока недоступна. Обратитесь к владельцу сервиса."
            )
        if not hmac.compare_digest(digest(secret), digest(model.setup_code.get_secret_value())):
            raise AuthError(403, "Неверный код первой настройки.")
        if not self.status()["setup_required"]:
            raise AuthError(409, "Первый аккаунт уже создан. Войдите с почтой и паролем.")
        encoded = self.passwords.hash(model.password.get_secret_value())
        user_id, workspace_id = uuid4(), uuid4()
        with self.transaction(user_id=user_id, workspace_id=workspace_id) as db:
            claimed = db.execute(
                text(
                    "UPDATE auth_bootstrap SET claimed_at = now() "
                    "WHERE singleton AND claimed_at IS NULL RETURNING singleton"
                )
            ).scalar_one_or_none()
            if claimed is None:
                raise AuthError(409, "Первый аккаунт уже создан. Войдите с почтой и паролем.")
            user = (
                db.execute(
                    text(
                        "INSERT INTO auth_users(id, email, display_name, password_hash) "
                        "VALUES (:id, :email, :name, :hash) RETURNING id, auth_epoch"
                    ),
                    {
                        "id": user_id,
                        "email": str(model.email).casefold(),
                        "name": model.display_name,
                        "hash": encoded,
                    },
                )
                .mappings()
                .one()
            )
            db.execute(
                text("INSERT INTO workspaces(id, name) VALUES (:id, :name)"),
                {"id": workspace_id, "name": model.workspace_name},
            )
            db.execute(
                text(
                    "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                    "VALUES (:workspace, :user, 'owner')"
                ),
                {"workspace": workspace_id, "user": user_id},
            )
            audit(db, "bootstrap_complete", user_id=user_id, workspace_id=workspace_id)
            return self._issue(db, user, device)

    def login(self, model: LoginInput, ip: str, device: str) -> str:
        email = str(model.email).casefold()
        self.throttle(f"login-ip:{ip}", limit=60)
        self.throttle(f"login-email:{email}", limit=10)
        with self.transaction(login_email=email) as db:
            user = (
                db.execute(
                    text(
                        "SELECT id, password_hash, auth_epoch, active "
                        "FROM auth_users WHERE email=:e"
                    ),
                    {"e": email},
                )
                .mappings()
                .one_or_none()
            )
        encoded = user["password_hash"] if user is not None else self.passwords.dummy
        valid = self.passwords.verify(encoded, model.password.get_secret_value())
        if not valid or user is None or not user["active"]:
            with self.transaction() as db:
                audit(db, "login_failed")
            raise AuthError(401, UNAUTHORIZED)
        with self.transaction(user_id=user["id"]) as db:
            current = (
                db.execute(
                    text("SELECT id, auth_epoch, active FROM auth_users WHERE id=:u FOR UPDATE"),
                    {"u": user["id"]},
                )
                .mappings()
                .one()
            )
            if not current["active"] or current["auth_epoch"] != user["auth_epoch"]:
                raise AuthError(401, UNAUTHORIZED)
            raw = self._issue(db, current, device)
            audit(db, "login_success", user_id=user["id"])
            return raw

    def authenticate(self, raw: str | None) -> Identity:
        if raw is None or not valid_token(raw):
            raise AuthError(401, "Войдите в личный кабинет.")
        with self.transaction(session_hash=digest(raw)) as db:
            session = (
                db.execute(
                    text(
                        "SELECT id, user_id, auth_epoch FROM auth_sessions WHERE token_hash=:t "
                        "AND revoked_at IS NULL AND expires_at > now() "
                        "AND last_seen_at > now() - (:idle * interval '1 minute')"
                    ),
                    {"t": digest(raw), "idle": self.settings.session_idle_minutes},
                )
                .mappings()
                .one_or_none()
            )
            if session is None:
                raise AuthError(401, "Сессия завершена. Войдите снова.")
            context(db, user_id=session["user_id"])
            user = (
                db.execute(
                    text(
                        "SELECT id, email, display_name, email_verified_at FROM auth_users "
                        "WHERE id=:id AND active AND auth_epoch=:epoch"
                    ),
                    {"id": session["user_id"], "epoch": session["auth_epoch"]},
                )
                .mappings()
                .one_or_none()
            )
            if user is None:
                raise AuthError(401, "Сессия завершена. Войдите снова.")
            db.execute(
                text(
                    "UPDATE auth_sessions SET last_seen_at=now() "
                    "WHERE id=:id AND last_seen_at < now() - interval '1 minute'"
                ),
                {"id": session["id"]},
            )
            return Identity(
                user_id=user["id"],
                session_id=session["id"],
                email=user["email"],
                display_name=user["display_name"],
                email_verified=user["email_verified_at"] is not None,
                auth_epoch=session["auth_epoch"],
                raw_session=raw,
            )

    def workspaces(self, identity: Identity) -> list[dict]:
        with self.transaction(identity) as db:
            rows = (
                db.execute(
                    text(
                        "SELECT w.id, w.name, w.timezone, m.role, m.sections FROM workspace_memberships m "
                        "JOIN workspaces w ON w.id=m.workspace_id "
                        "WHERE m.user_id=:u AND m.active ORDER BY w.name, w.id"
                    ),
                    {"u": identity.user_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def _workspace(self, db, identity: Identity, workspace_id: UUID, *, manage=False):
        row = (
            db.execute(
                text(
                    "SELECT w.id, w.name, w.timezone, m.role, m.sections FROM workspace_memberships m "
                    "JOIN workspaces w ON w.id=m.workspace_id "
                    "WHERE m.user_id=:u AND m.workspace_id=:w AND m.active"
                ),
                {"u": identity.user_id, "w": workspace_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuthError(404, "Организация недоступна.")
        if manage and row["role"] not in {"owner", "admin"}:
            raise AuthError(403, "Для этого действия нужны права администратора.")
        context(
            db,
            workspace_id=workspace_id,
            membership_admin="true" if row["role"] in {"owner", "admin"} else "false",
        )
        return row

    def _workspace_chat(self, db, identity: Identity, workspace_id: UUID):
        row = (
            db.execute(
                text(
                    "SELECT w.id, w.name, w.timezone, m.role, m.sections FROM workspace_memberships m "
                    "JOIN workspaces w ON w.id=m.workspace_id "
                    "WHERE m.user_id=:u AND m.workspace_id=:w AND m.active"
                ),
                {"u": identity.user_id, "w": workspace_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuthError(404, "Организация недоступна.")
        if row["role"] not in {"owner", "admin"} and "messages" not in (row["sections"] or []):
            raise AuthError(403, "Для этого действия нужен доступ к разделу «Сообщения».")
        context(
            db,
            workspace_id=workspace_id,
            membership_admin="true" if row["role"] in {"owner", "admin"} else "false",
        )
        return row

    def workspace(self, identity: Identity, workspace_id: UUID) -> dict:
        with self.transaction(identity) as db:
            return dict(self._workspace(db, identity, workspace_id))

    def rename_workspace(self, identity: Identity, workspace_id: UUID, name: str):
        with self.transaction(identity) as db:
            self._workspace(db, identity, workspace_id, manage=True)
            db.execute(
                text("UPDATE workspaces SET name=:n WHERE id=:w"), {"n": name, "w": workspace_id}
            )
            audit(db, "workspace_updated", user_id=identity.user_id, workspace_id=workspace_id)

    def sessions(self, identity: Identity) -> list[dict]:
        with self.transaction(identity) as db:
            rows = (
                db.execute(
                    text(
                        "SELECT id, device_label, created_at, last_seen_at, expires_at "
                        "FROM auth_sessions WHERE user_id=:u AND revoked_at IS NULL "
                        "AND expires_at > now() ORDER BY last_seen_at DESC"
                    ),
                    {"u": identity.user_id},
                )
                .mappings()
                .all()
            )
        return [dict(row, current=row["id"] == identity.session_id) for row in rows]

    def revoke(self, identity: Identity, session_id: UUID | None = None):
        with self.transaction(identity) as db:
            params = {"u": identity.user_id}
            statement = "UPDATE auth_sessions SET revoked_at=now() WHERE user_id=:u"
            if session_id is not None:
                statement += " AND id=:id"
                params["id"] = session_id
            changed = db.execute(text(statement + " RETURNING id"), params).fetchall()
            if session_id is not None and not changed:
                raise AuthError(404, "Сессия недоступна.")
            audit(
                db,
                "session_revoked" if session_id is not None else "logout_all",
                user_id=identity.user_id,
                subject_id=session_id,
            )

    def change_password(self, identity: Identity, model: PasswordInput):
        self.throttle(f"password:{identity.user_id}", limit=5)
        with self.transaction(identity) as db:
            old = db.execute(
                text("SELECT password_hash FROM auth_users WHERE id=:u"),
                {"u": identity.user_id},
            ).scalar_one()
        if not self.passwords.verify(old, model.current_password.get_secret_value()):
            raise AuthError(403, "Текущий пароль указан неверно.")
        encoded = self.passwords.hash(model.new_password.get_secret_value())
        with self.transaction(identity) as db:
            changed = db.execute(
                text(
                    "UPDATE auth_users SET password_hash=:new, auth_epoch=auth_epoch+1 "
                    "WHERE id=:u AND password_hash=:old AND auth_epoch=:epoch RETURNING id"
                ),
                {"new": encoded, "old": old, "u": identity.user_id, "epoch": identity.auth_epoch},
            ).scalar_one_or_none()
            if changed is None:
                raise AuthError(409, "Данные доступа изменились. Войдите снова.")
            db.execute(
                text("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=:u"),
                {"u": identity.user_id},
            )
            audit(db, "password_changed", user_id=identity.user_id)

    def members(self, identity: Identity, workspace_id: UUID) -> list[dict]:
        with self.transaction(identity) as db:
            self._workspace(db, identity, workspace_id, manage=True)
            # RLS grants directory reads only inside this already-authorized workspace.
            rows = (
                db.execute(
                    text(
                        "SELECT m.user_id, m.role, m.sections, m.created_at, u.email, u.display_name "
                        "FROM workspace_memberships m JOIN auth_users u ON u.id=m.user_id "
                        "WHERE m.workspace_id=:w AND m.active ORDER BY m.created_at, m.user_id"
                    ),
                    {"w": workspace_id},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def invite(self, identity: Identity, workspace_id: UUID, model: InvitationInput) -> dict:
        self.throttle(f"invite:{identity.user_id}", limit=30, minutes=60)
        email, raw, invitation_id = str(model.email).casefold(), new_token(), uuid4()
        with self.transaction(identity) as db:
            workspace = self._workspace(db, identity, workspace_id, manage=True)
            if model.role == "admin" and workspace["role"] != "owner":
                raise AuthError(403, "Пригласить администратора может владелец.")
            db.execute(
                text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace_id}
            )
            db.execute(
                text(
                    "UPDATE auth_invitations SET revoked_at=now() WHERE workspace_id=:w "
                    "AND email=:e AND used_at IS NULL AND revoked_at IS NULL"
                ),
                {"w": workspace_id, "e": email},
            )
            db.execute(
                text(
                    "INSERT INTO auth_invitations(id, workspace_id, email, role, token_hash, "
                    "issued_by, expires_at) VALUES (:id, :w, :e, :r, :t, :by, "
                    "now() + interval '48 hours')"
                ),
                {
                    "id": invitation_id,
                    "w": workspace_id,
                    "e": email,
                    "r": model.role,
                    "t": digest(raw),
                    "by": identity.user_id,
                },
            )
            audit(
                db,
                "invite_created",
                user_id=identity.user_id,
                workspace_id=workspace_id,
                subject_id=invitation_id,
            )
        # Fragment is never sent to the web server or included in HTTP access logs.
        return {"url": f"{self.settings.public_url}/#invite={raw}", "expires_in_hours": 48}

    def _invitation(self, db, raw: str, *, lock=False):
        if not valid_token(raw):
            raise AuthError(404, INVALID_INVITE)
        context(db, invite_hash=digest(raw))
        row = (
            db.execute(
                text(
                    "SELECT id, workspace_id, email, role, issued_by FROM auth_invitations "
                    "WHERE token_hash=:t AND used_at IS NULL AND revoked_at IS NULL "
                    "AND expires_at > now()" + (" FOR UPDATE" if lock else "")
                ),
                {"t": digest(raw)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuthError(404, INVALID_INVITE)
        return row

    def preview_invitation(self, raw: str, ip: str) -> dict:
        self.throttle(f"invite-preview:{ip}", limit=60)
        with self.transaction() as db:
            row = self._invitation(db, raw)
            return {"email": row["email"], "role": row["role"]}

    def accept_invitation(
        self, model: AcceptInvitationInput, identity: Identity | None, ip: str, device: str
    ) -> str:
        self.throttle(f"invite-accept:{ip}", limit=20)
        raw = model.token.get_secret_value()
        with self.transaction() as db:
            invitation = self._invitation(db, raw)
            context(db, login_email=invitation["email"])
            existing = db.execute(
                text("SELECT id FROM auth_users WHERE email=:e"), {"e": invitation["email"]}
            ).scalar_one_or_none()
        if existing is not None and (identity is None or identity.user_id != existing):
            raise AuthError(409, "Войдите с почтой приглашения, затем примите его.")
        if identity is not None and identity.email != invitation["email"]:
            raise AuthError(409, "Выйдите из текущего аккаунта и продолжите по приглашению.")
        if existing is None and (not model.display_name.strip() or model.password is None):
            raise AuthError(422, "Укажите имя и пароль длиной от 15 символов.")
        encoded = (
            self.passwords.hash(model.password.get_secret_value()) if existing is None else None
        )
        user_id = existing or uuid4()
        with self.transaction(identity, user_id=user_id) as db:
            current = self._invitation(db, raw, lock=True)
            if current["email"] != invitation["email"]:
                raise AuthError(404, INVALID_INVITE)
            # A removed/demoted inviter cannot grant access using an old link.
            issuer = db.execute(
                text(
                    "SELECT role FROM workspace_memberships "
                    "WHERE user_id=:u AND workspace_id=:w AND active"
                ),
                {"u": current["issued_by"], "w": current["workspace_id"]},
            ).scalar_one_or_none()
            if issuer not in {"owner", "admin"} or (
                current["role"] == "admin" and issuer != "owner"
            ):
                raise AuthError(404, INVALID_INVITE)
            context(db, login_email=current["email"])
            if existing is None:
                user = (
                    db.execute(
                        text(
                            "INSERT INTO auth_users(id, email, display_name, password_hash) "
                            "VALUES (:id, :e, :n, :p) RETURNING id, auth_epoch"
                        ),
                        {
                            "id": user_id,
                            "e": current["email"],
                            "n": model.display_name.strip(),
                            "p": encoded,
                        },
                    )
                    .mappings()
                    .one()
                )
            else:
                user = (
                    db.execute(
                        text(
                            "SELECT id, auth_epoch FROM auth_users "
                            "WHERE id=:u AND active FOR UPDATE"
                        ),
                        {"u": user_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if user is None or user["auth_epoch"] != identity.auth_epoch:
                    raise AuthError(401, "Войдите снова.")
            db.execute(
                text(
                    "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                    "VALUES (:w, :u, :r) ON CONFLICT (workspace_id, user_id) DO UPDATE "
                    "SET active=true, role=EXCLUDED.role "
                    "WHERE NOT workspace_memberships.active "
                    "AND workspace_memberships.role <> 'owner'"
                ),
                {"w": current["workspace_id"], "u": user_id, "r": current["role"]},
            )
            # An invitation must never elevate an already participating user's role.
            db.execute(
                text("UPDATE auth_invitations SET used_at=now() WHERE id=:id"),
                {"id": current["id"]},
            )
            audit(
                db,
                "invite_accepted",
                user_id=user_id,
                workspace_id=current["workspace_id"],
                subject_id=current["id"],
            )
            return self._issue(db, user, device)

    def set_member_role(self, identity: Identity, workspace_id: UUID, user_id: UUID, model: MemberRoleInput):
        with self.transaction(identity) as db:
            actor = self._workspace(db, identity, workspace_id, manage=True)
            target = db.execute(
                text(
                    "SELECT role FROM workspace_memberships "
                    "WHERE workspace_id=:w AND user_id=:u AND active FOR UPDATE"
                ),
                {"w": workspace_id, "u": user_id},
            ).scalar_one_or_none()
            if target is None:
                raise AuthError(404, "Сотрудник недоступен.")
            if target == "owner" or model.role == "owner":
                raise AuthError(403, "Роль владельца не изменяется.")
            if target == "admin" and actor["role"] != "owner":
                raise AuthError(403, "Роль администратора может менять только владелец.")
            if model.role == "admin" and actor["role"] != "owner":
                raise AuthError(403, "Назначить администратора может только владелец.")
            db.execute(
                text(
                    "UPDATE workspace_memberships SET role=:r "
                    "WHERE workspace_id=:w AND user_id=:u AND active"
                ),
                {"r": model.role, "w": workspace_id, "u": user_id},
            )
            audit(
                db,
                "member_role_updated",
                user_id=identity.user_id,
                workspace_id=workspace_id,
                subject_id=user_id,
            )

    def set_member_sections(
        self, identity: Identity, workspace_id: UUID, user_id: UUID, model: MemberSectionsInput
    ):
        with self.transaction(identity) as db:
            self._workspace(db, identity, workspace_id, manage=True)
            target = db.execute(
                text(
                    "SELECT role FROM workspace_memberships "
                    "WHERE workspace_id=:w AND user_id=:u AND active FOR UPDATE"
                ),
                {"w": workspace_id, "u": user_id},
            ).scalar_one_or_none()
            if target is None:
                raise AuthError(404, "Сотрудник недоступен.")
            if target in {"owner", "admin"}:
                raise AuthError(403, "Права этого сотрудника не настраиваются.")
            db.execute(
                text(
                    "UPDATE workspace_memberships SET sections=:s "
                    "WHERE workspace_id=:w AND user_id=:u AND active"
                ),
                {"s": model.sections, "w": workspace_id, "u": user_id},
            )
            audit(
                db,
                "member_sections_updated",
                user_id=identity.user_id,
                workspace_id=workspace_id,
                subject_id=user_id,
            )

    def remove_member(self, identity: Identity, workspace_id: UUID, user_id: UUID):
        with self.transaction(identity) as db:
            actor = self._workspace(db, identity, workspace_id, manage=True)
            target = db.execute(
                text(
                    "SELECT role FROM workspace_memberships "
                    "WHERE workspace_id=:w AND user_id=:u AND active FOR UPDATE"
                ),
                {"w": workspace_id, "u": user_id},
            ).scalar_one_or_none()
            if target is None:
                raise AuthError(404, "Сотрудник недоступен.")
            if target == "owner" or (target == "admin" and actor["role"] != "owner"):
                raise AuthError(403, "Недостаточно прав для отзыва этого доступа.")
            db.execute(
                text(
                    "UPDATE workspace_memberships SET active=false "
                    "WHERE workspace_id=:w AND user_id=:u"
                ),
                {"w": workspace_id, "u": user_id},
            )
            db.execute(
                text(
                    "UPDATE auth_invitations SET revoked_at=now() "
                    "WHERE workspace_id=:w AND issued_by=:u AND used_at IS NULL"
                ),
                {"w": workspace_id, "u": user_id},
            )
            audit(
                db,
                "member_removed",
                user_id=identity.user_id,
                workspace_id=workspace_id,
                subject_id=user_id,
            )
