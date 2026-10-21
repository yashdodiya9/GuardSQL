"""Turn independent signals into one score, with weights that are visible and testable.

The weights are heuristics. M8 calibrates them against the golden set; until then do not
quote them as probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class JudgeVerdict(StrEnum):
    YES = "YES"
    NO = "NO"
    UNSURE = "UNSURE"


@dataclass(frozen=True)
class Signals:
    samples_requested: int
    # Counts the primary answer too, so the minimum is 1.
    samples_agreeing: int
    repairs: int
    empty_result: bool
    judge: JudgeVerdict | None = None


@dataclass(frozen=True)
class ConfidenceConfig:
    base: float = 0.40  # validated and executed
    agreement_weight: float = 0.45
    unsampled_bonus: float = 0.15  # one sample can never prove agreement
    repair_penalty: float = 0.15
    max_repairs_counted: int = 2
    empty_penalty: float = 0.10
    judge_yes: float = 0.15
    judge_no: float = -0.35
    high: float = 0.80
    medium: float = 0.50


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)


def base_score(signals: Signals, cfg: ConfidenceConfig) -> float:
    """Score from the signals that cost nothing extra (everything except the judge)."""
    total = cfg.base
    if signals.samples_requested > 1:
        total += cfg.agreement_weight * (signals.samples_agreeing / signals.samples_requested)
    else:
        total += cfg.unsampled_bonus
    total -= cfg.repair_penalty * min(signals.repairs, cfg.max_repairs_counted)
    if signals.empty_result:
        total -= cfg.empty_penalty
    return _clamp(total)


def apply_judge(score: float, verdict: JudgeVerdict | None, cfg: ConfidenceConfig) -> float:
    """A NO always counts. A YES can raise the score but never into HIGH on its own: the
    question is untrusted input and can talk a judge into agreeing."""
    if verdict is JudgeVerdict.NO:
        return _clamp(score + cfg.judge_no)
    if verdict is JudgeVerdict.YES:
        raised = score + cfg.judge_yes
        if score < cfg.high:
            raised = min(raised, cfg.high - 0.01)
        return _clamp(raised)
    return score


def level(score: float, cfg: ConfidenceConfig) -> Confidence:
    if score >= cfg.high:
        return Confidence.HIGH
    if score >= cfg.medium:
        return Confidence.MEDIUM
    return Confidence.LOW
