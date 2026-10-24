import pytest

from guardsql.execution.executor import ExecutionResult
from guardsql.hallucination.confidence import Confidence, Signals
from guardsql.hallucination.pipeline import PipelineResult
from guardsql.routing import EscalatingPipeline, escalation_reason


class Stub:
    def __init__(self, result: PipelineResult) -> None:
        self.result = result
        self.calls = 0

    def ask(self, question: str) -> PipelineResult:
        self.calls += 1
        return self.result


def good(*, empty: bool = False, calls: int = 1, tin: int = 100, tout: int = 10) -> PipelineResult:
    execution = ExecutionResult(ok=True, columns=("c",), rows=() if empty else ((5,),))
    return PipelineResult(
        ok=True,
        sql="SELECT 1",
        execution=execution,
        confidence=Confidence.MEDIUM,
        score=0.55,
        signals=Signals(1, 1, 0, empty),
        llm_calls=calls,
        input_tokens=tin,
        output_tokens=tout,
    )


def failed(
    stage: str, code: str, *, calls: int = 1, tin: int = 100, tout: int = 10
) -> PipelineResult:
    return PipelineResult(
        ok=False,
        stage=stage,
        code=code,
        message="m",
        llm_calls=calls,
        input_tokens=tin,
        output_tokens=tout,
    )


@pytest.mark.parametrize(
    ("stage", "code", "reason"),
    [
        ("validate", "UNKNOWN_TABLE", "validate:UNKNOWN_TABLE"),
        ("validate", "UNKNOWN_COLUMN", "validate:UNKNOWN_COLUMN"),
        ("validate", "FUNCTION_NOT_ALLOWED", "validate:FUNCTION_NOT_ALLOWED"),
        ("execute", "DATABASE_ERROR", "execute:DATABASE_ERROR"),
    ],
)
def test_honest_mistakes_escalate(stage: str, code: str, reason: str) -> None:
    assert escalation_reason(failed(stage, code)) == reason


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        ("validate", "RESTRICTED_COLUMN"),
        ("validate", "NOT_SELECT"),
        ("validate", "FORBIDDEN_CONSTRUCT"),
        ("validate", "MULTIPLE_STATEMENTS"),
        ("generate", "CANNOT_ANSWER"),
        ("generate", "LLM_ERROR"),
        ("generate", "NO_SQL"),
        ("execute", "COST_TOO_HIGH"),
        ("execute", "TIMEOUT"),
        ("execute", "UNAVAILABLE"),
    ],
)
def test_attacks_refusals_and_infrastructure_failures_never_escalate(stage: str, code: str) -> None:
    assert escalation_reason(failed(stage, code)) is None


def test_empty_result_escalates_but_a_normal_answer_does_not() -> None:
    assert escalation_reason(good(empty=True)) == "empty_result"
    assert escalation_reason(good()) is None


def test_confident_cheap_answer_never_calls_the_strong_model() -> None:
    primary, strong = Stub(good()), Stub(good())
    out = EscalatingPipeline(primary, strong).ask("q")
    assert not out.escalated and strong.calls == 0


def test_escalation_returns_the_strong_answer_and_sums_usage() -> None:
    primary = Stub(failed("validate", "UNKNOWN_TABLE", calls=3, tin=300, tout=30))
    strong = Stub(good(calls=1, tin=500, tout=20))
    out = EscalatingPipeline(primary, strong).ask("q")
    assert out.ok and out.escalated
    assert out.escalation_reason == "validate:UNKNOWN_TABLE"
    assert (out.llm_calls, out.input_tokens, out.output_tokens) == (4, 800, 50)
    assert (out.strong_calls, out.strong_input_tokens, out.strong_output_tokens) == (1, 500, 20)


def test_empty_cheap_answer_stands_if_the_strong_model_fails() -> None:
    primary = Stub(good(empty=True))
    strong = Stub(failed("generate", "LLM_ERROR"))
    out = EscalatingPipeline(primary, strong).ask("q")
    assert out.ok and out.escalated
    assert out.execution is not None and out.execution.rows == ()


def test_both_failing_reports_the_strong_failure() -> None:
    primary = Stub(failed("validate", "UNKNOWN_TABLE"))
    strong = Stub(failed("validate", "UNKNOWN_COLUMN"))
    out = EscalatingPipeline(primary, strong).ask("q")
    assert not out.ok and out.code == "UNKNOWN_COLUMN" and out.escalated
