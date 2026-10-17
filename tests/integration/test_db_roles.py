import psycopg
import pytest

pytestmark = pytest.mark.integration

WRITE_STATEMENTS = [
    "INSERT INTO products (id, name, category, price, stock) VALUES (999, 'x', 'x', 1, 1)",
    "UPDATE products SET price = 0 WHERE id = 1",
    "DELETE FROM orders",
    "TRUNCATE order_items",
    "DROP TABLE orders",
    "CREATE TABLE evil (id int)",
]


def test_seed_data_present(ro_conn: psycopg.Connection) -> None:
    counts = {}
    for table in ("customers", "products", "orders"):
        row = ro_conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        counts[table] = row[0]
    assert counts == {"customers": 300, "products": 40, "orders": 2000}


@pytest.mark.parametrize("sql", WRITE_STATEMENTS)
def test_readonly_role_cannot_write(ro_conn: psycopg.Connection, sql: str) -> None:
    with pytest.raises(
        (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)
    ):
        ro_conn.execute(sql)


@pytest.mark.parametrize("sql", WRITE_STATEMENTS)
def test_privileges_hold_even_if_readonly_flag_is_disabled(
    ro_conn: psycopg.Connection, sql: str
) -> None:
    """Defense in depth: the session can turn the read-only default off,
    but the role still has no write privileges."""
    ro_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro_conn.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT salary FROM employees",
        "SELECT email FROM employees",
        "SELECT email FROM customers",
        "SELECT phone FROM customers",
        "SELECT date_of_birth FROM customers",
        "SELECT * FROM customers",
    ],
)
def test_pii_columns_are_blocked_by_the_database(ro_conn: psycopg.Connection, sql: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro_conn.execute(sql)


def test_non_pii_columns_are_readable(ro_conn: psycopg.Connection) -> None:
    rows = ro_conn.execute("SELECT first_name, country FROM customers LIMIT 3").fetchall()
    assert len(rows) == 3


def test_role_defaults(ro_conn: psycopg.Connection) -> None:
    timeout = ro_conn.execute("SHOW statement_timeout").fetchone()
    readonly = ro_conn.execute("SHOW default_transaction_read_only").fetchone()
    assert timeout is not None and timeout[0] == "5s"
    assert readonly is not None and readonly[0] == "on"
