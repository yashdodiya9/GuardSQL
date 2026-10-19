import pytest
from pydantic import ValidationError

from guardsql.config import Settings


def make(**overrides: object) -> Settings:
    # _env_file=None: unit tests must not depend on a developer's local .env
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_restricted_columns_are_trimmed_lowercased_and_deduplicated() -> None:
    settings = make(restricted_columns=" Email, PHONE ,,salary,email")
    assert settings.restricted_column_set == {"email", "phone", "salary"}


def test_empty_restricted_columns_means_none() -> None:
    assert make(restricted_columns="").restricted_column_set == frozenset()


def test_policy_limits_never_default_above_max() -> None:
    limits = make(default_row_limit=500, max_row_limit=200).policy_limits
    assert limits["default_limit"] == 200 and limits["max_limit"] == 200


def test_pool_cannot_exceed_the_roles_connection_limit() -> None:
    with pytest.raises(ValidationError):
        make(pool_max_size=11)


@pytest.mark.parametrize(
    "overrides",
    [{"max_plan_cost": 0}, {"statement_timeout_ms": 0}, {"default_row_limit": 0}],
)
def test_nonsense_limits_fail_at_startup(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**overrides)
