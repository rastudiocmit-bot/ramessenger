"""Authorised connection setup and complete, cached branch discovery."""

import time
from contextlib import contextmanager
from uuid import UUID, uuid4

from sqlalchemy import text

from center_messenger.accounts import Accounts, audit
from center_messenger.alfa_client import ERRORS, AlfaClient, AlfaError, AlfaSettingsInput
from center_messenger.auth_security import AuthError, digest
from center_messenger.connector_vault import ConnectorVault


class AlfaConnections:
    def __init__(self, accounts: Accounts, vault: ConnectorVault, client_factory=AlfaClient):
        self.accounts, self.vault, self.client_factory = accounts, vault, client_factory

    @contextmanager
    def _transaction(self, actor, workspace, *, lock=False):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            if lock:
                db.execute(
                    text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace}
                )
            yield db

    @staticmethod
    def _row(db, workspace):
        return (
            db.execute(
                text(
                    "SELECT a.*,c.status FROM alfa_settings a JOIN connections c "
                    "ON c.workspace_id=a.workspace_id AND c.id=a.connection_id "
                    "WHERE a.workspace_id=:w"
                ),
                {"w": workspace},
            )
            .mappings()
            .one_or_none()
        )

    def status(self, actor, workspace):
        with self._transaction(actor, workspace) as db:
            row = self._row(db, workspace)
            result = {
                "vault_ready": self.vault.ready,
                "configured": False,
                "origin": "",
                "email": "",
                "seed_branch_id": None,
                "status": "not_configured",
                "last_checked_at": None,
                "last_success_at": None,
                "error": None,
                "branches": [],
            }
            if row is None:
                return result
            branches = (
                db.execute(
                    text(
                        "SELECT external_id,name FROM alfa_branches WHERE workspace_id=:w "
                        "AND connection_id=:c AND available ORDER BY lower(name),external_id"
                    ),
                    {"w": workspace, "c": row["connection_id"]},
                )
                .mappings()
                .all()
            )
            result.update(
                configured=row["secret_ciphertext"] is not None,
                origin=row["origin"],
                email=row["api_email"],
                seed_branch_id=row["seed_branch_id"],
                status=row["status"],
                last_checked_at=row["last_checked_at"],
                last_success_at=row["last_success_at"],
                error=ERRORS.get(row["last_error_code"]),
                branches=[{"id": str(b["external_id"]), "name": b["name"]} for b in branches],
            )
            return result

    def save(self, actor, workspace: UUID, model: AlfaSettingsInput):
        with self._transaction(actor, workspace, lock=True) as db:
            old = self._row(db, workspace)
            email = str(model.email).casefold()
            same_origin = old is not None and old["origin"] == model.origin
            same_user = same_origin and old["api_email"] == email
            if model.api_key is None and (not same_user or old["secret_ciphertext"] is None):
                raise AuthError(422, "Введите ключ API (v2api) для этого подключения.")
            connection = old["connection_id"] if same_origin else uuid4()
            changed = (
                not same_user
                or model.api_key is not None
                or old["seed_branch_id"] != model.seed_branch_id
            )
            credentials = (
                {"api_key": model.api_key.get_secret_value()}
                if model.api_key is not None
                else self.vault.open(workspace, connection, old["secret_ciphertext"])
            )
            if changed:
                credentials = {"api_key": credentials["api_key"]}
            encrypted = self.vault.seal(workspace, connection, credentials)
            if not same_origin:
                if old is not None:
                    db.execute(
                        text(
                            "UPDATE connections SET status='not_configured' "
                            "WHERE workspace_id=:w AND id=:c"
                        ),
                        {"w": workspace, "c": old["connection_id"]},
                    )
                db.execute(
                    text(
                        "INSERT INTO connections(workspace_id,id,kind,name) "
                        "VALUES (:w,:c,'alfa_crm','АльфаCRM')"
                    ),
                    {"w": workspace, "c": connection},
                )
            db.execute(
                text(
                    "INSERT INTO alfa_settings(workspace_id,connection_id,origin,api_email,"
                    "secret_ciphertext,seed_branch_id) VALUES (:w,:c,:o,:e,:s,:b) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET connection_id=EXCLUDED.connection_id,"
                    "origin=EXCLUDED.origin,api_email=EXCLUDED.api_email,"
                    "secret_ciphertext=EXCLUDED.secret_ciphertext,seed_branch_id=EXCLUDED.seed_branch_id,"
                    "version=alfa_settings.version+1,updated_at=now(),last_error_code=NULL,"
                    "last_success_at=CASE WHEN :changed THEN NULL "
                    "ELSE alfa_settings.last_success_at END,"
                    "last_checked_at=CASE WHEN :changed THEN NULL "
                    "ELSE alfa_settings.last_checked_at END"
                ),
                {
                    "w": workspace,
                    "c": connection,
                    "o": model.origin,
                    "e": email,
                    "s": encrypted,
                    "b": model.seed_branch_id,
                    "changed": changed,
                },
            )
            if changed:
                db.execute(
                    text("DELETE FROM alfa_family_details WHERE workspace_id=:w"),
                    {"w": workspace},
                )
                db.execute(
                    text("UPDATE alfa_branches SET available=false WHERE workspace_id=:w"),
                    {"w": workspace},
                )
                db.execute(
                    text(
                        "UPDATE connections SET status='not_configured' "
                        "WHERE workspace_id=:w AND id=:c"
                    ),
                    {"w": workspace, "c": connection},
                )
            self._pause_sync(db, workspace)
            audit(
                db,
                "alfa_configured",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=connection,
            )

    @contextmanager
    def _host_gate(self, origin):
        host_hash, owner = digest("alfa:" + origin), uuid4()
        with self.accounts.transaction() as db:
            acquired = db.execute(
                text(
                    "INSERT INTO alfa_request_leases(host_hash,lease_owner,lease_until) "
                    "VALUES (:h,:o,now()+interval '45 seconds') ON CONFLICT(host_hash) DO UPDATE "
                    "SET lease_owner=EXCLUDED.lease_owner,lease_until=EXCLUDED.lease_until "
                    "WHERE alfa_request_leases.lease_until < now() RETURNING lease_owner"
                ),
                {"h": host_hash, "o": owner},
            ).scalar_one_or_none()
        if acquired is None:
            raise AuthError(409, "Проверка этой CRM уже выполняется. Повторите немного позже.")
        deadline = time.monotonic() + 22

        def gate():
            while True:
                if time.monotonic() >= deadline:
                    raise AlfaError("timeout")
                with self.accounts.transaction() as db:
                    wait = db.execute(
                        text(
                            "SELECT EXTRACT(EPOCH FROM next_request_at-clock_timestamp()) "
                            "FROM alfa_request_leases WHERE host_hash=:h AND lease_owner=:o "
                            "AND lease_until>clock_timestamp() FOR UPDATE"
                        ),
                        {"h": host_hash, "o": owner},
                    ).scalar_one_or_none()
                    if wait is None:
                        raise AlfaError("timeout")
                    if wait <= 0:
                        db.execute(
                            text(
                                "UPDATE alfa_request_leases SET "
                                "next_request_at=clock_timestamp()+interval '350 milliseconds' "
                                "WHERE host_hash=:h AND lease_owner=:o"
                            ),
                            {"h": host_hash, "o": owner},
                        )
                        return
                time.sleep(min(float(wait), 0.35))

        try:
            yield gate
        finally:
            with self.accounts.transaction() as db:
                db.execute(
                    text(
                        "UPDATE alfa_request_leases SET lease_until=now() "
                        "WHERE host_hash=:h AND lease_owner=:o"
                    ),
                    {"h": host_hash, "o": owner},
                )

    def check(self, actor, workspace):
        # No open database transaction is held during any external HTTP request.
        with self._transaction(actor, workspace) as db:
            row = self._row(db, workspace)
            if row is None or row["secret_ciphertext"] is None:
                raise AuthError(409, "Сначала сохраните подключение АльфаCRM.")
            snapshot = dict(row)
        self.accounts.throttle(f"alfa-check:{workspace}:{actor.user_id}", limit=5, minutes=1)
        error, branches = None, None
        with self._host_gate(snapshot["origin"]) as gate:
            # Read again after acquiring the cross-process lease: another check may have
            # refreshed the cached token since the first read.
            with self._transaction(actor, workspace) as db:
                current = self._row(db, workspace)
                if current is None or current["version"] != snapshot["version"]:
                    raise AuthError(409, "Настройки изменились. Повторите проверку.")
                snapshot = dict(current)
            credentials = self.vault.open(
                workspace, snapshot["connection_id"], snapshot["secret_ciphertext"]
            )
            client = self.client_factory(
                snapshot["origin"], snapshot["api_email"], credentials, gate
            )
            try:
                branches = client.branches(snapshot["seed_branch_id"])
                if not branches:
                    raise AlfaError("forbidden")
            except AlfaError as failure:
                error = failure
            with self._transaction(actor, workspace, lock=True) as db:
                current = self._row(db, workspace)
                if current is None or current["version"] != snapshot["version"]:
                    raise AuthError(409, "Настройки изменились. Повторите проверку.")
                state = (
                    "active"
                    if error is None
                    else "reauth_required"
                    if error.code in {"unauthorized", "forbidden"}
                    else "error"
                )
                encrypted = self.vault.seal(workspace, snapshot["connection_id"], credentials)
                db.execute(
                    text(
                        "UPDATE alfa_settings SET last_checked_at=now(),"
                        "last_success_at=CASE WHEN :ok THEN now() ELSE last_success_at END,"
                        "last_error_code=:error,secret_ciphertext=:s WHERE workspace_id=:w"
                    ),
                    {
                        "w": workspace,
                        "s": encrypted,
                        "ok": error is None,
                        "error": error.code if error else None,
                    },
                )
                db.execute(
                    text("UPDATE connections SET status=:state WHERE workspace_id=:w AND id=:c"),
                    {"w": workspace, "c": snapshot["connection_id"], "state": state},
                )
                if error is None:
                    db.execute(
                        text(
                            "UPDATE alfa_branches SET available=false "
                            "WHERE workspace_id=:w AND connection_id=:c"
                        ),
                        {"w": workspace, "c": snapshot["connection_id"]},
                    )
                    for branch in branches:
                        db.execute(
                            text(
                                "INSERT INTO alfa_branches "
                                "(workspace_id,connection_id,external_id,name) "
                                "VALUES (:w,:c,:id,:name) "
                                "ON CONFLICT(workspace_id,connection_id,external_id) "
                                "DO UPDATE SET name=EXCLUDED.name,available=true,last_seen_at=now()"
                            ),
                            {"w": workspace, "c": snapshot["connection_id"], **branch},
                        )
                audit(
                    db,
                    "alfa_check_failed" if error else "alfa_checked",
                    user_id=actor.user_id,
                    workspace_id=workspace,
                    subject_id=snapshot["connection_id"],
                )
        if error:
            raise AuthError(502, str(error))
        return self.status(actor, workspace)

    def disconnect(self, actor, workspace):
        with self._transaction(actor, workspace, lock=True) as db:
            row = self._row(db, workspace)
            if row is None:
                return
            db.execute(
                text("DELETE FROM alfa_family_details WHERE workspace_id=:w"),
                {"w": workspace},
            )
            db.execute(
                text(
                    "UPDATE alfa_settings SET secret_ciphertext=NULL,version=version+1,"
                    "last_error_code=NULL,last_success_at=NULL,last_checked_at=NULL,"
                    "updated_at=now() WHERE workspace_id=:w"
                ),
                {"w": workspace},
            )
            db.execute(
                text(
                    "UPDATE connections SET status='not_configured' WHERE workspace_id=:w AND id=:c"
                ),
                {"w": workspace, "c": row["connection_id"]},
            )
            db.execute(
                text("UPDATE alfa_branches SET available=false WHERE workspace_id=:w"),
                {"w": workspace},
            )
            self._pause_sync(db, workspace)
            audit(
                db,
                "alfa_disconnected",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=row["connection_id"],
            )

    @staticmethod
    def _pause_sync(db, workspace):
        db.execute(
            text(
                "UPDATE alfa_sync_runs SET state='cancelled',finished_at=now() "
                "WHERE workspace_id=:w AND state IN ('queued','running')"
            ),
            {"w": workspace},
        )
        db.execute(
            text(
                "UPDATE alfa_sync_control SET enabled=false,running_id=NULL WHERE workspace_id=:w"
            ),
            {"w": workspace},
        )
        db.execute(text("DELETE FROM alfa_sync_queue WHERE workspace_id=:w"), {"w": workspace})
