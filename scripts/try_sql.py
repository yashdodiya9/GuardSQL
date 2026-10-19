"""Try the guardrails by hand: validate, then (if allowed) execute.

uv run python scripts/try_sql.py "SELECT status, count(*) FROM orders GROUP BY status"
"""

from __future__ import annotations

import sys

import psycopg

from guardsql.config import get_settings
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.guardrails.ast_validator import validate_sql
from guardsql.schema.introspect import introspect


def main(sql: str) -> int:
    settings = get_settings()
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        snapshot = introspect(conn, settings.postgres_schema)
    policy = snapshot.to_policy(settings.restricted_column_set, **settings.policy_limits)

    verdict = validate_sql(sql, policy)
    if not verdict.ok:
        print(f"BLOCKED by validator [{verdict.code}]: {verdict.message}")
        return 1
    print(f"validator OK -> will run:\n  {verdict.sql}\n")

    with ReadOnlyExecutor(settings) as executor:
        result = executor.execute(verdict)
    if not result.ok:
        print(f"BLOCKED by executor [{result.code}]: {result.message}")
        return 1

    print(" | ".join(result.columns))
    for row in result.rows[:10]:
        print(" | ".join(str(value) for value in row))
    more = " (limit reached, more rows may exist)" if result.row_limit_hit else ""
    print(
        f"\n{len(result.rows)} row(s){more} in {result.elapsed_ms:.0f} ms, "
        f"planner cost {result.plan_cost:,.0f}"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
