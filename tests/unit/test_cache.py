import types

import pytest

from guardsql import cache as cache_module
from guardsql.cache import CachingPipeline, cache_fingerprint, normalize_question
from guardsql.execution.executor import ExecCode, ExecutionResult
from guardsql.hallucination.confidence import Confidence, Signals
from guardsql.hallucination.pipeline import PipelineResult


class Script:
    """Hands out items in order, then repeats the last one."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls: list[str] = []

    def _next(self):
        return self.items.pop(0) if len(self.items) > 1 else self.items[0]


class Inner(Script):
    def ask(self, question: str) -> PipelineResult:
        self.calls.append(question)
        return self._next()


class Runner(Script):
    def execute(self, validated) -> ExecutionResult:
        self.calls.append(validated.sql)
        return self._next()


def execution(value: int | None) -> ExecutionResult:
    return ExecutionResult(ok=True, columns=("c",), rows=((value,),))


def good(
    value: int | None = 1, *, confidence: Confidence = Confidence.MEDIUM, empty: bool = False
) -> PipelineResult:
    rows = () if empty else ((value,),)
    return PipelineResult(
        ok=True,
        sql="SELECT 1",
        execution=ExecutionResult(ok=True, columns=("c",), rows=rows),
        confidence=confidence,
        score=0.55,
        signals=Signals(1, 1, 0, empty),
        llm_calls=1,
        input_tokens=400,
        output_tokens=30,
    )


def failed() -> PipelineResult:
    return PipelineResult(ok=False, stage="validate", code="UNKNOWN_TABLE", message="m")


DB_ERROR = ExecutionResult(ok=False, code=ExecCode.DATABASE_ERROR, message="m")


def accept(sql, policy):
    return types.SimpleNamespace(ok=True, sql=sql, code=None, message=None)


def reject(sql, policy):
    return types.SimpleNamespace(ok=False, sql=None, code="RESTRICTED_COLUMN", message="m")


@pytest.fixture(autouse=True)
def allow_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_module, "validate_sql", accept)


def make(inner, runner, **kwargs) -> CachingPipeline:
    return CachingPipeline(inner, runner, object(), fingerprint="fp", **kwargs)


def test_normalization_ignores_case_spacing_and_trailing_punctuation() -> None:
    assert normalize_question("  How MANY   orders?? ") == normalize_question("how many orders")
    assert normalize_question("orders by status") != normalize_question("orders by country")
    assert normalize_question("how many orders ?") == normalize_question("how many orders")


def test_repeat_question_is_a_hit_with_fresh_rows_and_no_llm() -> None:
    inner, runner = Inner(good(1)), Runner(execution(2))
    pipe = make(inner, runner)
    first = pipe.ask("How many orders?")
    second = pipe.ask("how many   orders")
    assert first.execution is not None and first.execution.rows == ((1,),)
    assert second.cache_hit and not first.cache_hit
    assert second.execution is not None and second.execution.rows == ((2,),)
    assert (second.llm_calls, second.input_tokens, second.output_tokens) == (0, 0, 0)
    assert second.confidence is Confidence.MEDIUM
    assert len(inner.calls) == 1 and runner.calls == ["SELECT 1"]
    assert pipe.stats() == {"hits": 1, "misses": 1, "size": 1}


@pytest.mark.parametrize(
    "result",
    [
        failed(),
        good(empty=True),
        good(None),
        good(confidence=Confidence.LOW),
    ],
    ids=["failure", "empty", "all-null", "low-confidence"],
)
def test_only_healthy_answers_are_cached(result: PipelineResult) -> None:
    inner = Inner(result)
    pipe = make(inner, Runner(execution(1)))
    pipe.ask("q")
    pipe.ask("q")
    assert len(inner.calls) == 2


def test_relative_time_questions_are_never_cached() -> None:
    inner = Inner(good())
    pipe = make(inner, Runner(execution(1)))
    pipe.ask("orders placed today")
    pipe.ask("orders placed today")
    assert len(inner.calls) == 2


def test_replay_database_error_falls_through_to_the_pipeline() -> None:
    inner = Inner(good(1), good(3))
    pipe = make(inner, Runner(DB_ERROR))
    pipe.ask("q")
    second = pipe.ask("q")
    assert not second.cache_hit
    assert second.execution is not None and second.execution.rows == ((3,),)
    assert len(inner.calls) == 2


def test_replay_empty_result_falls_through() -> None:
    inner = Inner(good(1), good(3))
    pipe = make(inner, Runner(ExecutionResult(ok=True, columns=("c",), rows=())))
    pipe.ask("q")
    assert not pipe.ask("q").cache_hit
    assert len(inner.calls) == 2


def test_a_stricter_validator_blocks_a_cached_query(monkeypatch: pytest.MonkeyPatch) -> None:
    inner, runner = Inner(good(1)), Runner(execution(1))
    pipe = make(inner, runner)
    pipe.ask("q")
    monkeypatch.setattr(cache_module, "validate_sql", reject)
    second = pipe.ask("q")
    assert not second.cache_hit
    assert len(inner.calls) == 2
    assert runner.calls == []  # nothing was executed for the rejected replay


def test_entries_expire() -> None:
    now = [0.0]
    inner = Inner(good())
    pipe = make(inner, Runner(execution(1)), ttl_seconds=10, clock=lambda: now[0])
    pipe.ask("q")
    now[0] = 5
    assert pipe.ask("q").cache_hit
    now[0] = 11
    assert not pipe.ask("q").cache_hit
    assert len(inner.calls) == 2


def test_least_recently_used_entry_is_evicted() -> None:
    inner = Inner(good())
    pipe = make(inner, Runner(execution(1)), max_entries=2)
    for question in ("a", "b", "c"):
        pipe.ask(question)
    assert len(inner.calls) == 3
    assert pipe.ask("b").cache_hit  # still cached
    assert not pipe.ask("a").cache_hit  # was evicted
    assert len(inner.calls) == 4


def test_fingerprint_depends_on_every_part_and_their_boundaries() -> None:
    assert cache_fingerprint("prompt", "7b") == cache_fingerprint("prompt", "7b")
    assert cache_fingerprint("prompt", "7b") != cache_fingerprint("prompt", "14b")
    assert cache_fingerprint("ab", "c") != cache_fingerprint("a", "bc")
