import pytest

from guardsql.evals.adversarial import PromptAttack
from guardsql.evals.compare import key_of
from guardsql.evals.golden import GoldenCase
from guardsql.evals.results import AttackOutcome, Outcome
from guardsql.evals.runner import EvalSetupError, leaked, preflight, run_attacks, run_golden
from guardsql.evals.seed_policy import RESTRICTED, SEED_POLICY
from guardsql.execution.executor import ExecCode, ExecutionResult
from guardsql.guardrails.ast_validator import ValidationResult
from guardsql.hallucination.confidence import Confidence, Signals
from guardsql.hallucination.pipeline import PipelineResult


def rows(n: int, cols: tuple[str, ...] = ("count",)) -> ExecutionResult:
    return ExecutionResult(ok=True, columns=cols, rows=((n,),))


class StubRunner:
    """Result chosen by the table named in the SQL."""

    def __init__(self, by_table: dict[str, ExecutionResult]) -> None:
        self.by_table = by_table

    def execute(self, validated: ValidationResult) -> ExecutionResult:
        assert validated.sql is not None
        for table, result in self.by_table.items():
            if table in validated.sql:
                return result
        raise AssertionError(validated.sql)


def answer(execution: ExecutionResult, sql: str = "SELECT 1", repairs: int = 0) -> PipelineResult:
    return PipelineResult(
        ok=True,
        sql=sql,
        execution=execution,
        confidence=Confidence.HIGH,
        score=0.85,
        signals=Signals(3, 3, repairs, False),
        llm_calls=3,
        input_tokens=100,
        output_tokens=10,
    )


def failure(stage: str, code: str) -> PipelineResult:
    return PipelineResult(ok=False, stage=stage, code=code, message="m")


class StubAsker:
    def __init__(self, by_question: dict[str, PipelineResult | Exception]) -> None:
        self.by_question = by_question

    def ask(self, question: str) -> PipelineResult:
        item = self.by_question[question]
        if isinstance(item, Exception):
            raise item
        return item


ORDERS = GoldenCase("a", "q-orders", "SELECT count(*) FROM orders", "easy")
UNANS = GoldenCase("u", "q-unanswerable", None, "unanswerable")


def keys_for(*cases: GoldenCase) -> dict:
    return preflight(cases, SEED_POLICY, StubRunner({"orders": rows(5)}))


def test_preflight_returns_reference_keys() -> None:
    assert keys_for(ORDERS, UNANS)["a"] == key_of(rows(5))


def test_preflight_reports_every_problem() -> None:
    bad_sql = GoldenCase("b", "q", "SELECT email FROM customers", "easy")
    empty = GoldenCase("c", "q", "SELECT count(*) FROM products", "easy")
    runner = StubRunner({"products": ExecutionResult(ok=True, columns=("c",), rows=())})
    with pytest.raises(EvalSetupError) as info:
        preflight([bad_sql, empty], SEED_POLICY, runner)
    message = str(info.value)
    assert "b: reference rejected" in message
    assert "c: reference returned no rows" in message


def test_preflight_allows_empty_when_declared() -> None:
    ok_empty = GoldenCase("c", "q", "SELECT count(*) FROM products", "easy", allow_empty=True)
    runner = StubRunner({"products": ExecutionResult(ok=True, columns=("c",), rows=())})
    assert "c" in preflight([ok_empty], SEED_POLICY, runner)


def test_correct_wrong_and_column_names_ignored() -> None:
    keys = keys_for(ORDERS)
    asker = StubAsker({"q-orders": answer(rows(5, ("n",)))})
    (result,) = run_golden(asker, [ORDERS], keys)
    assert result.outcome is Outcome.CORRECT
    asker = StubAsker({"q-orders": answer(rows(6))})
    (result,) = run_golden(asker, [ORDERS], keys)
    assert result.outcome is Outcome.WRONG


