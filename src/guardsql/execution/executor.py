"""The only code allowed to run LLM-generated SQL.

Layers a query passes through here, in order:
  1. It must carry a successful validation result (we run the validator's output).
  2. A pooled connection that is READ ONLY and logged in as the read-only role.
  3. A per-transaction time budget (set_config(..., is_local => true) so it cannot leak).
  4. The EXPLAIN cost gate (nothing runs yet).
  5. Execution, fetching at most the validated row limit.
The transaction is always rolled back: this code never commits anything.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from guardsql.config import Settings
from guardsql.guardrails.ast_validator import ValidationResult
from guardsql.guardrails.cost_gate import exceeds_budget, explain_sql, parse_explain_json

log = logging.getLogger(__name__)


class ExecCode(StrEnum):
    NOT_VALIDATED = "NOT_VALIDATED"
    COST_TOO_HIGH = "COST_TOO_HIGH"
    TIMEOUT = "TIMEOUT"
    DATABASE_ERROR = "DATABASE_ERROR"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    # True when the result reached the row limit, so more rows may exist.
    row_limit_hit: bool = False
    elapsed_ms: float = 0.0
    plan_cost: float | None = None
    code: ExecCode | None = None
    message: str | None = None


def _configure(conn: psycopg.Connection[Any]) -> None:
    """Runs once per new pooled connection: every transaction on it starts READ ONLY."""
    conn.read_only = True


class ReadOnlyExecutor:
    def __init__(self, settings: Settings, acquire_timeout_s: float = 5.0) -> None:
        self._settings = settings
        self._acquire_timeout_s = acquire_timeout_s
        self._pool = ConnectionPool(
            conninfo=settings.readonly_conninfo,
            min_size=1,
            max_size=settings.pool_max_size,
            open=False,
            configure=_configure,
            check=ConnectionPool.check_connection,
            name="guardsql-ro",
        )

    def open(self, wait: bool = True, timeout_s: float = 10.0) -> None:
        self._pool.open(wait=wait, timeout=timeout_s)

    def close(self) -> None:
        self._pool.close()

    def ping(self) -> bool:
        """True if a pooled connection can run a trivial query. Used by /ready."""
        try:
            with self._pool.connection(timeout=self._acquire_timeout_s) as conn:
                conn.execute("SELECT 1")
                conn.rollback()
        except Exception:
            return False
        return True

    def __enter__(self) -> ReadOnlyExecutor:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def execute(self, validated: ValidationResult) -> ExecutionResult:
        """Run a validated query. Expected failures come back as results, not exceptions."""
        if not validated.ok or validated.sql is None:
            return ExecutionResult(
                ok=False,
                code=ExecCode.NOT_VALIDATED,
                message="Only queries that passed validation can be executed.",
            )
        sql = validated.sql
        limit = min(validated.limit or self._settings.max_row_limit, self._settings.max_row_limit)
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            with self._pool.connection(timeout=self._acquire_timeout_s) as conn:
                try:
                    with conn.cursor() as cur:
                        # The third argument (true) makes each setting local to this
                        # transaction, so nothing leaks to the next user of the connection.
                        # JIT is off because our queries are budgeted to be short, and even a
                        # plan-only EXPLAIN of an expensive query would try to load the JIT
                        # compiler: the gate must decide the outcome, not JIT availability.
                        cur.execute(
                            "SELECT set_config('statement_timeout', %s, true), "
                            "set_config('jit', 'off', true)",
                            (str(self._settings.statement_timeout_ms),),
                        )
                        cur.execute(explain_sql(sql))
                        row = cur.fetchone()
                        stats = parse_explain_json(row[0] if row else None)
                        if exceeds_budget(stats, self._settings.max_plan_cost):
                            return ExecutionResult(
                                ok=False,
                                elapsed_ms=elapsed(),
                                plan_cost=stats.total_cost,
                                code=ExecCode.COST_TOO_HIGH,
                                message=(
                                    f"Estimated cost {stats.total_cost:,.0f} is above the limit "
                                    f"of {self._settings.max_plan_cost:,.0f}. Add filters or "
                                    "avoid unbounded joins."
                                ),
                            )
                        cur.execute(sql)
                        columns = tuple(col.name for col in cur.description or ())
                        rows = tuple(tuple(r) for r in cur.fetchmany(limit))
                        return ExecutionResult(
                            ok=True,
                            columns=columns,
                            rows=rows,
                            row_limit_hit=len(rows) >= limit,
                            elapsed_ms=elapsed(),
                            plan_cost=stats.total_cost,
                        )
                finally:
                    conn.rollback()  # never commit; also clears an aborted transaction
        except psycopg.errors.QueryCanceled:
            return ExecutionResult(
                ok=False,
                elapsed_ms=elapsed(),
                code=ExecCode.TIMEOUT,
                message=f"Query exceeded the {self._settings.statement_timeout_ms} ms time limit.",
            )
        except psycopg.OperationalError as exc:  # includes pool timeouts and lost connections
            log.warning("database unavailable: %s", type(exc).__name__)
            return ExecutionResult(
                ok=False,
                elapsed_ms=elapsed(),
                code=ExecCode.UNAVAILABLE,
                message="The database is unavailable. Try again shortly.",
            )
        except psycopg.Error as exc:
            first_line = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            log.warning("database error: %s", type(exc).__name__)
            return ExecutionResult(
                ok=False,
                elapsed_ms=elapsed(),
                code=ExecCode.DATABASE_ERROR,
                message=first_line,
            )
