import pytest

from guardsql.schema.introspect import Column, ForeignKey, SchemaSnapshot, Table


@pytest.fixture
def snapshot() -> SchemaSnapshot:
    return SchemaSnapshot(
        schema_name="shop",
        tables=(
            Table(
                "customers",
                (Column("id", "integer", False), Column("country", "text", False)),
                primary_key=("id",),
            ),
            Table(
                "orders",
                (
                    Column("id", "integer", False),
                    Column("customer_id", "integer", False),
                    Column(
                        "status",
                        "text",
                        False,
                        "pending, shipped, delivered, or cancelled",
                    ),
                ),
                description="One row per customer order.",
                primary_key=("id",),
                foreign_keys=(ForeignKey(("customer_id",), "customers", ("id",)),),
            ),
        ),
    )


def test_prompt_describes_tables_keys_and_comments(snapshot: SchemaSnapshot) -> None:
    prompt = snapshot.to_prompt()
    assert "TABLE orders -- One row per customer order." in prompt
    assert "customer_id integer REFERENCES customers(id)" in prompt
    assert "id integer PRIMARY KEY" in prompt
    assert "status text -- pending, shipped, delivered, or cancelled" in prompt


def test_policy_contains_exactly_the_snapshot(snapshot: SchemaSnapshot) -> None:
    policy = snapshot.to_policy(frozenset({"email"}), default_limit=50)
    assert policy.tables == {
        "customers": ("id", "country"),
        "orders": ("id", "customer_id", "status"),
    }
    assert policy.restricted_columns == {"email"}
    assert policy.default_limit == 50
