from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, text

from center_messenger.config import Settings

SCHEMA_REVISION = "0016_crm_card_preference"


def log_database_error(logger, error, *, operation):
    # SQLAlchemy's exception text can include SQL or driver detail containing
    # user data. Log just its type and SQLSTATE, never SQL, params or repr(error).
    original = getattr(error, "orig", None)
    logger.error("Database operation failed operation=%s exception=%s driver=%s sqlstate=%s",
                 operation, type(error).__name__, type(original).__name__ if original else "none",
                 getattr(original, "sqlstate", None))


def make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url(),
        pool_size=2,
        max_overflow=1,
        pool_timeout=3,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )


def database_is_ready(engine: Engine, expected_role: str) -> bool:
    with engine.connect() as connection:
        role = (
            connection.execute(
                text(
                    "SELECT current_user AS name, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
            .mappings()
            .one()
        )
        if role["name"] != expected_role or role["rolsuper"] or role["rolbypassrls"]:
            return False
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if revision != SCHEMA_REVISION:
            return False
        # Fail closed if runtime owns a tenant table, RLS was disabled or FORCE was removed.
        tables = (
            connection.execute(
                text(
                    "SELECT count(*) AS found, count(*) FILTER (WHERE "
                    "NOT c.relrowsecurity OR NOT c.relforcerowsecurity "
                    "OR pg_has_role(current_user, c.relowner, 'USAGE')) AS unsafe "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = ANY(:tables) "
                ),
                {"tables": list(TENANT_TABLES + IDENTITY_RLS_TABLES)},
            )
            .mappings()
            .one()
        )
        return tables["found"] == len(TENANT_TABLES + IDENTITY_RLS_TABLES) and tables["unsafe"] == 0


TENANT_TABLES = (
    "workspaces",
    "contacts",
    "students",
    "contact_student_links",
    "connections",
    "conversations",
    "messages",
    "source_records",
    "inbound_events",
    "send_commands",
    "outbox_events",
    "alfa_settings",
    "alfa_branches",
    "alfa_sync_runs",
    "alfa_sync_control",
    "alfa_customers",
    "alfa_groups",
    "alfa_memberships",
    "alfa_statuses",
    "alfa_directory",
    "max_settings",
    "message_changes",
    "max_tombstones",
    "alfa_max_settings",
    "alfa_family_details",
    "ai_settings",
    "ai_agents",
    "ai_agent_versions",
    "ai_usage",
)

IDENTITY_RLS_TABLES = (
    "auth_users",
    "auth_sessions",
    "workspace_memberships",
    "auth_invitations",
    "auth_events",
)


@contextmanager
def workspace_transaction(engine: Engine, workspace_id: UUID) -> Iterator[Connection]:
    # Only an authenticated/authorized server operation may choose this ID in future APIs.
    # Never obtain workspace_id directly from an unverified HTTP header or model output.
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be UUID")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(workspace_id)},
        )
        yield connection
