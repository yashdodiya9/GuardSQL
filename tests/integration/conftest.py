from collections.abc import Iterator

import psycopg
import pytest

from guardsql.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture
def ro_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        yield conn
