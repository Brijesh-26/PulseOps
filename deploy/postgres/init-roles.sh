#!/bin/sh
# Creates a least-privilege application role alongside the owner role that
# runs migrations. Runs once, on first initialisation of the Postgres volume.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pulseops_app') THEN
    CREATE ROLE pulseops_app LOGIN PASSWORD '${PULSEOPS_APP_DB_PASSWORD}';
  END IF;
END
\$\$;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO pulseops_app;
GRANT USAGE ON SCHEMA public TO pulseops_app;
SQL
