#!/usr/bin/env bash
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v ro_user="$GUARDSQL_RO_USER" \
     -v ro_pw="$GUARDSQL_RO_PASSWORD" \
     -v db_name="$POSTGRES_DB" <<'EOSQL'

CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 10;

GRANT CONNECT ON DATABASE :"db_name" TO :"ro_user";
GRANT USAGE ON SCHEMA shop TO :"ro_user";

-- Whole-table SELECT where nothing is sensitive
GRANT SELECT ON shop.products, shop.orders, shop.order_items TO :"ro_user";

-- Column-level SELECT: PII columns are deliberately omitted
GRANT SELECT (id, first_name, last_name, country, signup_date) ON shop.customers TO :"ro_user";
GRANT SELECT (id, name, department, hire_date)                 ON shop.employees TO :"ro_user";

-- Role defaults. These are conveniences, NOT the security boundary:
-- a session can override them. The privileges above are the boundary.
ALTER ROLE :"ro_user" SET search_path = shop;
ALTER ROLE :"ro_user" SET statement_timeout = '5s';
ALTER ROLE :"ro_user" SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;

EOSQL