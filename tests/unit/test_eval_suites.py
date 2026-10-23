import pytest

from guardsql.evals.adversarial import (
    CONTROLS,
    EXECUTOR_PAYLOADS,
    PROMPT_ATTACKS,
    ROBUSTNESS_INPUTS,
    SQL_PAYLOADS,
)
from guardsql.evals.golden import GOLDEN
from guardsql.evals.seed_policy import RESTRICTED, SEED_POLICY
from guardsql.guardrails.ast_validator import ValidationResult, validate_sql


def test_ids_are_unique() -> None:
    for suite in (GOLDEN, SQL_PAYLOADS, CONTROLS, PROMPT_ATTACKS, EXECUTOR_PAYLOADS):
        ids = [item.id for item in suite]
        assert len(ids) == len(set(ids))


def test_suite_sizes() -> None:
    answerable = [c for c in GOLDEN if c.answerable]
    assert len(answerable) >= 40
    assert len([c for c in GOLDEN if not c.answerable]) >= 3
    assert len(SQL_PAYLOADS) >= 50
    assert len(PROMPT_ATTACKS) >= 20


@pytest.mark.parametrize("case", [c for c in GOLDEN if c.answerable], ids=lambda c: c.id)
def test_every_reference_passes_the_validator(case) -> None:  # type: ignore[no-untyped-def]
    assert case.reference_sql is not None
    result = validate_sql(case.reference_sql, SEED_POLICY)
    assert result.ok, f"{case.id}: {result.code} {result.message}"


@pytest.mark.parametrize("payload", SQL_PAYLOADS, ids=lambda p: p.id)
def test_hostile_sql_is_rejected(payload) -> None:  # type: ignore[no-untyped-def]
    result = validate_sql(payload.sql, SEED_POLICY)
    assert not result.ok, f"{payload.id} was ACCEPTED: {result.sql}"
    assert result.code in payload.expect, f"{payload.id}: got {result.code}, {result.message}"


@pytest.mark.parametrize("control", CONTROLS, ids=lambda c: c.id)
def test_benign_sql_is_accepted_and_clean(control) -> None:  # type: ignore[no-untyped-def]
    result = validate_sql(control.sql, SEED_POLICY)
    assert result.ok, f"{control.id} falsely rejected: {result.code} {result.message}"
    assert result.sql is not None
    for forbidden in control.absent:
        assert forbidden.lower() not in result.sql.lower()
    if control.expect_limit is not None:
        assert result.limit == control.expect_limit


@pytest.mark.parametrize("text", ROBUSTNESS_INPUTS, ids=range(len(ROBUSTNESS_INPUTS)))
def test_validator_never_raises_and_accepted_output_is_safe(text: str) -> None:
    result = validate_sql(text, SEED_POLICY)
    assert isinstance(result, ValidationResult)
    if result.ok:
        assert result.sql is not None
        assert not any(c in result.sql.lower() for c in RESTRICTED)
        assert "drop" not in result.sql.lower()


def test_prompt_attacks_cover_the_main_categories() -> None:
    assert {a.category for a in PROMPT_ATTACKS} >= {"pii", "injection", "write", "system", "dos"}


def test_executor_payloads_pass_the_validator() -> None:
    for payload in EXECUTOR_PAYLOADS:
        assert validate_sql(payload.sql, SEED_POLICY).ok, payload.id


def test_and_or_are_operators_not_functions() -> None:
    result = validate_sql("SELECT id FROM orders WHERE id > 1 AND id < 5 OR id = 9", SEED_POLICY)
    assert result.ok, f"{result.code} {result.message}"
