"""Validate-then-execute: whatever the validator emits must really run on Postgres."""

import psycopg
import pytest

from guardsql.guardrails.ast_validator import validate_sql
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.schema.introspect import introspect

pytestmark = pytest.mark.integration

RESTRICTED = frozenset({"email", "phone", "date_of_birth", "salary"})

QUERIES = [
    "SELECT count(*) FROM orders",
    "SELECT status, count(*) FROM orders GROUP BY status ORDER BY 2 DESC",
    "SELECT o.id, c.first_name FROM orders o JOIN customers c ON c.id = o.customer_id",
    "SELECT date_trunc('month', order_date) AS m, sum(total_amount) FROM orders GROUP BY 1",
    "SELECT extract(year FROM order_date) AS y, count(*) FROM orders GROUP BY y",
    "WITH big AS (SELECT * FROM orders WHERE total_amount > 100) SELECT count(*) FROM big",
    "SELECT name, rank() OVER (ORDER BY price DESC) FROM products",
    "SELECT id FROM orders UNION SELECT id FROM customers",
    "SELECT * FROM customers",
    "SELECT * FROM employees",
    "SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = orders.id)",
    "SELECT category, round(avg(price), 2) FROM products GROUP BY category",
    "SELECT id FROM orders LIMIT ALL",
    "SELECT id FROM orders FETCH FIRST 50000 ROWS ONLY",
    "SELECT $$ ; DROP TABLE orders; $$",
    "SELECT E'\\'; DROP TABLE orders; --'",
    "SELECT 1 /* a /* b */ ; DROP TABLE orders; -- */",
]


@pytest.fixture
def policy(ro_conn: psycopg.Connection) -> SchemaPolicy:
    return introspect(ro_conn, "shop").to_policy(RESTRICTED)


@pytest.mark.parametrize("sql", QUERIES)
def test_validated_sql_executes_and_respects_the_cap(
    ro_conn: psycopg.Connection, policy: SchemaPolicy, sql: str
) -> None:
    result = validate_sql(sql, policy)
    assert result.ok and result.sql is not None, result.message
    rows = ro_conn.execute(result.sql).fetchall()  # type: ignore[arg-type]
    assert result.limit is not None and len(rows) <= result.limit


def test_adversarial_strings_leave_the_database_intact(
    ro_conn: psycopg.Connection, policy: SchemaPolicy
) -> None:
    for sql in QUERIES[-3:]:
        result = validate_sql(sql, policy)
        assert result.sql is not None
        ro_conn.execute(result.sql)  # type: ignore[arg-type]
    count = ro_conn.execute("SELECT count(*) FROM orders").fetchone()
    assert count is not None and count[0] == 2000


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c FROM customers c",
        "SELECT c::text FROM customers c",
        "SELECT (c).email FROM customers c",
        "SELECT salary FROM employees",
    ],
)
def test_database_is_the_backstop_if_the_validator_were_bypassed(
    ro_conn: psycopg.Connection, sql: str
) -> None:
    """Send the raw SQL straight to Postgres: the grants alone must refuse it."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro_conn.execute(sql)  # type: ignore[arg-type]


@pytest.mark.integration
def test_friendly_names_run_in_postgres(ro_conn, live_policy) -> None:  # type: ignore[no-untyped-def]
    result = validate_sql(
        "SELECT status, count(*), count(*) FROM orders GROUP BY status ORDER BY count(*) DESC",
        live_policy,
    )
    assert result.ok and result.sql is not None
    with ro_conn.cursor() as cur:
        cur.execute(result.sql)
        assert [d.name for d in cur.description or []] == ["status", "count", "count_2"]
