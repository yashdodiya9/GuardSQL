from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from guardsql.api.app import create_app
from guardsql.api.security import ClientAuth, DailyQuota, RateLimiter, hash_key
from guardsql.audit import MemoryAuditLog
from guardsql.config import Settings
from guardsql.execution.executor import ExecutionResult
from guardsql.hallucination.confidence import Confidence, Signals
from guardsql.hallucination.pipeline import PipelineResult

ALICE, BOB = "gsql_alice-quota-key", "gsql_bob-quota-key"
CLIENTS = {"alice": hash_key(ALICE), "bob": hash_key(BOB)}
NOON = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def use(quota: DailyQuota, client: str, times: int) -> None:
    for _ in range(times):
        assert quota.check(client) is None
        quota.record(client)


def test_a_client_is_stopped_at_its_own_cap_without_affecting_others() -> None:
    quota = DailyQuota(2, 0, clock=lambda: NOON)
    use(quota, "alice", 2)
    denied = quota.check("alice")
    assert denied is not None and denied[0] == "DAILY_LIMIT"
    assert quota.check("bob") is None


def test_the_total_cap_stops_everyone() -> None:
    quota = DailyQuota(0, 3, clock=lambda: NOON)
    use(quota, "alice", 2)
    use(quota, "bob", 1)
    for client in ("alice", "bob", "carol"):
        denied = quota.check(client)
        assert denied is not None and denied[0] == "DAILY_CAPACITY"


def test_zero_means_unlimited() -> None:
    quota = DailyQuota(0, 0, clock=lambda: NOON)
    use(quota, "alice", 500)


def test_counts_reset_at_utc_midnight_and_the_wait_is_until_then() -> None:
    now = [NOON]
    quota = DailyQuota(1, 0, clock=lambda: now[0])
    use(quota, "alice", 1)
    denied = quota.check("alice")
    assert denied is not None and denied[1] == 12 * 3600
    now[0] = datetime(2026, 11, 2, 0, 0, 1, tzinfo=UTC)
    assert quota.check("alice") is None


class Stub:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def ask(self, question: str) -> PipelineResult:
        self.questions.append(question)
        return PipelineResult(
            ok=True,
            sql="SELECT 1",
            execution=ExecutionResult(ok=True, columns=("c",), rows=((5,),), elapsed_ms=1.0),
            confidence=Confidence.MEDIUM,
            score=0.55,
            signals=Signals(1, 1, 0, False),
            llm_calls=1,
            input_tokens=100,
            output_tokens=10,
        )

    def ping(self) -> bool:
        return True

    def schema(self) -> dict[str, list[str]]:
        return {"orders": ["id"]}

    def close(self) -> None:
        pass


def make_client(quota: DailyQuota) -> tuple[TestClient, Stub, MemoryAuditLog]:
    stub, audit = Stub(), MemoryAuditLog()
    app = create_app(
        stub,  # type: ignore[arg-type]
        audit=audit,
        max_concurrent_asks=4,
        auth=ClientAuth(CLIENTS),
        limiter=RateLimiter(0, 1),
        quota=quota,
    )
    return TestClient(app), stub, audit


def ask(client: TestClient, key: str) -> object:
    return client.post("/v1/ask", json={"question": "how many orders"}, headers={"X-API-Key": key})


def test_api_returns_429_daily_limit_and_never_calls_the_model() -> None:
    client, stub, audit = make_client(DailyQuota(2, 0, clock=lambda: NOON))
    with client:
        assert ask(client, ALICE).status_code == 200  # type: ignore[attr-defined]
        assert ask(client, ALICE).status_code == 200  # type: ignore[attr-defined]
        blocked = ask(client, ALICE)
        assert ask(client, BOB).status_code == 200  # type: ignore[attr-defined]
    assert blocked.status_code == 429  # type: ignore[attr-defined]
    assert blocked.json()["error"]["code"] == "DAILY_LIMIT"  # type: ignore[attr-defined]
    assert blocked.headers["retry-after"] == str(12 * 3600)  # type: ignore[attr-defined]
    assert len(stub.questions) == 3  # the blocked request never reached the model
    assert any(getattr(e, "code", None) == "DAILY_LIMIT" for e in audit.events)


def test_api_returns_daily_capacity_when_the_overall_cap_is_used_up() -> None:
    client, _, _ = make_client(DailyQuota(0, 1, clock=lambda: NOON))
    with client:
        assert ask(client, ALICE).status_code == 200  # type: ignore[attr-defined]
        blocked = ask(client, BOB)
    assert blocked.status_code == 429  # type: ignore[attr-defined]
    assert blocked.json()["error"]["code"] == "DAILY_CAPACITY"  # type: ignore[attr-defined]


def test_settings_default_to_no_daily_caps_and_reject_negatives() -> None:
    settings = Settings(_env_file=None)
    assert (settings.daily_limit_per_key, settings.daily_limit_total) == (0, 0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, daily_limit_per_key=-1)
