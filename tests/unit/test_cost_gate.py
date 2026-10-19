import pytest

from guardsql.guardrails.cost_gate import (
    PlanStats,
    exceeds_budget,
    explain_sql,
    parse_explain_json,
)


def test_parses_top_node_cost_not_child_cost() -> None:
    payload = [
        {
            "Plan": {
                "Node Type": "Aggregate",
                "Total Cost": 120150.0,
                "Plan Rows": 1,
                "Plans": [{"Node Type": "Nested Loop", "Total Cost": 5.0, "Plan Rows": 9}],
            }
        }
    ]
    assert parse_explain_json(payload) == PlanStats(total_cost=120150.0, plan_rows=1.0)


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, "plan", [{}], [{"Plan": {}}], [{"Plan": {"Total Cost": "abc", "Plan Rows": 1}}]],
)
def test_malformed_explain_output_is_an_error(payload: object) -> None:
    with pytest.raises(ValueError, match="Unexpected EXPLAIN output"):
        parse_explain_json(payload)


def test_budget_boundary_is_inclusive() -> None:
    stats = PlanStats(total_cost=100.0, plan_rows=1)
    assert not exceeds_budget(stats, 100.0)
    assert exceeds_budget(stats, 99.99)


def test_explain_never_uses_analyze() -> None:
    wrapped = explain_sql("SELECT 1")
    assert wrapped == "EXPLAIN (FORMAT JSON) SELECT 1"
    assert "analyze" not in wrapped.lower()
