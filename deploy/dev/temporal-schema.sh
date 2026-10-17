#!/bin/sh
# Apply the Temporal schema to the temporal / temporal_visibility databases.
# POSIX sh only: the admin-tools image has NO bash.
# Adapted from the cloud host's Temporal stack schema job.
# The databases already exist (created by pg-init), so there is no create step.
set -eu

: "${POSTGRES_SEEDS:?POSTGRES_SEEDS is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PWD:?POSTGRES_PWD is required}"

DB="${DB:-postgres12}"
DB_PORT="${DB_PORT:-5432}"
DBNAME="${DBNAME:-temporal}"
VISIBILITY_DBNAME="${VISIBILITY_DBNAME:-temporal_visibility}"
TEMPORAL_HOME="${TEMPORAL_HOME:-/etc/temporal}"

# The tool reads the password from the environment; never pass it on a command line.
export SQL_PASSWORD="$POSTGRES_PWD"

# Bounded wait for the database to accept connections: 60 tries x 2 s.
tries=0
while ! nc -z "$POSTGRES_SEEDS" "$DB_PORT"; do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
        echo "schema: pg not reachable at $POSTGRES_SEEDS:$DB_PORT after 120s" >&2
        exit 1
    fi
    sleep 2
done

sql_tool() {
    _db="$1"
    shift
    temporal-sql-tool --plugin "$DB" --ep "$POSTGRES_SEEDS" \
        -u "$POSTGRES_USER" -p "$DB_PORT" --db "$_db" "$@"
}

# Idempotent: setup-schema only creates the version tables; if they already
# exist it fails, which we treat as "already initialized" — a genuine
# connection/auth failure still surfaces because update-schema then fails.
setup_schema() {
    if ! sql_tool "$1" setup-schema -v 0.0; then
        echo "schema: $1 already initialized, skipping setup-schema" >&2
    fi
}

update_schema() {
    sql_tool "$1" update-schema -d "$2"
}

setup_schema "$DBNAME"
update_schema "$DBNAME" "$TEMPORAL_HOME/schema/postgresql/v12/temporal/versioned"

setup_schema "$VISIBILITY_DBNAME"
update_schema "$VISIBILITY_DBNAME" "$TEMPORAL_HOME/schema/postgresql/v12/visibility/versioned"

echo "schema: done"
