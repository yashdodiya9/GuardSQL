import re

import psycopg
import pytest

from guardsql.evals.adversarial import EXECUTOR_PAYLOADS
from guardsql.evals.compare import key_of
from guardsql.evals.golden import GOLDEN
from guardsql.evals.seed_policy import SEED_POLICY
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.guardrails.ast_validator import validate_sql


@pytest.mark.integration
def test_seed_policy_matches_the_live_database(live_policy) -> None:  # type: ignore[no-untyped-def]
    live = {t: set(cols) for t, cols in live_policy.tables.items()}
    seed = {t: set(cols) for t, cols in SEED_POLICY.tables.items()}
    assert live == seed
    assert set(live_policy.restricted_columns) == set(SEED_POLICY.restricted_columns)


@pytest.mark.integration
def test_every_reference_runs_and_is_sane(settings, live_policy) -> None:  # type: ignore[no-untyped-def]
    with ReadOnlyExecutor(settings) as executor:
        for case in GOLDEN:
            if case.reference_sql is None:
                continue
            result = executor.execute(validate_sql(case.reference_sql, live_policy))
            assert result.ok, f"{case.id}: {result.code} {result.message}"
            assert result.rows or case.allow_empty, f"{case.id} returned no rows"
            explicit_limit = re.search(r"\blimit\b", case.reference_sql, re.IGNORECASE)
            assert explicit_limit or not result.row_limit_hit, f"{case.id} hit the row limit"
            assert key_of(result)


@pytest.mark.integration
@pytest.mark.parametrize("payload", EXECUTOR_PAYLOADS, ids=lambda p: p.id)
def test_cost_gate_stops_expensive_queries(settings, live_policy, payload) -> None:  # type: ignore[no-untyped-def]
    with ReadOnlyExecutor(settings) as executor:
        result = executor.execute(validate_sql(payload.sql, live_policy))
    assert not result.ok
    assert result.code == payload.expect


@pytest.mark.integration
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status = 'x'",
        "INSERT INTO orders (id) VALUES (1)",
        "DROP TABLE orders",
        "TRUNCATE orders",
        "SELECT email FROM customers",
        "SELECT salary FROM employees",
        "SELECT phone, date_of_birth FROM customers",
    ],
)
def test_database_itself_refuses_when_the_validator_is_bypassed(ro_conn, sql) -> None:  # type: ignore[no-untyped-def]
    """Defence in depth: send the attack straight to Postgres as the read-only role."""
    try:
        with pytest.raises(psycopg.Error):
            ro_conn.execute(sql)
    finally:
        ro_conn.rollback()
