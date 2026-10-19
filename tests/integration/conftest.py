from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from guardsql.config import Settings
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.schema.introspect import introspect


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture
def ro_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def live_policy(settings: Settings) -> SchemaPolicy:
    """Policy built exactly the way the app builds it: introspection + settings."""
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        snapshot = introspect(conn, settings.postgres_schema)
    return snapshot.to_policy(settings.restricted_column_set, **settings.policy_limits)


@pytest.fixture
def make_executor(settings: Settings) -> Iterator[Callable[..., ReadOnlyExecutor]]:
    """Build executors with setting overrides; all are closed after the test."""
    created: list[ReadOnlyExecutor] = []

    def _make(**overrides: Any) -> ReadOnlyExecutor:
        executor = ReadOnlyExecutor(settings.model_copy(update=overrides))
        executor.open()
        created.append(executor)
        return executor

    yield _make
    for executor in created:
        executor.close()
