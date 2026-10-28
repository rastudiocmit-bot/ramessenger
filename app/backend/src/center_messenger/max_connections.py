"""Owner-controlled MAX bot setup; only our webhook URL may be changed or removed."""

import hashlib
import secrets
from uuid import uuid4

from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.auth_security import AuthError
from center_messenger.max_client import ERRORS, UPDATE_TYPES, MaxClient, MaxError


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MaxConnections:
    def __init__(self, alfa, client_factory=MaxClient):
        self.alfa, self.accounts, self.vault = alfa, alfa.accounts, alfa.vault
        self.client_factory = client_factory

    def transaction(self, actor, workspace, *, lock=False):
        return self.alfa._transaction(actor, workspace, lock=lock)

    def chat_transaction(self, actor, workspace, *, lock=False, snapshot=False):
        return self.alfa.chat_transaction(actor, workspace, lock=lock, snapshot=snapshot)

    @staticmethod
    def row(db, workspace, connection):
        row = (
            db.execute(
                text(
                    "SELECT s.*,c.name,c.status,(SELECT count(*) FROM inbound_events e "
                    "WHERE e.workspace_id=s.workspace_id AND e.connection_id=s.connection_id "
                    "AND e.state='failed') AS failed_events FROM max_settings s "
                    "JOIN connections c ON "
                    "c.workspace_id=s.workspace_id AND c.id=s.connection_id "
                    "WHERE s.workspace_id=:w AND s.connection_id=:c"
                ),
                {"w": workspace, "c": connection},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuthError(404, "Подключение MAX недоступно.")
        return row

    def hook_url(self, connection):
        return self.accounts.settings.public_url + f"/hooks/max/{connection}"

    def legacy_hook_urls(self, connection):
        return [origin + f"/hooks/max/{connection}" for origin in self.accounts.settings.legacy_origins]

    @staticmethod
    def public(row):
        return {
            "id": str(row["connection_id"]),
            "name": row["name"],
            "kind": "max_bot",
            "configured": row["secret_ciphertext"] is not None,
            "bot_name": row["bot_name"],
            "username": row["username"],
            "verified": row["bot_user_id"] is not None and row["checked_at"] is not None,
            "enabled": row["enabled"],
            "subscription_confirmed": row["subscription_confirmed"],
            "foreign_subscriptions": row["foreign_subscriptions"],
            "last_inbound_at": row["last_inbound_at"],
            "history_since": row["history_since"],
            "checked_at": row["checked_at"],
            "error": ERRORS.get(row["last_error_code"]),
            "can_send": row["enabled"]
            and row["subscription_confirmed"]
            and row["status"] == "active",
            "history_backfill": False,
            "send_attachments": False,
            "failed_events": row.get("failed_events", 0),
        }

    def listing(self, actor, workspace):
        with self.transaction(actor, workspace) as db:
            rows = db.execute(
                text(
                    "SELECT s.*,c.name,c.status,(SELECT count(*) FROM inbound_events e "
                    "WHERE e.workspace_id=s.workspace_id AND e.connection_id=s.connection_id "
                    "AND e.state='failed') AS failed_events FROM max_settings s JOIN connections c "
                    "ON c.workspace_id=s.workspace_id AND c.id=s.connection_id "
                    "WHERE s.workspace_id=:w ORDER BY c.created_at,c.id"
                ),
                {"w": workspace},
            ).mappings()
            return {"channels": [self.public(row) for row in rows], "vault_ready": self.vault.ready}

    def create(self, actor, workspace, model):
        self.accounts.throttle(f"max-create:{workspace}:{actor.user_id}", limit=8, minutes=1)
        connection, secret = uuid4(), secrets.token_urlsafe(32)
        token = model.token.get_secret_value()
        encrypted = self.vault.seal(
            workspace, connection, {"api_key": token, "hook_secret": secret}
        )
        with self.transaction(actor, workspace, lock=True) as db:
            count = db.execute(
                text("SELECT count(*) FROM max_settings WHERE workspace_id=:w"), {"w": workspace}
            ).scalar_one()
            if count >= 8:
                raise AuthError(409, "В первой версии доступно до восьми MAX-ботов на организацию.")
            duplicate = db.execute(
                text("SELECT connection_id FROM max_routes WHERE token_fingerprint=:h"),
                {"h": fingerprint(token)},
            ).scalar_one_or_none()
            if duplicate:
                raise AuthError(409, "Этот токен уже сохранён в подключении MAX.")
            db.execute(
                text(
                    "INSERT INTO connections(workspace_id,id,kind,name) VALUES (:w,:c,'max_bot',:n)"
                ),
                {"w": workspace, "c": connection, "n": model.name},
            )
            db.execute(
                text(
                    "INSERT INTO max_settings(workspace_id,connection_id,secret_ciphertext) "
                    "VALUES (:w,:c,:s)"
                ),
                {"w": workspace, "c": connection, "s": encrypted},
            )
            db.execute(
                text(
                    "INSERT INTO "
                    "max_routes(connection_id,workspace_id,token_fingerprint,secret_hash) "
                    "VALUES (:c,:w,:h,:s)"
                ),
                {
                    "c": connection,
                    "w": workspace,
                    "h": fingerprint(token),
                    "s": fingerprint(secret),
                },
            )
            audit(
                db,
                "max_token_saved",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )
        return str(connection)

    def snapshot(self, actor, workspace, connection):
        with self.transaction(actor, workspace) as db:
            row = dict(self.row(db, workspace, connection))
            if not row["secret_ciphertext"]:
                raise AuthError(409, "Сначала сохраните токен MAX-бота.")
            credentials = self.vault.open(workspace, connection, row["secret_ciphertext"])
        return row, credentials, self.client_factory(credentials["api_key"])

    @staticmethod
    def unchanged(current, snapshot):
        if current["version"] != snapshot["version"]:
            raise AuthError(409, "Подключение изменилось. Обновите страницу и повторите действие.")

    @staticmethod
    def subscriptions_ok(items, url):
        return any(
            s["url"] == url
            and (s.get("update_types") is None or set(UPDATE_TYPES).issubset(s["update_types"]))
            for s in items
        )

    @staticmethod
    def signature(items, ours):
        return {
            (s["url"], tuple(sorted(s.get("update_types") or [])))
            for s in items
            if s["url"] != ours
        }

    def _identity(self, db, workspace, connection, row, info, subscriptions):
        if row["bot_user_id"] is not None and row["bot_user_id"] != info["id"]:
            raise AuthError(409, "Это другой MAX-бот. Добавьте его отдельным подключением.")
        occupied = db.execute(
            text("SELECT connection_id FROM max_routes WHERE remote_bot_id=:bot"),
            {"bot": info["id"]},
        ).scalar_one_or_none()
        if occupied and occupied != connection:
            raise AuthError(409, "Этот MAX-бот уже связан с другим подключением.")
        ours = self.hook_url(connection)
        db.execute(
            text("UPDATE max_routes SET remote_bot_id=:bot WHERE connection_id=:c"),
            {"c": connection, "bot": info["id"]},
        )
        db.execute(
            text(
                "UPDATE max_settings SET bot_user_id=:bot,bot_name=:name,username=:user,"
                "checked_at=now(),foreign_subscriptions=:count,last_error_code=NULL,"
                "subscription_confirmed=:confirmed WHERE workspace_id=:w AND connection_id=:c"
            ),
            {
                "w": workspace,
                "c": connection,
                "bot": info["id"],
                "name": info["name"],
                "user": info["username"],
                "count": len([s for s in subscriptions if s["url"] != ours]),
                "confirmed": self.subscriptions_ok(subscriptions, ours),
            },
        )
        db.execute(
            text("UPDATE connections SET status='active' WHERE workspace_id=:w AND id=:c"),
            {"w": workspace, "c": connection},
        )

    def _failure(self, actor, workspace, connection, snapshot, error):
        with self.transaction(actor, workspace, lock=True) as db:
            row = self.row(db, workspace, connection)
            self.unchanged(row, snapshot)
            db.execute(
                text(
                    "UPDATE max_settings SET last_error_code=:code "
                    "WHERE workspace_id=:w AND connection_id=:c"
                ),
                {"w": workspace, "c": connection, "code": error.code},
            )
            db.execute(
                text("UPDATE connections SET status=:s WHERE workspace_id=:w AND id=:c"),
                {
                    "w": workspace,
                    "c": connection,
                    "s": "reauth_required" if error.code == "unauthorized" else "error",
                },
            )
        raise AuthError(502, str(error)) from None

    def check(self, actor, workspace, connection):
        self.accounts.throttle(f"max-check:{workspace}:{actor.user_id}", limit=15, minutes=1)
        snapshot, _, client = self.snapshot(actor, workspace, connection)
        try:
            info, subscriptions = client.me(), client.subscriptions()
        except MaxError as error:
            return self._failure(actor, workspace, connection, snapshot, error)
        with self.transaction(actor, workspace, lock=True) as db:
            row = self.row(db, workspace, connection)
            self.unchanged(row, snapshot)
            self._identity(db, workspace, connection, row, info, subscriptions)
            result = self.public(self.row(db, workspace, connection))
            audit(
                db,
                "max_access_checked",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )
        return result

    def enable(self, actor, workspace, connection):
        self.accounts.throttle(f"max-enable:{workspace}:{actor.user_id}", limit=5, minutes=1)
        if not self.accounts.settings.public_url.startswith("https://"):
            raise AuthError(409, "Для приёма MAX требуется HTTPS на порту 443.")
        snapshot, credentials, client = self.snapshot(actor, workspace, connection)
        ours = self.hook_url(connection)
        try:
            info, before = client.me(), client.subscriptions()
            with self.transaction(actor, workspace, lock=True) as db:
                current = self.row(db, workspace, connection)
                self.unchanged(current, snapshot)
                self._identity(db, workspace, connection, current, info, before)
                # Accept immediately: MAX may deliver before the verification GET finishes.
                db.execute(
                    text(
                        "UPDATE max_settings SET enabled=true,subscription_confirmed=false,"
                        "history_since=coalesce(history_since,now()) "
                        "WHERE workspace_id=:w AND connection_id=:c"
                    ),
                    {"w": workspace, "c": connection},
                )
                db.execute(
                    text("UPDATE max_routes SET accept_events=true WHERE connection_id=:c"),
                    {"c": connection},
                )
            client.subscribe(ours, credentials["hook_secret"])
            after = client.subscriptions()
            if not self.subscriptions_ok(after, ours) or not self.signature(before, ours).issubset(
                self.signature(after, ours)
            ):
                raise MaxError("subscription_changed")
            # After a domain move: this connection's webhook on the former domain is ours too.
            for old in self.legacy_hook_urls(connection):
                if any(s["url"] == old for s in after):
                    client.unsubscribe(old)
                    after = client.subscriptions()
                    if not self.subscriptions_ok(after, ours):
                        raise MaxError("subscription_changed")
        except MaxError as error:
            return self._failure(actor, workspace, connection, snapshot, error)
        with self.transaction(actor, workspace, lock=True) as db:
            row = self.row(db, workspace, connection)
            self.unchanged(row, snapshot)
            self._identity(db, workspace, connection, row, info, after)
            audit(
                db,
                "max_receiving_enabled",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )
            return self.public(self.row(db, workspace, connection))

    def disable(self, actor, workspace, connection):
        snapshot, _, client = self.snapshot(actor, workspace, connection)
        with self.transaction(actor, workspace, lock=True) as db:
            self.unchanged(self.row(db, workspace, connection), snapshot)
            db.execute(
                text(
                    "UPDATE max_settings SET enabled=false,subscription_confirmed=false,"
                    "version=version+1 WHERE workspace_id=:w AND connection_id=:c"
                ),
                {"w": workspace, "c": connection},
            )
            db.execute(
                text("UPDATE max_routes SET accept_events=false WHERE connection_id=:c"),
                {"c": connection},
            )
            audit(
                db,
                "max_receiving_disabled",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )
        # Explicit user action. DELETE is restricted to this connection's exact URL.
        try:
            ours = self.hook_url(connection)
            before = client.subscriptions()
            if any(s["url"] == ours for s in before):
                client.unsubscribe(ours)
                after = client.subscriptions()
                if not self.signature(before, ours).issubset(self.signature(after, ours)):
                    raise MaxError("subscription_changed")
        except MaxError as error:
            raise AuthError(502, "Приём в кабинете отключён. " + str(error)) from None

    def rotate(self, actor, workspace, connection, model):
        snapshot, credentials, _ = self.snapshot(actor, workspace, connection)
        token = model.token.get_secret_value()
        client = self.client_factory(token)
        try:
            info, subscriptions = client.me(), client.subscriptions()
        except MaxError as error:
            raise AuthError(502, str(error)) from None
        with self.transaction(actor, workspace, lock=True) as db:
            current = self.row(db, workspace, connection)
            self.unchanged(current, snapshot)
            self._identity(db, workspace, connection, current, info, subscriptions)
            credentials["api_key"] = token
            encrypted = self.vault.seal(workspace, connection, credentials)
            db.execute(
                text(
                    "UPDATE max_settings SET secret_ciphertext=:s,version=version+1 "
                    "WHERE workspace_id=:w AND connection_id=:c"
                ),
                {"w": workspace, "c": connection, "s": encrypted},
            )
            db.execute(
                text("UPDATE connections SET name=:n WHERE workspace_id=:w AND id=:c"),
                {"w": workspace, "c": connection, "n": model.name},
            )
            db.execute(
                text("UPDATE max_routes SET token_fingerprint=:h WHERE connection_id=:c"),
                {"c": connection, "h": fingerprint(token)},
            )
            audit(
                db,
                "max_token_rotated",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )
