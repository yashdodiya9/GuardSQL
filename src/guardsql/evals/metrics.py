from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from guardsql.evals.results import AttackOutcome, AttackResult, CaseResult, Outcome


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. Honest at small n, unlike mean +/- stderr."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.1f}%" if n else "n/a"


def _with_ci(k: int, n: int) -> str:
    low, high = wilson(k, n)
    return f"{k}/{n} = {_pct(k, n)} (95% CI {100 * low:.1f}-{100 * high:.1f}%)"


_EXECUTED = (Outcome.CORRECT, Outcome.WRONG)


@dataclass(frozen=True)
class GoldenSummary:
    total: int
    answerable: int
    correct: int
    executed: int
    outcomes: dict[str, int]
    by_difficulty: dict[str, tuple[int, int]]  # difficulty -> (correct, n)
    calibration: dict[str, tuple[int, int]]  # confidence -> (correct, executed)
    repaired: int
    repaired_correct: int
    unanswerable: int
    unanswerable_handled: int
    latency_p50_ms: float
    latency_p95_ms: float
    mean_llm_calls: float
    mean_input_tokens: float
    mean_output_tokens: float
    usage_complete: bool


def summarize_golden(results: Sequence[CaseResult]) -> GoldenSummary:
    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]
    executed = [r for r in answerable if r.outcome in _EXECUTED]

    by_difficulty: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in answerable:
        by_difficulty[r.difficulty][1] += 1
        by_difficulty[r.difficulty][0] += r.outcome is Outcome.CORRECT

    calibration: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in executed:
        key = r.confidence or "NONE"
        calibration[key][1] += 1
        calibration[key][0] += r.outcome is Outcome.CORRECT

    repaired = [r for r in answerable if r.repairs > 0]
    n = len(results)
    return GoldenSummary(
        total=n,
        answerable=len(answerable),
        correct=sum(r.outcome is Outcome.CORRECT for r in answerable),
        executed=len(executed),
        outcomes=dict(Counter(str(r.outcome) for r in results)),
        by_difficulty={k: (v[0], v[1]) for k, v in by_difficulty.items()},
        calibration={k: (v[0], v[1]) for k, v in calibration.items()},
        repaired=len(repaired),
        repaired_correct=sum(r.outcome is Outcome.CORRECT for r in repaired),
        unanswerable=len(unanswerable),
        unanswerable_handled=sum(r.outcome not in _EXECUTED for r in unanswerable),
        latency_p50_ms=percentile([r.latency_ms for r in results], 50),
        latency_p95_ms=percentile([r.latency_ms for r in results], 95),
        mean_llm_calls=sum(r.llm_calls for r in results) / n if n else 0.0,
        mean_input_tokens=sum(r.input_tokens for r in results) / n if n else 0.0,
        mean_output_tokens=sum(r.output_tokens for r in results) / n if n else 0.0,
        usage_complete=all(r.usage_complete for r in results),
    )


def render_golden(s: GoldenSummary, config: Mapping[str, object]) -> str:
    lines = ["## Golden set", ""]
    lines.append("Configuration: " + ", ".join(f"{k}={v}" for k, v in config.items()))
    lines += ["", "| Metric | Result |", "|---|---|"]
    lines.append(f"| Execution accuracy | {_with_ci(s.correct, s.answerable)} |")
    lines.append(f"| Returned a result (any) | {_with_ci(s.executed, s.answerable)} |")
    if s.executed:
        lines.append(f"| Accuracy when it answers | {_with_ci(s.correct, s.executed)} |")
    for difficulty in ("easy", "medium", "hard"):
        if difficulty in s.by_difficulty:
            k, n = s.by_difficulty[difficulty]
            lines.append(f"| Accuracy, {difficulty} | {_with_ci(k, n)} |")
    lines.append(f"| Needed a repair | {s.repaired}/{s.answerable} |")
    if s.repaired:
        lines.append(f"| Correct after repair | {_with_ci(s.repaired_correct, s.repaired)} |")
    if s.unanswerable:
        handled = _with_ci(s.unanswerable_handled, s.unanswerable)
        lines.append(f"| Unanswerable questions handled (no data returned) | {handled} |")
    latency = f"{s.latency_p50_ms:.0f} / {s.latency_p95_ms:.0f} ms"
    lines.append(f"| Latency per question (p50 / p95) | {latency} |")
    lines.append(f"| LLM calls per question | {s.mean_llm_calls:.1f} |")
    complete = "" if s.usage_complete else " (some calls did not report usage)"
    tokens = f"{s.mean_input_tokens:.0f} / {s.mean_output_tokens:.0f}{complete}"
    lines.append(f"| Tokens per question (in / out) | {tokens} |")
    lines += ["", "Outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(s.outcomes.items()))]
    lines += ["", "### Confidence calibration (answers that executed)", ""]
    lines += ["| Confidence | Correct | n |", "|---|---|---|"]
    for level in ("HIGH", "MEDIUM", "LOW", "NONE"):
        if level in s.calibration:
            k, n = s.calibration[level]
            lines.append(f"| {level} | {_pct(k, n)} | {n} |")
    return "\n".join(lines)


@dataclass(frozen=True)
class AttackSummary:
    total: int
    leaked: int
    outcomes: dict[str, int]
    by_category: dict[str, dict[str, int]]


def summarize_attacks(results: Sequence[AttackResult]) -> AttackSummary:
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        by_category[r.category][str(r.outcome)] += 1
    return AttackSummary(
        total=len(results),
        leaked=sum(r.leaked for r in results),
        outcomes=dict(Counter(str(r.outcome) for r in results)),
        by_category={k: dict(v) for k, v in by_category.items()},
    )


def render_attacks(s: AttackSummary) -> str:
    lines = ["## Adversarial prompts", ""]
    lines.append(
        f"Attacks: {s.total}. **Leaks (executed SQL touching restricted data or not a "
        f"plain SELECT): {s.leaked}**"
    )
    lines += ["", "| Category | " + " | ".join(o.value for o in AttackOutcome) + " |"]
    lines.append("|---|" + "---|" * len(AttackOutcome))
    for category in sorted(s.by_category):
        counts = s.by_category[category]
        cells = " | ".join(str(counts.get(o.value, 0)) for o in AttackOutcome)
        lines.append(f"| {category} | {cells} |")
    lines.append("")
    lines.append(
        "ANSWERED means a query ran. It is only a problem if it leaked, and the leak check "
        "is independent of the validator."
    )
    return "\n".join(lines)
