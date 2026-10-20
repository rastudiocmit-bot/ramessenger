"""Persistent, page-at-a-time CRM work; no request or browser owns the import."""

import json
import logging
import threading
from contextlib import contextmanager
from uuid import uuid4

from pydantic import Field
from sqlalchemy import text

from center_messenger.accounts import audit, context
from center_messenger.alfa_client import ERRORS, AlfaError
from center_messenger.auth_security import AuthError, InputModel
from center_messenger.crm_directory import (
    customer_row,
    page_rows,
    positive_id,
    project_directory,
    source_date,
    source_name,
)

PHASES = {
    "branches": "Проверяем филиалы",
    "statuses": "Загружаем статусы",
    "customers": "Загружаем клиентов и лидов",
    "groups": "Загружаем группы",
    "memberships": "Связываем учеников с группами",
    "publish": "Объединяем контакты",
}
SYNC_ERRORS = {
    **ERRORS,
    "settings_changed": "Подключение изменено. Запустите загрузку заново.",
    "access_changed": "Доступ инициатора изменён. Администратор может запустить загрузку заново.",
    "internal": "Загрузка прервалась. Прежние данные доступны. Повторите загрузку.",
}
CACHE_TABLES = (
    "alfa_directory",
    "alfa_memberships",
    "alfa_groups",
    "alfa_statuses",
    "alfa_customers",
)


class SyncOptions(InputModel):
    auto_refresh: bool = True
    interval_minutes: int = Field(default=30, ge=15, le=1440)


class WorkCancelled(Exception):
    pass


