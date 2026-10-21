import sqlglot

from guardsql.guardrails.ast_validator import validate_sql
from guardsql.guardrails.policy import SchemaPolicy


def sql_of(query: str, policy: SchemaPolicy) -> str:
    result = validate_sql(query, policy)
    assert result.ok, result.message
    assert result.sql is not None
    return result.sql


def test_aggregate_gets_its_function_name(policy: SchemaPolicy) -> None:
    sql = sql_of("SELECT count(*) FROM orders", policy)
    assert "AS count" in sql
    assert "_col_" not in sql


def test_non_aggregate_gets_positional_name(policy: SchemaPolicy) -> None:
    sql = sql_of("SELECT 1 + 1 FROM orders", policy)
    assert "AS column_1" in sql
    assert "_col_" not in sql


def test_explicit_alias_is_untouched(policy: SchemaPolicy) -> None:
    sql = sql_of("SELECT count(*) AS n FROM orders", policy)
    assert "AS n" in sql
    assert "AS count" not in sql


def test_duplicates_get_suffixes(policy: SchemaPolicy) -> None:
    sql = sql_of("SELECT status, count(*), count(*) FROM orders GROUP BY status", policy)
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert parsed.named_selects == ["status", "count", "count_2"]


def test_never_collides_with_a_users_alias(policy: SchemaPolicy) -> None:
    sql = sql_of("SELECT count(*) AS count, count(*) FROM orders", policy)
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert parsed.named_selects == ["count", "count_2"]


def test_union_names_come_from_the_left_side(policy: SchemaPolicy) -> None:
    sql = sql_of("SELECT count(*) FROM orders UNION ALL SELECT count(*) FROM customers", policy)
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert parsed.named_selects == ["count"]


def test_order_by_aggregate_still_validates(policy: SchemaPolicy) -> None:
    sql = sql_of(
        "SELECT status, count(*) FROM orders GROUP BY status ORDER BY count(*) DESC", policy
    )
    assert "_col_" not in sql


def test_rewrite_does_not_weaken_checks(policy: SchemaPolicy) -> None:
    result = validate_sql("SELECT email, count(*) FROM customers GROUP BY email", policy)
    assert not result.ok
