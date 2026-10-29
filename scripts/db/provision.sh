#!/usr/bin/env bash
# Provision a fresh hosted Postgres with the GuardSQL schema, seed data and
# read-only role, using the same SQL files as the local docker init.
#
# Usage:
#   ADMIN_DATABASE_URL='postgresql://user:pass@host/db?sslmode=require' \
#   RO_PASSWORD='choose-a-strong-one' \
#   ./scripts/db/provision.sh
set -euo pipefail

: "${ADMIN_DATABASE_URL:?Set ADMIN_DATABASE_URL to the admin connection string}"
: "${RO_PASSWORD:?Set RO_PASSWORD to the password for the read-only role}"
RO_USER="${RO_USER:-guardsql_ro}"
HERE="$(cd "$(dirname "$0")" && pwd)"

run_psql() {
  docker run --rm -i -v "$HERE/init:/init:ro" postgres:16-alpine \
    psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 "$@"
}

exists="$(run_psql -tAc "SELECT to_regnamespace('shop') IS NOT NULL")"
if [ "$exists" = "t" ]; then
  echo "Schema 'shop' already exists in this database. Refusing to run." >&2
  exit 1
fi

db_name="$(run_psql -tAc "SELECT current_database()")"

echo "Creating schema..."
run_psql -f /init/01_schema.sql
echo "Loading seed data..."
run_psql -f /init/02_seed.sql

echo "Creating read-only role..."
sed -n "/<<'EOSQL'/,/^EOSQL/p" "$HERE/init/03_roles.sh" | sed '1d;$d' \
  | run_psql -v ro_user="$RO_USER" -v ro_pw="$RO_PASSWORD" -v db_name="$db_name" -f -

echo
echo "Done. Use these on the API host:"
echo "  POSTGRES_DB=$db_name"
echo "  POSTGRES_RO_USER=$RO_USER"
echo "  POSTGRES_SSLMODE=require"