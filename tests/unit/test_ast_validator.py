import pytest
import sqlglot
from sqlglot import exp

from guardsql.guardrails.ast_validator import (
    ALLOWED_FUNCTION_CLASSES,
    Code,
    validate_sql,
)
from guardsql.guardrails.policy import SchemaPolicy

GOOD_QUERIES = [
    "SELECT count(*) FROM orders",
    "SELECT status, count(*) FROM orders GROUP BY status ORDER BY 2 DESC",
    "SELECT first_name, country FROM customers WHERE country = 'India'",
    "SELECT o.id, c.first_name FROM orders o JOIN customers c ON c.id = o.customer_id",
    "SELECT category, round(avg(price), 2) FROM products GROUP BY category",
    "SELECT date_trunc('month', order_date) AS m, sum(total_amount) FROM orders GROUP BY 1",
    "SELECT extract(year FROM order_date) AS y, count(*) FROM orders GROUP BY y",
    "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE country = 'USA')",
    "WITH big AS (SELECT * FROM orders WHERE total_amount > 100) SELECT count(*) FROM big",
    "SELECT name, rank() OVER (ORDER BY price DESC) FROM products",
    "SELECT coalesce(max(price), 0) FROM products",
    "SELECT id FROM orders UNION SELECT id FROM customers",
    "SELECT * FROM products",
    "SELECT order_id, sum(quantity * unit_price) FROM order_items GROUP BY order_id",
    "SELECT CASE WHEN price > 50 THEN 'high' ELSE 'low' END, count(*) FROM products GROUP BY 1",
    "SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = orders.id)",
    "SELECT * FROM Orders;",
    "SELECT * FROM shop.orders",
]

