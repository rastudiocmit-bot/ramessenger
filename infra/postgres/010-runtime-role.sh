#!/bin/sh
set -eu
# This entrypoint runs only when PostgreSQL initializes an EMPTY data volume.
# Secrets are passed through environment and psql quoting, never through SQL concatenation.
CM_INIT_APP_PASSWORD=$(cat /run/secrets/db_app_password)
export CM_INIT_APP_PASSWORD
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_password CM_INIT_APP_PASSWORD
CREATE ROLE cm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_password';
REVOKE ALL ON DATABASE center_messenger FROM PUBLIC;
GRANT CONNECT ON DATABASE center_messenger TO cm_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO cm_app;
SQL
unset CM_INIT_APP_PASSWORD
