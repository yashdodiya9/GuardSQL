import types

import pytest
from fastapi.testclient import TestClient

from guardsql.api.app import create_app
from guardsql.api.security import ClientAuth, RateLimiter, hash_key, parse_api_keys
from guardsql.audit import MemoryAuditLog
from guardsql.execution.executor import ExecutionResult
from guardsql.hallucination.confidence import Confidence, Signals
from guardsql.hallucination.pipeline import PipelineResult

ALICE_KEY = "gsql_alice-secret-key"
BOB_KEY = "gsql_bob-secret-key"
CLIENTS = {"alice": hash_key(ALICE_KEY), "bob": hash_key(BOB_KEY)}
QUESTION = {"question": "how many orders"}


class Stub:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def ask(self, question: str) -> PipelineResult:
        self.questions.append(question)
        execution = ExecutionResult(ok=True, columns=("c",), rows=((5,),), elapsed_ms=1.0)
        return PipelineResult(
            ok=True,
            sql="SELECT 1",
            execution=execution,
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


def make_client(limiter: RateLimiter | None = None, *, auth: ClientAuth | None = None):
    stub, audit = Stub(), MemoryAuditLog()
    app = create_app(
        stub,
        audit=audit,
        max_concurrent_asks=4,
        auth=auth if auth is not None else ClientAuth(CLIENTS),
        limiter=limiter if limiter is not None else RateLimiter(0, 1),
    )
    return app, stub, audit


def test_parse_api_keys_accepts_valid_entries() -> None:
    parsed = parse_api_keys(f"alice:{hash_key('a')}, bob:{hash_key('b').upper()}")
    assert parsed == {"alice": hash_key("a"), "bob": hash_key("b")}
    assert parse_api_keys("") == {}


@pytest.mark.parametrize(
    "raw",
    ["alice", "alice:nothex", f"bad name:{hash_key('a')}", f"a:{hash_key('a')},a:{hash_key('b')}"],
    ids=["no-hash", "bad-hash", "bad-name", "duplicate"],
)
def test_parse_api_keys_rejects_bad_entries(raw: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_api_keys(raw)
    assert "nothex" not in str(caught.value)


def test_identify_matches_only_the_right_key() -> None:
    auth = ClientAuth(CLIENTS)
    assert auth.identify(ALICE_KEY) == "alice"
    assert auth.identify(BOB_KEY) == "bob"
    assert auth.identify("wrong") is None
    assert auth.identify("") is None
    assert auth.identify(None) is None


def test_production_without_keys_refuses_to_start() -> None:
    settings = types.SimpleNamespace(app_env="production", api_keys="")
    with pytest.raises(RuntimeError, match="API_KEYS"):
        ClientAuth.from_settings(settings)  # type: ignore[arg-type]


def test_local_without_keys_runs_with_auth_off() -> None:
    settings = types.SimpleNamespace(app_env="local", api_keys="")
    assert not ClientAuth.from_settings(settings).enabled  # type: ignore[arg-type]


def test_missing_or_wrong_key_is_401_and_never_reaches_the_service() -> None:
    app, stub, _ = make_client()
    with TestClient(app) as client:
        missing = client.post("/v1/ask", json=QUESTION)
        wrong = client.post("/v1/ask", json=QUESTION, headers={"X-API-Key": "nope"})
    for response in (missing, wrong):
        assert response.status_code == 401
        body = response.json()["error"]
        assert body["code"] == "UNAUTHORIZED" and body["request_id"]
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert stub.questions == []


def test_a_bad_body_without_a_key_is_401_not_422() -> None:
    app, _, _ = make_client()
    with TestClient(app) as client:
        assert client.post("/v1/ask", json={"nope": 1}).status_code == 401


def test_both_header_styles_work() -> None:
    app, stub, _ = make_client()
    with TestClient(app) as client:
        by_header = client.post("/v1/ask", json=QUESTION, headers={"X-API-Key": ALICE_KEY})
        by_bearer = client.post(
            "/v1/ask", json=QUESTION, headers={"Authorization": f"Bearer {BOB_KEY}"}
        )
    assert by_header.status_code == 200 and by_bearer.status_code == 200
    assert len(stub.questions) == 2


def test_health_and_ready_are_open_but_schema_is_not() -> None:
    app, _, _ = make_client()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/v1/schema").status_code == 401
        assert client.get("/v1/schema", headers={"X-API-Key": ALICE_KEY}).status_code == 200


def test_audit_records_the_client_name_and_never_the_key() -> None:
    app, _, audit = make_client()
    with TestClient(app) as client:
        client.post("/v1/ask", json=QUESTION, headers={"X-API-Key": ALICE_KEY})
    assert audit.events[-1].client == "alice"
    assert ALICE_KEY not in repr(audit.events)


def test_rate_limit_returns_429_with_retry_after_and_is_per_client() -> None:
    now = [0.0]
    limiter = RateLimiter(60, 2, clock=lambda: now[0])  # 1 request/second, burst of 2
    app, stub, audit = make_client(limiter)
    alice = {"X-API-Key": ALICE_KEY}
    with TestClient(app) as client:
        assert client.post("/v1/ask", json=QUESTION, headers=alice).status_code == 200
        assert client.post("/v1/ask", json=QUESTION, headers=alice).status_code == 200
        limited = client.post("/v1/ask", json=QUESTION, headers=alice)
        other = client.post("/v1/ask", json=QUESTION, headers={"X-API-Key": BOB_KEY})
        now[0] = 1.0
        refilled = client.post("/v1/ask", json=QUESTION, headers=alice)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.headers["Retry-After"] == "1"
    assert other.status_code == 200
    assert refilled.status_code == 200
    assert len(stub.questions) == 4  # the limited request never reached the service
    assert any(e.code == "RATE_LIMITED" and e.client == "alice" for e in audit.events)


def test_limiter_refills_gradually_and_zero_disables_it() -> None:
    now = [0.0]
    limiter = RateLimiter(60, 1, clock=lambda: now[0])
    assert limiter.check("a") is None
    wait = limiter.check("a")
    assert wait == pytest.approx(1.0)
    now[0] = 0.5
    assert limiter.check("a") == pytest.approx(0.5)
    now[0] = 1.0
    assert limiter.check("a") is None
    unlimited = RateLimiter(0, 1)
    assert all(unlimited.check("a") is None for _ in range(100))


def test_auth_off_means_no_key_and_no_rate_limit() -> None:
    app, stub, _ = make_client(RateLimiter(60, 1), auth=ClientAuth({}))
    with TestClient(app) as client:
        statuses = [client.post("/v1/ask", json=QUESTION).status_code for _ in range(5)]
    assert statuses == [200] * 5
    assert len(stub.questions) == 5
