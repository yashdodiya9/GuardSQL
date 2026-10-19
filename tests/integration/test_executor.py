from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from guardsql.config import Settings
from guardsql.execution.executor import ExecCode, ExecutionResult, ReadOnlyExecutor
from guardsql.guardrails.ast_validator import validate_sql
from guardsql.guardrails.policy import SchemaPolicy

pytestmark = pytest.mark.integration

MakeExecutor = Callable[..., ReadOnlyExecutor]

# Four-way cross join of 2000 rows: ~1.6e13 combinations. If this ever runs, it never finishes.
RUNAWAY = "SELECT count(*) FROM orders a, orders b, orders c, orders d"


def run(executor: ReadOnlyExecutor, policy: SchemaPolicy, sql: str) -> ExecutionResult:
    validated = validate_sql(sql, policy)
    assert validated.ok, validated.message
    return executor.execute(validated)


def test_returns_columns_and_rows(make_executor: MakeExecutor, live_policy: SchemaPolicy) -> None:
    result = run(
        make_executor(),
        live_policy,
        "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status",
    )
    assert result.ok
    assert result.columns == ("status", "n")
    assert [r[0] for r in result.rows] == ["cancelled", "delivered", "pending", "shipped"]
    assert sum(r[1] for r in result.rows) == 2000
    assert not result.row_limit_hit
    assert result.plan_cost is not None and result.plan_cost > 0


def test_row_limit_is_applied_and_reported(
    make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    result = run(make_executor(), live_policy, "SELECT id FROM orders")
    assert result.ok and len(result.rows) == 100 and result.row_limit_hit


def test_percent_signs_in_sql_are_not_treated_as_placeholders(
    make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    result = run(
        make_executor(),
        live_policy,
        "SELECT count(*) FROM customers WHERE first_name LIKE '%a%'",
    )
    assert result.ok and result.rows[0][0] > 0


def test_cost_gate_rejects_before_running(
    make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    """With default settings the runaway query is refused by the *planner estimate*.
    If it had been executed, we would see TIMEOUT instead of COST_TOO_HIGH."""
    result = run(make_executor(), live_policy, RUNAWAY)
    assert not result.ok
    assert result.code == ExecCode.COST_TOO_HIGH
    assert result.plan_cost is not None and result.plan_cost > 50_000
    assert result.message is not None and "cost" in result.message.lower()
    assert result.elapsed_ms < 1000


def test_timeout_is_the_backstop_when_the_planner_is_wrong(
    settings: Settings, make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    """Disable the cost gate to simulate a bad estimate: the time budget still stops it."""
    executor = make_executor(max_plan_cost=1e30, statement_timeout_ms=200)
    result = run(executor, live_policy, RUNAWAY)
    assert not result.ok and result.code == ExecCode.TIMEOUT
    assert result.elapsed_ms < 3000
    # The pool recovers: the very same executor still answers normal queries.
    follow_up = run(executor, live_policy, "SELECT count(*) FROM orders")
    assert follow_up.ok and follow_up.rows[0][0] == 2000
    # Cancelled means gone: no backend is still grinding on the runaway query.
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE state = 'active' AND query LIKE '%orders d%' AND pid <> pg_backend_pid()"
        ).fetchone()
    assert row is not None and row[0] == 0


def test_time_budget_does_not_leak_into_the_next_use_of_the_connection(
    make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    executor = make_executor(pool_max_size=1, statement_timeout_ms=200, max_plan_cost=1e30)
    run(executor, live_policy, RUNAWAY)
    # Same single connection, new transaction: back to the role's own default (5s).
    with executor._pool.connection() as conn:
        timeout = conn.execute("SHOW statement_timeout").fetchone()
        read_only = conn.execute("SHOW transaction_read_only").fetchone()
        conn.rollback()
    assert timeout is not None and timeout[0] == "5s"
    assert read_only is not None and read_only[0] == "on"


def test_database_outage_is_reported_not_raised(
    settings: Settings, live_policy: SchemaPolicy
) -> None:
    broken = settings.model_copy(update={"postgres_port": 1})
    executor = ReadOnlyExecutor(broken, acquire_timeout_s=0.5)
    executor.open(wait=False)
    try:
        result = run(executor, live_policy, "SELECT count(*) FROM orders")
    finally:
        executor.close()
    assert not result.ok and result.code == ExecCode.UNAVAILABLE


def test_database_errors_are_reported_not_raised(
    make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    """A query the validator allows can still fail in Postgres (here: division by zero)."""
    result = run(make_executor(), live_policy, "SELECT 1 / 0 FROM orders")
    assert not result.ok and result.code == ExecCode.DATABASE_ERROR
    assert result.message is not None and "division by zero" in result.message


def test_concurrent_requests_share_a_small_pool(
    make_executor: MakeExecutor, live_policy: SchemaPolicy
) -> None:
    executor = make_executor(pool_max_size=3)
    validated = validate_sql("SELECT status, count(*) FROM orders GROUP BY status", live_policy)
    with ThreadPoolExecutor(max_workers=12) as threads:
        results = list(threads.map(lambda _: executor.execute(validated), range(48)))
    assert all(r.ok and sum(row[1] for row in r.rows) == 2000 for r in results)


def test_pool_survives_an_error(make_executor: MakeExecutor, live_policy: SchemaPolicy) -> None:
    executor = make_executor(pool_max_size=1)
    assert run(executor, live_policy, "SELECT 1 / 0 FROM orders").code == ExecCode.DATABASE_ERROR
    assert run(executor, live_policy, "SELECT count(*) FROM orders").ok


def test_executor_connections_are_read_only(make_executor: MakeExecutor) -> None:
    """Pooled connections start every transaction READ ONLY (the privileges are tested in M1)."""
    executor = make_executor(pool_max_size=1)
    with executor._pool.connection() as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM orders")
        conn.rollback()