@pytest.mark.parametrize(
    ("stage", "code", "outcome"),
    [
        ("generate", "CANNOT_ANSWER", Outcome.REFUSED),
        ("generate", "LLM_ERROR", Outcome.MODEL_FAILED),
        ("generate", "NO_SQL", Outcome.MODEL_FAILED),
        ("validate", "UNKNOWN_TABLE", Outcome.BLOCKED),
        ("execute", ExecCode.COST_TOO_HIGH.value, Outcome.EXEC_FAILED),
        ("internal", "EXCEPTION", Outcome.MODEL_FAILED),
    ],
)
def test_failure_outcomes(stage: str, code: str, outcome: Outcome) -> None:
    asker = StubAsker({"q-orders": failure(stage, code)})
    (result,) = run_golden(asker, [ORDERS], keys_for(ORDERS))
    assert result.outcome is outcome


def test_a_crashing_asker_does_not_abort_the_run() -> None:
    asker = StubAsker({"q-orders": RuntimeError("boom")})
    (result,) = run_golden(asker, [ORDERS], keys_for(ORDERS))
    assert result.outcome is Outcome.MODEL_FAILED


def test_unanswerable_that_returns_data_is_wrong() -> None:
    asker = StubAsker({"q-unanswerable": answer(rows(1))})
    (result,) = run_golden(asker, [UNANS], {})
    assert result.outcome is Outcome.WRONG and not result.answerable


def test_repairs_and_usage_are_recorded() -> None:
    asker = StubAsker({"q-orders": answer(rows(5), repairs=2)})
    (result,) = run_golden(asker, [ORDERS], keys_for(ORDERS))
    assert result.repairs == 2 and result.llm_calls == 3 and result.input_tokens == 100


def test_delay_between_but_not_after_the_last() -> None:
    sleeps: list[float] = []
    asker = StubAsker({"q-orders": answer(rows(5))})
    run_golden(asker, [ORDERS, ORDERS, ORDERS], keys_for(ORDERS), delay_s=2.0, sleep=sleeps.append)
    assert sleeps == [2.0, 2.0]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT first_name FROM customers", False),
        ("SELECT email FROM customers", True),
        ("SELECT count(*) AS salary FROM employees", True),  # conservative on purpose
        ("DELETE FROM orders", True),
        ("SELECT 1 FROM", True),  # unparseable counts as a leak
        ("SELECT id FROM orders UNION SELECT id FROM customers", False),
    ],
)
def test_leak_oracle(sql: str, expected: bool) -> None:
    assert leaked(sql, RESTRICTED) is expected


def test_attack_outcomes_and_leak_flag() -> None:
    attacks = [
        PromptAttack("1", "pii", "refused"),
        PromptAttack("2", "pii", "blocked"),
        PromptAttack("3", "inj", "harmless"),
        PromptAttack("4", "inj", "leaky"),
    ]
    asker = StubAsker(
        {
            "refused": failure("generate", "CANNOT_ANSWER"),
            "blocked": failure("validate", "RESTRICTED_COLUMN"),
            "harmless": answer(rows(1), sql="SELECT count(*) FROM orders"),
            "leaky": answer(rows(1), sql="SELECT email FROM customers"),
        }
    )
    results = run_attacks(asker, attacks, RESTRICTED)
    assert [r.outcome for r in results] == [
        AttackOutcome.REFUSED,
        AttackOutcome.BLOCKED,
        AttackOutcome.ANSWERED,
        AttackOutcome.ANSWERED,
    ]
    assert [r.leaked for r in results] == [False, False, False, True]


def test_preflight_accepts_a_full_result_when_the_reference_has_its_own_limit() -> None:
    case = GoldenCase("t", "q", "SELECT id FROM orders ORDER BY id LIMIT 5", "easy")
    full = ExecutionResult(ok=True, columns=("id",), rows=((1,),), row_limit_hit=True)
    assert "t" in preflight([case], SEED_POLICY, StubRunner({"orders": full}))


def test_preflight_rejects_a_truncated_reference_without_a_limit() -> None:
    case = GoldenCase("t", "q", "SELECT id FROM orders", "easy")
    full = ExecutionResult(ok=True, columns=("id",), rows=((1,),), row_limit_hit=True)
    with pytest.raises(EvalSetupError, match="row limit"):
        preflight([case], SEED_POLICY, StubRunner({"orders": full}))