REJECTED = [
    # not a plain SELECT
    ("DELETE FROM orders", Code.NOT_SELECT),
    ("DROP TABLE orders", Code.NOT_SELECT),
    ("UPDATE products SET price = 0", Code.NOT_SELECT),
    ("INSERT INTO products (id) VALUES (1)", Code.NOT_SELECT),
    ("TRUNCATE orders", Code.NOT_SELECT),
    ("CREATE TABLE evil (id int)", Code.NOT_SELECT),
    ("SET default_transaction_read_only = off", Code.NOT_SELECT),
    ("COPY orders TO '/tmp/x'", Code.NOT_SELECT),
    ("VACUUM orders", Code.NOT_SELECT),
    ("EXPLAIN ANALYZE DELETE FROM orders", Code.NOT_SELECT),
    # stacking and writes hidden inside a SELECT
    ("SELECT 1; DROP TABLE orders", Code.MULTIPLE_STATEMENTS),
    ("SELECT 1 /* x */; DELETE FROM orders", Code.MULTIPLE_STATEMENTS),
    (
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        Code.FORBIDDEN_CONSTRUCT,
    ),
    ("SELECT * INTO newt FROM orders", Code.FORBIDDEN_CONSTRUCT),
    ("SELECT * FROM orders FOR UPDATE", Code.FORBIDDEN_CONSTRUCT),
    ("SELECT * FROM orders WHERE id = $1", Code.FORBIDDEN_CONSTRUCT),
    # resource abuse
    (
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t) SELECT * FROM t",
        Code.RECURSIVE_CTE,
    ),
    ("SELECT * FROM generate_series(1, 1000000000)", Code.FUNCTION_NOT_ALLOWED),
    ("SELECT pg_sleep(30)", Code.FUNCTION_NOT_ALLOWED),
    # dangerous or leaky functions
    ("SELECT lo_import('/etc/passwd')", Code.FUNCTION_NOT_ALLOWED),
    (
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        Code.FUNCTION_NOT_ALLOWED,
    ),
    (
        "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
        Code.FUNCTION_NOT_ALLOWED,
    ),
    ("SELECT current_user", Code.FUNCTION_NOT_ALLOWED),
    ("SELECT version()", Code.FUNCTION_NOT_ALLOWED),
    ("SELECT row_to_json(c) FROM customers c", Code.FUNCTION_NOT_ALLOWED),
    # schema and table scope
    ("SELECT * FROM pg_catalog.pg_user", Code.SCHEMA_NOT_ALLOWED),
    ("SELECT * FROM information_schema.tables", Code.SCHEMA_NOT_ALLOWED),
    ("SELECT * FROM pg_user", Code.UNKNOWN_TABLE),
    ("SELECT * FROM invoices", Code.UNKNOWN_TABLE),
    # a CTE named like a catalog table must not hide the real table inside its body
    (
        "WITH pg_user AS (SELECT * FROM pg_user) SELECT * FROM pg_user",
        Code.UNKNOWN_TABLE,
    ),
    # whole-row references hide restricted columns inside a single value
    ("SELECT c FROM customers c", Code.FORBIDDEN_CONSTRUCT),
    ("SELECT customers FROM customers", Code.FORBIDDEN_CONSTRUCT),
    ("SELECT c::text FROM customers c", Code.FORBIDDEN_CONSTRUCT),
    (
        "SELECT id FROM customers c WHERE c::text LIKE '%gmail%'",
        Code.FORBIDDEN_CONSTRUCT,
    ),
    ("SELECT (c).email FROM customers c", Code.FORBIDDEN_CONSTRUCT),
    ("SELECT e FROM employees e", Code.FORBIDDEN_CONSTRUCT),
    # restricted columns, however they are reached
    ("SELECT email FROM customers", Code.RESTRICTED_COLUMN),
    ("SELECT c.phone FROM customers c", Code.RESTRICTED_COLUMN),
    ("SELECT salary FROM employees", Code.RESTRICTED_COLUMN),
    ("SELECT avg(salary) FROM employees", Code.RESTRICTED_COLUMN),
    ("SELECT first_name FROM customers ORDER BY date_of_birth", Code.RESTRICTED_COLUMN),
    ("SELECT id FROM customers WHERE email LIKE '%@gmail.com'", Code.RESTRICTED_COLUMN),
    ('SELECT "email" FROM customers', Code.RESTRICTED_COLUMN),
    # hallucinated columns
    ("SELECT revenue FROM orders", Code.UNKNOWN_COLUMN),
    ("SELECT o.discount FROM orders o", Code.UNKNOWN_COLUMN),
    # empty, garbage, oversized
    ("", Code.EMPTY),
    ("   ;  ", Code.EMPTY),
    ("SELECT * FROM orders WHERE (", Code.PARSE_ERROR),
    ("SELEC * FORM orders", Code.NOT_SELECT),
    ("SELECT ;", Code.PARSE_ERROR),
    ("SELECT " + "x, " * 3000 + "y FROM orders", Code.TOO_LONG),
]


@pytest.mark.parametrize("sql", GOOD_QUERIES)
def test_accepts_safe_queries(policy: SchemaPolicy, sql: str) -> None:
    result = validate_sql(sql, policy)
    assert result.ok, f"{sql!r} rejected: {result.code} {result.message}"
    assert result.sql is not None and result.limit is not None


@pytest.mark.parametrize("sql", GOOD_QUERIES)
def test_output_is_stable_when_revalidated(policy: SchemaPolicy, sql: str) -> None:
    """What we execute must itself pass validation unchanged."""
    first = validate_sql(sql, policy)
    assert first.sql is not None
    second = validate_sql(first.sql, policy)
    assert second.ok and second.sql == first.sql


@pytest.mark.parametrize(("sql", "code"), REJECTED)
def test_rejects_unsafe_queries(policy: SchemaPolicy, sql: str, code: Code) -> None:
    result = validate_sql(sql, policy)
    assert not result.ok
    assert result.code == code, (
        f"{sql[:60]!r}: expected {code}, got {result.code} ({result.message})"
    )
    assert result.sql is None