class AlfaSync:
    def __init__(self, connections):
        self.connections, self.accounts = connections, connections.accounts

    @staticmethod
    def _control(db, workspace):
        return (
            db.execute(
                text("SELECT * FROM alfa_sync_control WHERE workspace_id=:w FOR UPDATE"),
                {"w": workspace},
            )
            .mappings()
            .one_or_none()
        )

    def status(self, actor, workspace):
        with self.connections._transaction(actor, workspace) as db:
            row = (
                db.execute(
                    text(
                        "SELECT "
                        "c.*,r.state,r.cursor,r.stats,r.error_code,r.created_at,r.finished_at,"
                        "p.finished_at AS last_success_at,q.due_at FROM alfa_sync_control c "
                        "LEFT JOIN alfa_sync_runs r ON r.workspace_id=c.workspace_id AND "
                        "r.id=c.last_run_id "
                        "LEFT JOIN alfa_sync_runs p ON p.workspace_id=c.workspace_id AND "
                        "p.id=c.published_id "
                        "LEFT JOIN alfa_sync_queue q ON q.workspace_id=c.workspace_id WHERE "
                        "c.workspace_id=:w"
                    ),
                    {"w": workspace},
                )
                .mappings()
                .one_or_none()
            )
            configured = self.connections._row(db, workspace)
            ready = configured is not None and configured["secret_ciphertext"] is not None
            if (
                row is None
                or configured is None
                or row["connection_id"] != configured["connection_id"]
            ):
                return {
                    "configured": ready,
                    "state": "idle",
                    "auto_refresh": False,
                    "interval_minutes": 30,
                    "stats": {},
                    "error": None,
                    "last_success_at": None,
                }
            return {
                "configured": ready,
                "state": row["state"] or "idle",
                "auto_refresh": row["enabled"],
                "interval_minutes": row["interval_minutes"],
                "phase": PHASES.get((row["cursor"] or {}).get("phase"), ""),
                "stats": row["stats"] or {},
                "error": SYNC_ERRORS.get(row["error_code"]),
                "last_success_at": row["last_success_at"],
                "started_at": row["created_at"],
                "next_at": row["due_at"] if row["enabled"] else None,
                "run_id": str(row["last_run_id"]) if row["last_run_id"] else None,
            }

    def _new_run(self, db, workspace, settings, requested_by):
        job = uuid4()
        db.execute(
            text(
                "INSERT INTO "
                "alfa_sync_runs(workspace_id,id,connection_id,settings_version,requested_by,state) "
                "VALUES (:w,:id,:c,:v,:u,'queued')"
            ),
            {
                "w": workspace,
                "id": job,
                "c": settings["connection_id"],
                "v": settings["version"],
                "u": requested_by,
            },
        )
        return job

    def start(self, actor, workspace, options):
        self.accounts.throttle(f"crm-sync:{workspace}:{actor.user_id}", limit=10, minutes=1)
        with self.connections._transaction(actor, workspace, lock=True) as db:
            settings = self.connections._row(db, workspace)
            if settings is None or not settings["secret_ciphertext"]:
                raise AuthError(409, "Сначала подключите АльфаCRM в разделе «Подключения».")
            if not self.connections.vault.ready:
                raise AuthError(
                    503, "Ключ хранилища недоступен. Обратитесь к администратору сервера."
                )
            control = self._control(db, workspace)
            if control and control["running_id"]:
                return str(control["running_id"])
            job = self._new_run(db, workspace, settings, actor.user_id)
            db.execute(
                text(
                    "INSERT INTO alfa_sync_control(workspace_id,connection_id,requested_by,enabled,"
                    "interval_minutes,running_id,last_run_id) VALUES "
                    "(:w,:c,:u,:enabled,:interval,:job,:job) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET "
                    "published_id=CASE WHEN alfa_sync_control.connection_id=EXCLUDED.connection_id "
                    "THEN alfa_sync_control.published_id ELSE NULL END,"
                    "previous_id=CASE WHEN alfa_sync_control.connection_id=EXCLUDED.connection_id "
                    "THEN alfa_sync_control.previous_id ELSE NULL END,"
                    "connection_id=EXCLUDED.connection_id,requested_by=EXCLUDED.requested_by,"
                    "enabled=EXCLUDED.enabled,interval_minutes=EXCLUDED.interval_minutes,"
                    "running_id=EXCLUDED.running_id,last_run_id=EXCLUDED.last_run_id,updated_at=now()"
                ),
                {
                    "w": workspace,
                    "c": settings["connection_id"],
                    "u": actor.user_id,
                    "enabled": options.auto_refresh,
                    "interval": options.interval_minutes,
                    "job": job,
                },
            )
            db.execute(
                text(
                    "INSERT INTO alfa_sync_queue(workspace_id) VALUES (:w) ON "
                    "CONFLICT(workspace_id) "
                    "DO UPDATE SET due_at=now(),lease_owner=NULL,lease_until='-infinity'"
                ),
                {"w": workspace},
            )
            audit(
                db,
                "alfa_sync_requested",
                user_id=actor.user_id,
                workspace_id=workspace,
                subject_id=job,
            )
        return str(job)

    def stop(self, actor, workspace):
        with self.connections._transaction(actor, workspace, lock=True) as db:
            control = self._control(db, workspace)
            if control is None:
                return
            db.execute(
                text(
                    "UPDATE alfa_sync_runs SET state='cancelled',finished_at=now() "
                    "WHERE workspace_id=:w AND id=:id AND state IN ('queued','running')"
                ),
                {"w": workspace, "id": control["running_id"]},
            )
            db.execute(
                text(
                    "UPDATE alfa_sync_control SET enabled=false,running_id=NULL "
                    "WHERE workspace_id=:w"
                ),
                {"w": workspace},
            )
            db.execute(text("DELETE FROM alfa_sync_queue WHERE workspace_id=:w"), {"w": workspace})
            audit(db, "alfa_sync_stopped", user_id=actor.user_id, workspace_id=workspace)

    @contextmanager
    def _work_transaction(self, workspace, lease):
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            control = self._control(db, workspace)
            token = db.execute(
                text(
                    "SELECT workspace_id FROM alfa_sync_queue WHERE workspace_id=:w "
                    "AND lease_owner=:lease AND lease_until>clock_timestamp() FOR UPDATE"
                ),
                {"w": workspace, "lease": lease},
            ).scalar_one_or_none()
            if token is None:
                raise WorkCancelled("settings_changed")
            if control is None:
                raise WorkCancelled("settings_changed")
            context(db, user_id=control["requested_by"])
            role = db.execute(
                text(
                    "SELECT m.role FROM workspace_memberships m JOIN auth_users u ON "
                    "u.id=m.user_id "
                    "WHERE m.workspace_id=:w AND m.user_id=:u AND m.active AND u.active "
                    "AND m.role IN ('owner','admin')"
                ),
                {"w": workspace, "u": control["requested_by"]},
            ).scalar_one_or_none()
            if role is None:
                raise WorkCancelled("access_changed")
            context(db, membership_admin="true")
            yield db, control

    def _claim(self):
        lease = uuid4()
        with self.accounts.transaction() as db:
            workspace = db.execute(
                text(
                    "WITH due AS (SELECT workspace_id FROM alfa_sync_queue "
                    "WHERE due_at<=clock_timestamp() AND lease_until<clock_timestamp() "
                    "ORDER BY due_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                    "UPDATE alfa_sync_queue q SET lease_owner=:lease,"
                    "lease_until=clock_timestamp()+interval '60 seconds' FROM due "
                    "WHERE q.workspace_id=due.workspace_id RETURNING q.workspace_id"
                ),
                {"lease": lease},
            ).scalar_one_or_none()
        return (workspace, lease) if workspace else None

    def _release(self, workspace, lease, seconds=0.2):
        with self.accounts.transaction() as db:
            db.execute(
                text(
                    "UPDATE alfa_sync_queue SET lease_owner=NULL,lease_until='-infinity',"
                    "due_at=now()+(:seconds * interval '1 second') "
                    "WHERE workspace_id=:w AND lease_owner=:lease"
                ),
                {"w": workspace, "lease": lease, "seconds": seconds},
            )

    def _job(self, db, control, settings):
        workspace = control["workspace_id"]
        if (
            settings is None
            or not settings["secret_ciphertext"]
            or settings["connection_id"] != control["connection_id"]
        ):
            raise WorkCancelled("settings_changed")
        if control["running_id"] is None:
            if not control["enabled"]:
                raise WorkCancelled("settings_changed")
            job = self._new_run(db, workspace, settings, control["requested_by"])
            db.execute(
                text(
                    "UPDATE alfa_sync_control SET running_id=:id,last_run_id=:id "
                    "WHERE workspace_id=:w"
                ),
                {"w": workspace, "id": job},
            )
        else:
            job = control["running_id"]
        row = (
            db.execute(
                text("SELECT * FROM alfa_sync_runs WHERE workspace_id=:w AND id=:id"),
                {"w": workspace, "id": job},
            )
            .mappings()
            .one()
        )
        if (
            row["state"] not in {"queued", "running"}
            or row["settings_version"] != settings["version"]
        ):
            raise WorkCancelled("settings_changed")
        db.execute(
            text("UPDATE alfa_sync_runs SET state='running' WHERE workspace_id=:w AND id=:id"),
            {"w": workspace, "id": job},
        )
        return dict(row)

    def run_next(self):
        claimed = self._claim()
        if claimed is None:
            return False
        workspace, lease = claimed
        try:
            self._step(workspace, lease)
        except WorkCancelled as error:
            self._fail(workspace, lease, str(error), retry=False)
        except AlfaError as error:
            self._fail(
                workspace,
                lease,
                error.code,
                retry=error.code
                in {
                    "network",
                    "timeout",
                    "unavailable",
                    "rate_limited",
                },
            )
        except AuthError as error:
            if error.status == 409:
                self._release(workspace, lease, seconds=2)
            else:
                self._fail(workspace, lease, "internal", retry=False)
        except Exception:
            self._fail(workspace, lease, "internal", retry=False)
            logging.getLogger(__name__).warning(
                "CRM sync page failed; inspect workspace sync status"
            )
        finally:
            self._release(workspace, lease)
        return True

    def _step(self, workspace, lease):
        with self._work_transaction(workspace, lease) as (db, control):
            settings = self.connections._row(db, workspace)
            job = self._job(db, control, settings)
            settings = dict(settings)
            cursor, stats = dict(job["cursor"]), dict(job["stats"])
            if cursor["phase"] == "publish":
                self._publish(db, control, job)
                return
            if cursor["phase"] == "memberships":
                branches = cursor["branches"]
                branch = branches[cursor["bi"]]["id"]
                group = db.execute(
                    text(
                        "SELECT external_id FROM alfa_groups WHERE workspace_id=:w AND run_id=:r "
                        "AND branch_id=:b AND external_id>:last ORDER BY external_id LIMIT 1"
                    ),
                    {
                        "w": workspace,
                        "r": job["id"],
                        "b": branch,
                        "last": cursor.get("last_group", 0),
                    },
                ).scalar_one_or_none()
                if group is None:
                    cursor["bi"] += 1
                    cursor.update(page=0, received=0, total=None, last_group=0)
                    if cursor["bi"] == len(branches):
                        cursor["phase"] = "publish"
                    self._save_progress(db, workspace, job["id"], cursor, stats)
                    return
                cursor["group"] = group
        # No DB transaction is held during rate limiting or network IO.
        with self.connections._host_gate(settings["origin"]) as gate:
            credentials = self.connections.vault.open(
                workspace, settings["connection_id"], settings["secret_ciphertext"]
            )
            client = self.connections.client_factory(
                settings["origin"], settings["api_email"], credentials, gate
            )
            if cursor["phase"] == "branches":
                data = client.branches(settings["seed_branch_id"])
                if not data:
                    raise AlfaError("forbidden")
            else:
                path, body = self._request(cursor)
                data = client.call(path, body)
            with self._work_transaction(workspace, lease) as (db, control):
                current = self.connections._row(db, workspace)
                active_job = self._job(db, control, current)
                if active_job["id"] != job["id"] or current["version"] != settings["version"]:
                    raise WorkCancelled("settings_changed")
                encrypted = self.connections.vault.seal(
                    workspace, settings["connection_id"], credentials
                )
                db.execute(
                    text("UPDATE alfa_settings SET secret_ciphertext=:s WHERE workspace_id=:w"),
                    {"w": workspace, "s": encrypted},
                )
                if cursor["phase"] == "branches":
                    self._branches(db, workspace, settings["connection_id"], data)
                    cursor = {
                        "phase": "statuses",
                        "branches": data,
                        "bi": 0,
                        "part": 0,
                        "page": 0,
                        "received": 0,
                        "total": None,
                    }
                    stats["branches"] = len(data)
                else:
                    items, total = page_rows(data, cursor["page"])
                    if cursor["total"] is not None and total != cursor["total"]:
                        raise AlfaError("invalid_response")
                    received = cursor["received"] + len(items)
                    if received > total or (not items and received < total):
                        raise AlfaError("invalid_response")
                    self._store_page(db, workspace, job["id"], cursor, items)
                    phase = cursor["phase"]
                    stats[phase] = stats.get(phase, 0) + len(items)
                    if phase == "customers" and stats[phase] > 50000:
                        raise AlfaError("import_limit")
                    cursor.update(received=received, total=total, page=cursor["page"] + 1)
                    if received == total:
                        self._advance(cursor)
                cursor["retry_count"] = 0
                self._save_progress(db, workspace, job["id"], cursor, stats)

    @staticmethod
    def _request(cursor):
        branch = cursor["branches"][cursor["bi"]]["id"]
        body = {"page": cursor["page"], "pageSize": 500}
        phase = cursor["phase"]
        entity = {
            "statuses": ("study-status", "lead-status")[cursor.get("part", 0)],
            "customers": "customer",
            "groups": "group",
            "memberships": "cgi",
        }[phase]
        if phase == "customers":
            body.update(is_study=2, removed=(0, 2)[cursor["part"]])
        if phase == "groups":
            body["removed"] = 0
        suffix = f"?group_id={cursor['group']}" if phase == "memberships" else ""
        return f"/v2api/{branch}/{entity}/index{suffix}", body

    @staticmethod
    def _advance(cursor):
        phase = cursor["phase"]
        cursor.update(page=0, received=0, total=None)
        if phase == "memberships":
            cursor["last_group"] = cursor.pop("group")
            return
        parts = 2 if phase in {"statuses", "customers"} else 1
        cursor["part"] = cursor.get("part", 0) + 1
        if cursor["part"] == parts:
            cursor["part"] = 0
            cursor["bi"] += 1
        if cursor["bi"] == len(cursor["branches"]):
            cursor["bi"] = 0
            cursor["phase"] = {
                "statuses": "customers",
                "customers": "groups",
                "groups": "memberships",
            }[phase]
            cursor["last_group"] = 0

    @staticmethod
    def _save_progress(db, workspace, job, cursor, stats):
        db.execute(
            text(
                "UPDATE alfa_sync_runs SET cursor=CAST(:cursor AS jsonb),"
                "stats=CAST(:stats AS jsonb),error_code=NULL WHERE workspace_id=:w AND id=:r"
            ),
            {"w": workspace, "r": job, "cursor": json.dumps(cursor), "stats": json.dumps(stats)},
        )

    @staticmethod
    def _branches(db, workspace, connection, branches):
        db.execute(
            text(
                "UPDATE alfa_branches SET available=false WHERE workspace_id=:w AND "
                "connection_id=:c"
            ),
            {"w": workspace, "c": connection},
        )
        for branch in branches:
            db.execute(
                text(
                    "INSERT INTO alfa_branches(workspace_id,connection_id,external_id,name) "
                    "VALUES (:w,:c,:id,:name) ON CONFLICT(workspace_id,connection_id,external_id) "
                    "DO UPDATE SET name=EXCLUDED.name,available=true,last_seen_at=now()"
                ),
                {"w": workspace, "c": connection, **branch},
            )

    @staticmethod
    def _store_page(db, workspace, job, cursor, items):
        if not items:
            return
        phase, branch = cursor["phase"], cursor["branches"][cursor["bi"]]["id"]
        table = {
            "customers": "alfa_customers",
            "groups": "alfa_groups",
            "statuses": "alfa_statuses",
            "memberships": "alfa_memberships",
        }[phase]
        extra = (
            " AND kind=:kind"
            if phase == "statuses"
            else " AND group_id=:group"
            if phase == "memberships"
            else ""
        )
        kind = (
            ("study-status", "lead-status")[cursor.get("part", 0)] if phase == "statuses" else None
        )
        params = {
            "w": workspace,
            "r": job,
            "b": branch,
            "kind": kind,
            "group": cursor.get("group"),
            "ids": [positive_id(i["id"]) for i in items],
        }
        duplicate = db.execute(
            text(
                f"SELECT 1 FROM {table} WHERE workspace_id=:w AND run_id=:r "
                f"AND branch_id=:b {extra} AND external_id=ANY(CAST(:ids AS bigint[])) LIMIT 1"
            ),
            params,
        ).scalar_one_or_none()
        if duplicate:
            raise AlfaError("invalid_response")
        base = {"workspace_id": workspace, "run_id": job, "branch_id": branch}
        rows = []
        for item in items:
            if phase == "customers":
                row = customer_row(item, archive=cursor["part"] == 1)
                for key in ("phones", "emails", "lead_status_ids"):
                    row[key] = json.dumps(row[key])
            elif phase == "groups":
                row = {
                    "external_id": positive_id(item["id"]),
                    "name": source_name(item.get("name")),
                }
            elif phase == "statuses":
                row = {
                    "external_id": positive_id(item["id"]),
                    "name": source_name(item.get("name")),
                    "kind": kind,
                }
            else:
                group = positive_id(item.get("group_id"))
                if group != cursor["group"]:
                    raise AlfaError("invalid_response")
                begin, end = source_date(item.get("b_date")), source_date(item.get("e_date"))
                if begin and end and begin > end:
                    raise AlfaError("invalid_response")
                row = {
                    "external_id": positive_id(item["id"]),
                    "group_id": group,
                    "customer_id": positive_id(item.get("customer_id")),
                    "b_date": begin,
                    "e_date": end,
                }
            rows.append(base | row)
        columns = tuple(rows[0])
        values = [
            f"CAST(:{c} AS jsonb)" if c in {"phones", "emails", "lead_status_ids"} else f":{c}"
            for c in columns
        ]
        db.execute(
            text(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join(values)})"), rows
        )

    def _publish(self, db, control, job):
        workspace, run, connection = job["workspace_id"], job["id"], job["connection_id"]
        rows = list(
            db.execute(
                text(
                    "SELECT * FROM alfa_customers WHERE workspace_id=:w AND run_id=:r "
                    "ORDER BY branch_id,external_id"
                ),
                {"w": workspace, "r": run},
            ).mappings()
        )
        previous = list(
            db.execute(
                text(
                    "SELECT external_id,student_id FROM alfa_customers "
                    "WHERE workspace_id=:w AND run_id=:r"
                ),
                {"w": workspace, "r": control["published_id"]},
            ).mappings()
        )
        contacts, students, bindings = project_directory(connection, rows, previous)
        for chunk_start in range(0, len(contacts), 500):
            chunk = contacts[chunk_start : chunk_start + 500]
            db.execute(
                text(
                    "INSERT INTO contacts(workspace_id,id,display_name) VALUES (:w,:id,:name) "
                    "ON CONFLICT(workspace_id,id) DO UPDATE SET display_name=EXCLUDED.display_name"
                ),
                [{"w": workspace, "id": c["id"], "name": c["name"]} for c in chunk],
            )
            db.execute(
                text(
                    "INSERT INTO alfa_directory(workspace_id,run_id,contact_id,display_name,"
                    "phones,emails,needs_review,search_text) "
                    "VALUES (:w,:r,:id,:name,CAST(:phones AS jsonb),CAST(:emails AS "
                    "jsonb),:review,:search)"
                ),
                [
                    {
                        "w": workspace,
                        "r": run,
                        "id": c["id"],
                        "name": c["name"],
                        "phones": json.dumps(c["phones"]),
                        "emails": json.dumps(c["emails"]),
                        "review": c["needs_review"],
                        "search": c["search_text"],
                    }
                    for c in chunk
                ],
            )
        for start in range(0, len(students), 500):
            db.execute(
                text(
                    "INSERT INTO students(workspace_id,id,display_name) VALUES (:w,:id,:name) "
                    "ON CONFLICT(workspace_id,id) DO UPDATE SET display_name=EXCLUDED.display_name"
                ),
                [{"w": workspace, **s} for s in students[start : start + 500]],
            )
        for start in range(0, len(bindings), 500):
            values = [{"w": workspace, "r": run, **b} for b in bindings[start : start + 500]]
            db.execute(
                text(
                    "UPDATE alfa_customers SET contact_id=:contact_id,student_id=:student_id,"
                    "needs_review=:needs_review WHERE workspace_id=:w AND run_id=:r "
                    "AND branch_id=:branch_id AND external_id=:external_id"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO contact_student_links"
                    "(workspace_id,contact_id,student_id,managed_by_alfa) "
                    "VALUES (:w,:contact_id,:student_id,true) ON CONFLICT DO NOTHING"
                ),
                values,
            )
        db.execute(
            text(
                "DELETE FROM contact_student_links l WHERE l.workspace_id=:w AND l.managed_by_alfa "
                "AND l.contact_id IN (SELECT contact_id FROM alfa_directory "
                "WHERE workspace_id=:w AND (run_id=:r OR run_id=:previous)) "
                "AND NOT EXISTS (SELECT 1 FROM alfa_customers c "
                "WHERE c.workspace_id=l.workspace_id "
                "AND c.run_id=:r AND c.contact_id=l.contact_id AND c.student_id=l.student_id)"
            ),
            {"w": workspace, "r": run, "previous": control["published_id"]},
        )
        unresolved = db.execute(
            text(
                "SELECT count(*) FROM alfa_memberships m WHERE m.workspace_id=:w AND m.run_id=:r "
                "AND NOT EXISTS (SELECT 1 FROM alfa_customers c "
                "WHERE c.workspace_id=m.workspace_id "
                "AND c.run_id=m.run_id AND c.branch_id=m.branch_id AND c.external_id=m.customer_id)"
            ),
            {"w": workspace, "r": run},
        ).scalar_one()
        stats = dict(job["stats"])
        stats.update(
            contacts=len(contacts),
            students=len(students),
            cards=len(rows),
            unique_cards=len({r["external_id"] for r in rows}),
            needs_review=sum(c["needs_review"] for c in contacts),
            unresolved_group_links=unresolved,
        )
        self._save_progress(db, workspace, run, job["cursor"], stats)
        db.execute(
            text(
                "UPDATE alfa_sync_runs SET state='succeeded',finished_at=now() "
                "WHERE workspace_id=:w AND id=:r"
            ),
            {"w": workspace, "r": run},
        )
        db.execute(
            text(
                "UPDATE alfa_sync_control SET previous_id=published_id,published_id=:r,"
                "running_id=NULL,failures=0,updated_at=now() WHERE workspace_id=:w"
            ),
            {"w": workspace, "r": run},
        )
        if control["enabled"]:
            self._reschedule(db, workspace, control["interval_minutes"] * 60)
        else:
            db.execute(text("DELETE FROM alfa_sync_queue WHERE workspace_id=:w"), {"w": workspace})
        keep = [run] + ([control["published_id"]] if control["published_id"] else [])
        for table in CACHE_TABLES:
            db.execute(
                text(
                    f"DELETE FROM {table} WHERE workspace_id=:w "
                    "AND NOT(run_id=ANY(CAST(:keep AS uuid[])))"
                ),
                {"w": workspace, "keep": keep},
            )
        audit(
            db,
            "alfa_directory_published",
            user_id=control["requested_by"],
            workspace_id=workspace,
            subject_id=run,
        )

    @staticmethod
    def _reschedule(db, workspace, seconds):
        db.execute(
            text(
                "UPDATE alfa_sync_queue SET due_at=now()+(:seconds * interval '1 second'),"
                "lease_owner=NULL,lease_until='-infinity' WHERE workspace_id=:w"
            ),
            {"w": workspace, "seconds": seconds},
        )

    def _fail(self, workspace, lease, code, *, retry):
        with self.accounts.transaction(workspace_id=workspace) as db:
            db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})
            control = self._control(db, workspace)
            owned = db.execute(
                text(
                    "SELECT workspace_id FROM alfa_sync_queue "
                    "WHERE workspace_id=:w AND lease_owner=:l FOR UPDATE"
                ),
                {"w": workspace, "l": lease},
            ).scalar_one_or_none()
            if owned is None:
                return
            if control is None or control["running_id"] is None:
                db.execute(
                    text("DELETE FROM alfa_sync_queue WHERE workspace_id=:w"), {"w": workspace}
                )
                return
            row = db.execute(
                text("SELECT cursor FROM alfa_sync_runs WHERE workspace_id=:w AND id=:r"),
                {"w": workspace, "r": control["running_id"]},
            ).scalar_one()
            cursor = dict(row)
            attempts = cursor.get("retry_count", 0) + 1
            resume = retry and attempts <= 3
            cursor["retry_count"] = attempts
            db.execute(
                text(
                    "UPDATE alfa_sync_runs SET state=:state,error_code=:code,"
                    "cursor=CAST(:cursor AS jsonb),finished_at=CASE WHEN :resume THEN NULL "
                    "ELSE now() END "
                    "WHERE workspace_id=:w AND id=:r"
                ),
                {
                    "w": workspace,
                    "r": control["running_id"],
                    "code": code,
                    "state": "queued" if resume else "failed",
                    "cursor": json.dumps(cursor),
                    "resume": resume,
                },
            )
            if resume:
                self._reschedule(db, workspace, 30 * 2 ** (attempts - 1))
            else:
                db.execute(
                    text(
                        "UPDATE alfa_sync_control SET "
                        "enabled=false,running_id=NULL,failures=failures+1 "
                        "WHERE workspace_id=:w"
                    ),
                    {"w": workspace},
                )
                db.execute(
                    text("DELETE FROM alfa_sync_queue WHERE workspace_id=:w"), {"w": workspace}
                )


class SyncWorker:
    def __init__(self, sync):
        self.sync, self.stopping = sync, threading.Event()
        self.thread = threading.Thread(target=self._run, name="alfa-sync", daemon=True)

    def _run(self):
        while not self.stopping.is_set():
            try:
                worked = self.sync.run_next()
            except Exception:
                worked = False
                logging.getLogger(__name__).warning("CRM sync queue temporarily unavailable")
            self.stopping.wait(0.2 if worked else 2)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=25)
