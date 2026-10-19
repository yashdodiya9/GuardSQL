"""EXPLAIN-based cost gate.

EXPLAIN (without ANALYZE) asks the planner what it *would* do. Nothing is executed, so
this check is cheap and safe. The planner's numbers are estimates, so this is an early
filter, not a guarantee: the statement timeout is what actually stops a runaway query.

We gate on total cost only. Estimated row count is not useful here because the validator
already caps every query with a LIMIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlanStats:
    total_cost: float
    plan_rows: float


def explain_sql(sql: str) -> str:
    """Wrap validated SQL for a plan-only request. Never add ANALYZE: it would run the query."""
    return f"EXPLAIN (FORMAT JSON) {sql}"


def parse_explain_json(payload: Any) -> PlanStats:
    """Read the top plan node from EXPLAIN (FORMAT JSON) output.

    Postgres returns [{"Plan": {"Total Cost": ..., "Plan Rows": ..., ...}}].
    The top node's cost already includes all child nodes.
    """
    try:
        plan = payload[0]["Plan"]
        return PlanStats(total_cost=float(plan["Total Cost"]), plan_rows=float(plan["Plan Rows"]))
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Unexpected EXPLAIN output: {type(exc).__name__}") from exc


def exceeds_budget(stats: PlanStats, max_cost: float) -> bool:
    return stats.total_cost > max_cost
