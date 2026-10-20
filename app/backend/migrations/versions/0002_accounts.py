"""Private-pilot accounts, sessions, invitations and identity audit."""

from alembic import op

revision = "0002_accounts"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None

USER = "nullif(current_setting('app.user_id', true), '')::uuid"
WORKSPACE = "nullif(current_setting('app.workspace_id', true), '')::uuid"
MANAGER = f"workspace_id = {WORKSPACE} AND current_setting('app.membership_admin', true) = 'true'"

DDL = (
    """CREATE TABLE auth_users (
      id uuid PRIMARY KEY,
      email text NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) <= 254),
      display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 100),
      password_hash text NOT NULL,
      auth_epoch integer NOT NULL DEFAULT 0 CHECK (auth_epoch >= 0),
      active boolean NOT NULL DEFAULT true,
      email_verified_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE auth_sessions (
      id uuid PRIMARY KEY,
      user_id uuid NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
      token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
      auth_epoch integer NOT NULL,
      device_label text NOT NULL CHECK (length(device_label) <= 200),
      created_at timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL,
      revoked_at timestamptz
    )""",
    "CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id, created_at DESC)",
    """CREATE TABLE workspace_memberships (
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      user_id uuid NOT NULL REFERENCES auth_users(id),
      role text NOT NULL CHECK (role IN (
        'owner', 'admin', 'branch_manager', 'operator', 'observer')),
      active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (workspace_id, user_id)
    )""",
    """CREATE UNIQUE INDEX one_active_workspace_owner ON workspace_memberships (workspace_id)
      WHERE role = 'owner' AND active""",
    """CREATE TABLE auth_invitations (
      id uuid PRIMARY KEY,
      workspace_id uuid NOT NULL REFERENCES workspaces(id),
      email text NOT NULL CHECK (email = lower(email) AND length(email) <= 254),
      role text NOT NULL CHECK (role IN ('admin', 'branch_manager', 'operator', 'observer')),
      token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
      issued_by uuid NOT NULL REFERENCES auth_users(id),
      created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL,
      used_at timestamptz,
      revoked_at timestamptz
    )""",
    """CREATE UNIQUE INDEX one_open_invitation ON auth_invitations (workspace_id, email)
      WHERE used_at IS NULL AND revoked_at IS NULL""",
    """CREATE TABLE auth_events (
      id uuid PRIMARY KEY,
      user_id uuid REFERENCES auth_users(id) ON DELETE SET NULL,
      workspace_id uuid REFERENCES workspaces(id),
      action text NOT NULL,
      subject_id uuid,
      created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX auth_events_user_idx ON auth_events (user_id, created_at DESC)",
    """CREATE TABLE auth_throttles (
      bucket_hash text PRIMARY KEY CHECK (bucket_hash ~ '^[a-f0-9]{64}$'),
      window_at timestamptz NOT NULL DEFAULT now(),
      attempts integer NOT NULL DEFAULT 1 CHECK (attempts > 0)
    )""",
    """CREATE TABLE auth_bootstrap (
      singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
      claimed_at timestamptz
    )""",
    "INSERT INTO auth_bootstrap (singleton) VALUES (true)",
)


def upgrade():
    for statement in DDL:
        op.execute(statement)
    predicates = {
        "auth_users": (
            f"id = {USER} OR email = current_setting('app.login_email', true)",
            f"id = {USER}",
        ),
        "auth_sessions": (
            f"user_id = {USER} OR token_hash = current_setting('app.session_hash', true)",
            f"user_id = {USER}",
        ),
        "workspace_memberships": (f"user_id = {USER} OR ({MANAGER})",) * 2,
        "auth_invitations": (
            f"({MANAGER}) OR token_hash = current_setting('app.invite_hash', true)",
        )
        * 2,
    }
    for table, (read, write) in predicates.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY identity_scope ON {table} TO cm_app USING ({read}) WITH CHECK ({write})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO cm_app")
    op.execute("ALTER TABLE auth_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auth_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY audit_read ON auth_events FOR SELECT TO cm_app "
        f"USING (user_id = {USER} OR ({MANAGER}))"
    )
    op.execute(
        f"CREATE POLICY audit_append ON auth_events FOR INSERT TO cm_app "
        f"WITH CHECK (user_id = {USER} OR user_id IS NULL)"
    )
    op.execute("GRANT SELECT, INSERT ON auth_events TO cm_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON auth_throttles TO cm_app")
    op.execute("GRANT SELECT, UPDATE ON auth_bootstrap TO cm_app")
    op.execute(
        "CREATE POLICY manager_directory ON auth_users FOR SELECT TO cm_app "
        "USING (EXISTS (SELECT 1 FROM workspace_memberships m "
        f"WHERE m.workspace_id={WORKSPACE} AND m.user_id=auth_users.id AND m.active "
        "AND current_setting('app.membership_admin', true)='true'))"
    )
    op.execute(
        "CREATE POLICY invitation_issuer_read ON workspace_memberships FOR SELECT TO cm_app "
        "USING (EXISTS (SELECT 1 FROM auth_invitations i "
        "WHERE i.workspace_id=workspace_memberships.workspace_id "
        "AND i.issued_by=workspace_memberships.user_id "
        "AND i.token_hash=current_setting('app.invite_hash', true) "
        "AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at>now()))"
    )
    op.execute(
        "CREATE POLICY member_directory ON workspaces FOR SELECT TO cm_app "
        "USING (EXISTS (SELECT 1 FROM workspace_memberships m "
        f"WHERE m.workspace_id = workspaces.id AND m.user_id = {USER} AND m.active))"
    )


def downgrade():
    op.execute("DROP POLICY member_directory ON workspaces")
    op.execute("DROP POLICY manager_directory ON auth_users")
    op.execute("DROP POLICY invitation_issuer_read ON workspace_memberships")
    for table in (
        "auth_bootstrap",
        "auth_throttles",
        "auth_events",
        "auth_invitations",
        "workspace_memberships",
        "auth_sessions",
        "auth_users",
    ):
        op.execute(f"DROP TABLE {table}")
