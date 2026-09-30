#!/bin/bash
# Creates a read-only login for the sample database, so connecting to it from the app's
# setup screen works with a login that can only read, the way the app recommends connecting
# to any database. Runs only when this container is initializing a fresh data directory, the
# same as every other script in docker-entrypoint-initdb.d.
set -euo pipefail

: "${CHINOOK_RO_PASSWORD:?CHINOOK_RO_PASSWORD must be set to create the read-only role}"

# The database name is the one 01-chinook-schema.sql creates, not $POSTGRES_DB: that
# variable names whatever database the entrypoint connects the init scripts to before they
# run (left at its own default so 01-chinook-schema.sql can drop and recreate "chinook"
# without dropping the database it is currently connected to), and by the time this script
# runs, 01-chinook-schema.sql has already created and populated "chinook" as its own separate
# step.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname chinook <<-EOSQL
    CREATE ROLE chinhook_ro WITH LOGIN PASSWORD '$CHINOOK_RO_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    GRANT CONNECT ON DATABASE chinook TO chinhook_ro;
    GRANT USAGE ON SCHEMA public TO chinhook_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO chinhook_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chinhook_ro;
    ALTER ROLE chinhook_ro SET default_transaction_read_only = on;
    ALTER ROLE chinhook_ro SET statement_timeout = '3s';
EOSQL
