import pytest
from pydantic import ValidationError

from guardsql.config import Settings


def test_sslmode_defaults_to_prefer_and_reaches_the_connection_string() -> None:
    settings = Settings(_env_file=None)
    assert settings.postgres_sslmode == "prefer"
    assert "sslmode=prefer" in settings.readonly_conninfo


def test_require_is_passed_through() -> None:
    settings = Settings(_env_file=None, postgres_sslmode="require")
    assert "sslmode=require" in settings.readonly_conninfo


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_sslmode="sometimes")  # type: ignore[arg-type]
