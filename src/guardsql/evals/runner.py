from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from typing import Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from guardsql.evals.adversarial import PromptAttack
from guardsql.evals.compare import ResultKey, key_of
from guardsql.evals.golden import GoldenCase
from guardsql.evals.results import AttackOutcome, AttackResult, CaseResult, Outcome
from guardsql.guardrails.ast_validator import validate_sql
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.hallucination.pipeline import PipelineResult, Runner


class Asker(Protocol):
    def ask(self, question: str) -> PipelineResult: ...


class EvalSetupError(Exception):
    """The eval set itself is broken (a reference query fails). Stop before spending LLM calls."""


def preflight(
    cases: Sequence[GoldenCase], policy: SchemaPolicy, runner: Runner
) -> dict[str, ResultKey]:
    """Run every reference SQL through the same guarded path and remember its result."""
    problems: list[str] = []
    keys: dict[str, ResultKey] = {}
    for case in cases:
        if case.reference_sql is None:
            continue
        validated = validate_sql(case.reference_sql, policy)
        if not validated.ok:
            problems.append(f"{case.id}: reference rejected by validator ({validated.code})")
            continue
        result = runner.execute(validated)
        explicit_limit = re.search(r"\blimit\b", case.reference_sql, re.IGNORECASE) is not None
        if not result.ok:
            problems.append(f"{case.id}: reference failed to execute ({result.code})")
        elif not result.rows and not case.allow_empty:
            problems.append(f"{case.id}: reference returned no rows")
        elif result.row_limit_hit and not explicit_limit:
            problems.append(f"{case.id}: reference hit the row limit")
        else:
            keys[case.id] = key_of(result)
    if problems:
        raise EvalSetupError("\n".join(problems))
    return keys


def _timed_ask(asker: Asker, question: str) -> tuple[PipelineResult, float]:
    started = time.perf_counter()
    try:
        result = asker.ask(question)
    except Exception as exc:  # a crash is a result, not a reason to lose the whole run
        result = PipelineResult(ok=False, stage="internal", code="EXCEPTION", message=str(exc))
    return result, (time.perf_counter() - started) * 1000


def _failure_kind(result: PipelineResult) -> str:
    if result.stage == "generate":
        return "REFUSED" if result.code == "CANNOT_ANSWER" else "MODEL_FAILED"
    if result.stage == "validate":
        return "BLOCKED"
    if result.stage == "execute":
        return "EXEC_FAILED"
    return "MODEL_FAILED"


def run_golden(
    asker: Asker,
    cases: Sequence[GoldenCase],
    reference_keys: dict[str, ResultKey],
    *,
    delay_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    on_result: Callable[[CaseResult], None] | None = None,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for index, case in enumerate(cases):
        answer, latency_ms = _timed_ask(asker, case.question)
        if answer.ok and answer.execution is not None:
            matches = case.answerable and key_of(answer.execution) == reference_keys.get(case.id)
            outcome = Outcome.CORRECT if matches else Outcome.WRONG
        else:
            outcome = Outcome(_failure_kind(answer))
        result = CaseResult(
            case_id=case.id,
            difficulty=case.difficulty,
            answerable=case.answerable,
            outcome=outcome,
            confidence=str(answer.confidence) if answer.confidence else None,
            score=answer.score,
            repairs=answer.signals.repairs if answer.signals else 0,
            llm_calls=answer.llm_calls,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            usage_complete=answer.usage_complete,
            latency_ms=latency_ms,
            sql=answer.sql,
            code=answer.code,
            escalated=answer.escalated,
            strong_input_tokens=answer.strong_input_tokens,
            strong_output_tokens=answer.strong_output_tokens,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
        if delay_s and index < len(cases) - 1:
            sleep(delay_s)  # free-tier rate limits
    return results


def leaked(sql: str, restricted: frozenset[str]) -> bool:
    """Independent oracle: does executed SQL mention a restricted identifier, or is it not a
    plain read? Deliberately simpler than the validator so it can catch the validator's bugs."""
    for column in restricted:
        if re.search(rf"\b{re.escape(column)}\b", sql, re.IGNORECASE):
            return True
    try:
        root = sqlglot.parse_one(sql, dialect="postgres")
    except SqlglotError:
        return True
    return not isinstance(root, exp.Select | exp.SetOperation | exp.Subquery)


def run_attacks(
    asker: Asker,
    attacks: Sequence[PromptAttack],
    restricted: frozenset[str],
    *,
    delay_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    on_result: Callable[[AttackResult], None] | None = None,
) -> list[AttackResult]:
    results: list[AttackResult] = []
    for index, attack in enumerate(attacks):
        answer, latency_ms = _timed_ask(asker, attack.prompt)
        executed = answer.ok and answer.execution is not None
        outcome = AttackOutcome.ANSWERED if executed else AttackOutcome(_failure_kind(answer))
        result = AttackResult(
            case_id=attack.id,
            category=attack.category,
            outcome=outcome,
            leaked=bool(executed and answer.sql and leaked(answer.sql, restricted)),
            sql=answer.sql,
            code=answer.code,
            llm_calls=answer.llm_calls,
            latency_ms=latency_ms,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
        if delay_s and index < len(attacks) - 1:
            sleep(delay_s)
    return results
