from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    CORRECT = "CORRECT"  # executed, and the result matches the reference
    WRONG = "WRONG"  # executed, but the result differs (or the question had no answer)
    REFUSED = "REFUSED"  # the model said CANNOT_ANSWER
    BLOCKED = "BLOCKED"  # the validator rejected the SQL (after any repairs)
    EXEC_FAILED = "EXEC_FAILED"  # cost gate, timeout or database error
    MODEL_FAILED = "MODEL_FAILED"  # LLM outage or no SQL in the reply


class AttackOutcome(StrEnum):
    ANSWERED = "ANSWERED"  # a query ran (it may be harmless; see `leaked`)
    REFUSED = "REFUSED"
    BLOCKED = "BLOCKED"
    EXEC_FAILED = "EXEC_FAILED"
    MODEL_FAILED = "MODEL_FAILED"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    difficulty: str
    answerable: bool
    outcome: Outcome
    confidence: str | None
    score: float | None
    repairs: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    usage_complete: bool
    latency_ms: float
    sql: str | None
    code: str | None
    escalated: bool = False
    strong_input_tokens: int = 0
    strong_output_tokens: int = 0


@dataclass(frozen=True)
class AttackResult:
    case_id: str
    category: str
    outcome: AttackOutcome
    # True if an executed query touched a restricted identifier or was not a plain SELECT.
    leaked: bool
    sql: str | None
    code: str | None
    llm_calls: int
    latency_ms: float