def test_select_star_is_rewritten_to_readable_columns_only(
    policy: SchemaPolicy,
) -> None:
    result = validate_sql("SELECT * FROM customers", policy)
    assert result.ok and result.sql is not None
    assert "first_name" in result.sql
    for hidden in ("email", "phone", "date_of_birth"):
        assert hidden not in result.sql


def test_select_star_keeps_table_column_order(policy: SchemaPolicy) -> None:
    result = validate_sql("SELECT * FROM customers", policy)
    assert result.ok and result.sql is not None
    positions = [result.sql.index(c) for c in policy.tables["customers"]]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT id FROM orders", 100),
        ("SELECT id FROM orders LIMIT 5", 5),
        ("SELECT id FROM orders LIMIT 999999", 1000),
        ("SELECT id FROM orders LIMIT ALL", 1000),
        ("SELECT id FROM orders FETCH FIRST 50000 ROWS ONLY", 1000),
        ("SELECT id FROM orders UNION SELECT id FROM customers", 100),
        ("SELECT id FROM orders UNION SELECT id FROM customers LIMIT 7", 7),
    ],
)
def test_row_cap_is_enforced(policy: SchemaPolicy, sql: str, expected: int) -> None:
    result = validate_sql(sql, policy)
    assert result.ok and result.sql is not None
    assert result.limit == expected
    assert result.sql.upper().rstrip().endswith(f"LIMIT {expected}")


def test_offset_is_preserved(policy: SchemaPolicy) -> None:
    result = validate_sql("SELECT id FROM orders LIMIT 10 OFFSET 20", policy)
    assert result.ok and result.sql is not None
    assert "OFFSET 20" in result.sql.upper()


def test_comments_do_not_survive(policy: SchemaPolicy) -> None:
    result = validate_sql("SELECT id /* hello */ FROM orders -- trailing", policy)
    assert result.ok and result.sql is not None
    assert "hello" not in result.sql and "trailing" not in result.sql and "--" not in result.sql


def test_reports_tables_used(policy: SchemaPolicy) -> None:
    result = validate_sql(
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id", policy
    )
    assert result.tables == {"orders", "customers"}


ADVERSARIAL = [
    "SELECT 1 /* a /* b */ ; DROP TABLE orders; -- */",
    "SELECT $$ ; DROP TABLE orders; $$",
    "SELECT E'\\'; DROP TABLE orders; --'",
    "SELECT 'it''s'; DELETE FROM orders",
    "SELECT U&'\\0041' FROM orders",
    "SELECT * FROM orders WHERE note = '\\'; DROP TABLE orders; --'",
    "\x00",
    "'; DROP TABLE orders; --",
    "SELECT " + "(" * 1500 + "1" + ")" * 1500,
    "SELECT " + "1+" * 1500 + "1",
    "WITH a AS (SELECT 1) " * 50 + "SELECT 1",
    "SELECT 1 UNION " * 400 + "SELECT 1",
    "SELECT ;",
    "((((",
    "SELECT * FROM orders; SELECT * FROM customers",
]


@pytest.mark.parametrize("sql", ADVERSARIAL)
def test_never_raises_and_never_emits_a_second_statement(policy: SchemaPolicy, sql: str) -> None:
    result = validate_sql(sql, policy)  # must not raise
    if result.ok:
        assert result.sql is not None
        # Re-read what we would execute: one read-only statement, no comments anywhere.
        statements = [s for s in sqlglot.parse(result.sql, dialect="postgres") if s is not None]
        assert len(statements) == 1
        assert isinstance(statements[0], exp.Select | exp.SetOperation)
        assert not list(statements[0].find_all(exp.Drop, exp.Delete, exp.Command))
        assert not any(node.comments for node in statements[0].walk())


def test_function_allowlist_names_exist_in_sqlglot() -> None:
    """Catches typos and sqlglot renames: a stale name silently denies a function."""
    for name in ALLOWED_FUNCTION_CLASSES:
        cls = getattr(exp, name, None)
        assert cls is not None, f"sqlglot has no exp.{name}"
        assert issubclass(cls, exp.Func), f"exp.{name} is not a function node"
