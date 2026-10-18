import psycopg
import pytest

from guardsql.guardrails.ast_validator import Code, validate_sql
from guardsql.schema.introspect import SchemaSnapshot, introspect

pytestmark = pytest.mark.integration

RESTRICTED = frozenset({"email", "phone", "date_of_birth", "salary"})


@pytest.fixture
def snapshot(ro_conn: psycopg.Connection) -> SchemaSnapshot:
    return introspect(ro_conn, "shop")


def test_finds_all_tables(snapshot: SchemaSnapshot) -> None:
    assert {t.name for t in snapshot.tables} == {
        "customers", "employees", "products", "orders", "order_items",
    }  # fmt: skip


def test_hidden_columns_never_appear(snapshot: SchemaSnapshot) -> None:
    """Column grants are the source of truth: restricted columns must not even be named."""
    assert snapshot.table("customers").column_names == {
        "id", "first_name", "last_name", "country", "signup_date",
    }  # fmt: skip
    assert snapshot.table("employees").column_names == {
        "id",
        "name",
        "department",
        "hire_date",
    }
    everything = snapshot.to_prompt().lower()
    for hidden in RESTRICTED:
        assert hidden not in everything


def test_pg_catalog_would_have_leaked(ro_conn: psycopg.Connection) -> None:
    """Why we avoid pg_catalog-based reflection: it ignores column privileges."""
    rows = ro_conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'shop.customers'::regclass "
        "AND attnum > 0 AND NOT attisdropped"
    ).fetchall()
    assert "email" in {r[0] for r in rows}


def test_comments_and_keys_are_read(snapshot: SchemaSnapshot) -> None:
    orders = snapshot.table("orders")
    assert orders.description is not None and "customer order" in orders.description
    status = next(c for c in orders.columns if c.name == "status")
    assert status.description is not None and "shipped" in status.description
    assert orders.primary_key == ("id",)
    assert any(
        fk.columns == ("customer_id",) and fk.ref_table == "customers" for fk in orders.foreign_keys
    )
    items = snapshot.table("order_items")
    assert {fk.ref_table for fk in items.foreign_keys} == {"orders", "products"}


def test_introspected_policy_drives_the_validator(snapshot: SchemaSnapshot) -> None:
    policy = snapshot.to_policy(RESTRICTED)
    assert validate_sql("SELECT first_name FROM customers LIMIT 3", policy).ok
    assert validate_sql("SELECT email FROM customers", policy).code == Code.RESTRICTED_COLUMN
    assert validate_sql("SELECT secret FROM customers", policy).code == Code.UNKNOWN_COLUMN
