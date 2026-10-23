import pytest

from guardsql.evals.metrics import (
    percentile,
    summarize_attacks,
    summarize_golden,
    wilson,
)
from guardsql.evals.results import AttackOutcome, AttackResult, CaseResult, Outcome


def case(
    outcome: Outcome,
    *,
    difficulty: str = "easy",
    answerable: bool = True,
    confidence: str | None = "HIGH",
    repairs: int = 0,
    latency: float = 100.0,
) -> CaseResult:
    return CaseResult(
        case_id="x",
        difficulty=difficulty,
        answerable=answerable,
        outcome=outcome,
        confidence=confidence,
        score=0.5,
        repairs=repairs,
        llm_calls=3,
        input_tokens=900,
        output_tokens=30,
        usage_complete=True,
        latency_ms=latency,
        sql=None,
        code=None,
    )


def test_wilson_known_values() -> None:
    low, high = wilson(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.2775, abs=1e-3)
    low, high = wilson(10, 10)
    assert low == pytest.approx(0.7225, abs=1e-3)
    assert high == 1.0
    assert wilson(0, 0) == (0.0, 0.0)


def test_wilson_contains_the_point_estimate() -> None:
    low, high = wilson(31, 42)
    assert low < 31 / 42 < high


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert percentile([], 50) == 0.0
    assert percentile([7], 95) == 7


def test_golden_summary_counts() -> None:
    results = [
        case(Outcome.CORRECT, difficulty="easy"),
        case(Outcome.CORRECT, difficulty="hard", confidence="LOW"),
        case(Outcome.WRONG, difficulty="hard", confidence="LOW", repairs=1),
        case(Outcome.BLOCKED, difficulty="medium", confidence=None),
        case(Outcome.REFUSED, difficulty="unanswerable", answerable=False, confidence=None),
        case(Outcome.WRONG, difficulty="unanswerable", answerable=False),
    ]
    s = summarize_golden(results)
    assert (s.total, s.answerable, s.correct, s.executed) == (6, 4, 2, 3)
    assert s.by_difficulty["hard"] == (1, 2)
    assert s.calibration["HIGH"] == (1, 1)
    assert s.calibration["LOW"] == (1, 2)
    assert (s.repaired, s.repaired_correct) == (1, 0)
    assert (s.unanswerable, s.unanswerable_handled) == (2, 1)
    assert s.mean_llm_calls == 3.0


def test_attack_summary() -> None:
    def attack(category: str, outcome: AttackOutcome, leaked: bool = False) -> AttackResult:
        return AttackResult("a", category, outcome, leaked, None, None, 1, 1.0)

    s = summarize_attacks(
        [
            attack("pii", AttackOutcome.BLOCKED),
            attack("pii", AttackOutcome.REFUSED),
            attack("inj", AttackOutcome.ANSWERED, leaked=True),
        ]
    )
    assert s.total == 3 and s.leaked == 1
    assert s.by_category["pii"] == {"BLOCKED": 1, "REFUSED": 1}
