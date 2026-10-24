"""question -> generate -> validate (repair) -> execute -> sample -> score -> maybe judge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from guardsql.config import Settings
from guardsql.execution.executor import ExecCode, ExecutionResult
from guardsql.guardrails.ast_validator import Code, ValidationResult, validate_sql
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.hallucination.confidence import (
    Confidence,
    ConfidenceConfig,
    Signals,
    apply_judge,
    base_score,
    level,
)
from guardsql.hallucination.consistency import fingerprint
from guardsql.hallucination.judge import SQLJudge
from guardsql.llm.generator import GenCode, SQLGenerator
from guardsql.llm.types import LLMResponse

# Rejections that are plausibly honest model mistakes. Everything else is final: a model
# that wrote DML, several statements or a PII column gets no second chance to "work around".
REPAIRABLE: frozenset[Code] = frozenset(
    {
        Code.PARSE_ERROR,
        Code.UNKNOWN_TABLE,
        Code.UNKNOWN_COLUMN,
        Code.SCHEMA_NOT_ALLOWED,
        Code.FUNCTION_NOT_ALLOWED,
        Code.UNANALYZABLE,
    }
)


class Runner(Protocol):
    def execute(self, validated: ValidationResult) -> ExecutionResult: ...


class Asker(Protocol):
    def ask(self, question: str) -> PipelineResult: ...


@dataclass(frozen=True)
class PipelineConfig:
    samples: int = 3
    sample_temperature: float = 0.7
    max_repairs: int = 2
    # The judge runs only when judge_low <= score < confidence.high.
    judge_low: float = 0.30
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)

    @classmethod
    def from_settings(cls, settings: Settings) -> PipelineConfig:
        return cls(
            samples=settings.consistency_samples,
            sample_temperature=settings.sample_temperature,
            max_repairs=settings.max_repairs,
        )


@dataclass(frozen=True)
class PipelineResult:
    ok: bool
    sql: str | None = None
    execution: ExecutionResult | None = None
    confidence: Confidence | None = None
    score: float | None = None
    signals: Signals | None = None
    # Failure details: which stage said no, and the stage's own code and message.
    stage: str | None = None
    code: str | None = None
    message: str | None = None
    # Cost accounting, used by M9. Failed LLM calls (no response) are not counted.
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_complete: bool = True
    escalated: bool = False
    escalation_reason: str | None = None
    strong_calls: int = 0
    strong_input_tokens: int = 0
    strong_output_tokens: int = 0
    cache_hit: bool = False


@dataclass
class _Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    complete: bool = True

    def add(self, response: LLMResponse | None) -> None:
        if response is None:
            return
        self.calls += response.attempts
        if response.input_tokens is None or response.output_tokens is None:
            self.complete = False
        self.input_tokens += response.input_tokens or 0
        self.output_tokens += response.output_tokens or 0


def _feedback(bad_sql: str, validation: ValidationResult) -> str:
    detail = f"{validation.code}: {validation.message}"[:300]
    return (
        f"Your previous query was:\n```sql\n{bad_sql}\n```\n"
        f"It was rejected: {detail}\n"
        "Write a corrected query using only tables and columns from the schema. "
        "Reply in the same format."
    )


def _db_feedback(sql: str, result: ExecutionResult) -> str:
    detail = (result.message or "database error")[:300]
    return (
        f"Your previous query was:\n```sql\n{sql}\n```\n"
        f"PostgreSQL rejected it when it ran: {detail}\n"
        "Write a corrected query using only tables and columns from the schema. "
        "Reply in the same format."
    )


def _is_empty(execution: ExecutionResult) -> bool:
    """No rows, or the single all-NULL row that aggregates return over zero rows."""
    if not execution.rows:
        return True
    return len(execution.rows) == 1 and all(value is None for value in execution.rows[0])


class AnswerPipeline:
    def __init__(
        self,
        generator: SQLGenerator,
        runner: Runner,
        policy: SchemaPolicy,
        *,
        config: PipelineConfig | None = None,
        judge: SQLJudge | None = None,
    ) -> None:
        self._gen = generator
        self._runner = runner
        self._policy = policy
        self._cfg = config or PipelineConfig()
        self._judge = judge

    def ask(self, question: str) -> PipelineResult:
        cfg = self._cfg
        usage = _Usage()

        def fail(
            stage: str, code: object, message: str | None, sql: str | None = None
        ) -> PipelineResult:
            return PipelineResult(
                ok=False,
                sql=sql,
                stage=stage,
                code=str(code),
                message=message,
                llm_calls=usage.calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                usage_complete=usage.complete,
            )

        # 1. generate, validate, execute; repair honest mistakes (validator or database)
        gen = self._gen.generate(question)
        usage.add(gen.response)
        repairs = 0
        while True:
            if not gen.ok or gen.sql is None:
                return fail("generate", gen.code, gen.message)
            validation = validate_sql(gen.sql, self._policy)
            if not validation.ok or validation.sql is None:
                if validation.code in REPAIRABLE and repairs < cfg.max_repairs:
                    repairs += 1
                    gen = self._gen.generate(question, feedback=_feedback(gen.sql, validation))
                    usage.add(gen.response)
                    continue
                return fail("validate", validation.code, validation.message, gen.sql)
            primary = self._runner.execute(validation)
            if primary.ok:
                break
            if primary.code is ExecCode.DATABASE_ERROR and repairs < cfg.max_repairs:
                repairs += 1
                gen = self._gen.generate(question, feedback=_db_feedback(validation.sql, primary))
                usage.add(gen.response)
                continue
            return fail("execute", primary.code, primary.message, validation.sql)
        primary_fp = fingerprint(primary)

        # 3. self-consistency: do independent samples produce the same result?
        agreeing = 1
        if cfg.samples > 1:
            seen: dict[str, str | None] = {validation.sql: primary_fp}
            for _ in range(cfg.samples - 1):
                sample = self._gen.generate(question, temperature=cfg.sample_temperature)
                usage.add(sample.response)
                if sample.code is GenCode.LLM_ERROR:
                    break  # do not hammer a provider that is failing
                if not sample.ok or sample.sql is None:
                    continue
                checked = validate_sql(sample.sql, self._policy)
                if not checked.ok or checked.sql is None:
                    continue
                if checked.sql not in seen:
                    seen[checked.sql] = fingerprint(self._runner.execute(checked))
                fp = seen[checked.sql]
                if fp is not None and fp == primary_fp:
                    agreeing += 1

        # 4. score, then the judge only if the answer is in the grey zone
        signals = Signals(
            samples_requested=cfg.samples,
            samples_agreeing=agreeing,
            repairs=repairs,
            empty_result=_is_empty(primary),
        )
        pre = base_score(signals, cfg.confidence)
        verdict = None
        if self._judge is not None and cfg.judge_low <= pre < cfg.confidence.high:
            outcome = self._judge.judge(question, validation.sql, primary.columns)
            usage.add(outcome.response)
            verdict = outcome.verdict
        score = apply_judge(pre, verdict, cfg.confidence)
        final = Signals(
            samples_requested=signals.samples_requested,
            samples_agreeing=signals.samples_agreeing,
            repairs=signals.repairs,
            empty_result=signals.empty_result,
            judge=verdict,
        )
        return PipelineResult(
            ok=True,
            sql=validation.sql,
            execution=primary,
            confidence=level(score, cfg.confidence),
            score=score,
            signals=final,
            llm_calls=usage.calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usage_complete=usage.complete,
        )
