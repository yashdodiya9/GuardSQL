import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guardsql.api.app import create_app
from guardsql.audit import AuditEvent, JsonlAuditLog, MemoryAuditLog
from guardsql.execution.executor import ExecutionResult
from guardsql.hallucination.confidence import Confidence, Signals
from guardsql.hallucination.pipeline import PipelineResult


def ok_result() -> PipelineResult:
    execution = ExecutionResult(
        ok=True, columns=("count",), rows=((5,),), elapsed_ms=3.0, plan_cost=12.0
    )
    return PipelineResult(
        ok=True,
        sql="SELECT count(*) FROM orders LIMIT 100",
        execution=execution,
        confidence=Confidence.HIGH,
        score=0.85,
        signals=Signals(3, 3, 0, False),
        llm_calls=3,
        input_tokens=900,
        output_tokens=30,
    )


def failure(stage: str, code: str, message: str = "m", sql: str | None = None) -> PipelineResult:
    return PipelineResult(ok=False, stage=stage, code=code, message=message, sql=sql)


class StubService:
    def __init__(
        self,
        result: PipelineResult | None = None,
        *,
        raises: Exception | None = None,
        ready: bool = True,
    ) -> None:
        self.result = result or ok_result()
        self.raises = raises
        self.ready = ready
        self.questions: list[str] = []

    def ask(self, question: str) -> PipelineResult:
        self.questions.append(question)
        if self.raises is not None:
            raise self.raises
        return self.result

    def ping(self) -> bool:
        return self.ready

    def schema(self) -> dict[str, list[str]]:
        return {"orders": ["id", "status"]}

    def close(self) -> None:
        pass


def client_for(stub: StubService, audit: MemoryAuditLog, slots: int = 4, **kw: bool) -> TestClient:
    app = create_app(service=stub, audit=audit, max_concurrent_asks=slots)
    return TestClient(app, **kw)  # type: ignore[arg-type]


def test_ask_success_shape_and_request_id() -> None:
    audit = MemoryAuditLog()
    with client_for(StubService(), audit) as client:
        response = client.post("/v1/ask", json={"question": "how many orders"})
    body = response.json()
    assert response.status_code == 200
    assert body["rows"] == [[5]]
    assert body["columns"] == ["count"]
    assert body["confidence"]["level"] == "HIGH"
    assert body["usage"]["llm_calls"] == 3
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_audit_event_for_success_has_metadata_but_no_rows() -> None:
    audit = MemoryAuditLog()
    with client_for(StubService(), audit) as client:
        client.post("/v1/ask", json={"question": "how many orders"})
    (event,) = audit.events
    assert event.ok and event.http_status == 200
    assert event.row_count == 1
    assert event.confidence == "HIGH"
    assert "rows" not in json.dumps(event.__dict__)


def test_valid_client_request_id_is_echoed() -> None:
    with client_for(StubService(), MemoryAuditLog()) as client:
        response = client.get("/health", headers={"X-Request-ID": "abc-12345_XYZ"})
    assert response.headers["X-Request-ID"] == "abc-12345_XYZ"


@pytest.mark.parametrize("bad", ["short", "has space in it", "x" * 65, "a\tb\tc\td\te\tf"])
def test_bad_client_request_id_is_replaced(bad: str) -> None:
    with client_for(StubService(), MemoryAuditLog()) as client:
        response = client.get("/health", headers={"X-Request-ID": bad})
    assert response.headers["X-Request-ID"] != bad
    assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.parametrize(
    "payload",
    [{}, {"question": ""}, {"question": 5}, {"question": "x" * 2001}, {"question": "ok", "x": 1}],
)
def test_invalid_body_is_422_envelope(payload: dict[str, object]) -> None:
    stub = StubService()
    with client_for(stub, MemoryAuditLog()) as client:
        response = client.post("/v1/ask", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert stub.questions == []


def test_non_json_body_is_422() -> None:
    with client_for(StubService(), MemoryAuditLog()) as client:
        response = client.post("/v1/ask", content=b"not json")
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("stage", "code", "status", "public_code"),
    [
        ("generate", "QUESTION_EMPTY", 422, "QUESTION_EMPTY"),
        ("generate", "QUESTION_TOO_LONG", 422, "QUESTION_TOO_LONG"),
        ("generate", "CANNOT_ANSWER", 422, "CANNOT_ANSWER"),
        ("generate", "LLM_ERROR", 503, "MODEL_UNAVAILABLE"),
        ("generate", "NO_SQL", 502, "MODEL_BAD_OUTPUT"),
        ("validate", "RESTRICTED_COLUMN", 422, "QUERY_BLOCKED"),
        ("validate", "UNKNOWN_TABLE", 422, "QUERY_BLOCKED"),
        ("execute", "COST_TOO_HIGH", 422, "QUERY_TOO_EXPENSIVE"),
        ("execute", "TIMEOUT", 504, "QUERY_TIMEOUT"),
        ("execute", "UNAVAILABLE", 503, "DATABASE_UNAVAILABLE"),
        ("execute", "DATABASE_ERROR", 502, "QUERY_FAILED"),
        ("execute", "NOT_VALIDATED", 500, "INTERNAL"),
        ("mystery", "???", 500, "INTERNAL"),
    ],
)
def test_failure_mapping(stage: str, code: str, status: int, public_code: str) -> None:
    audit = MemoryAuditLog()
    with client_for(StubService(failure(stage, code)), audit) as client:
        response = client.post("/v1/ask", json={"question": "q"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == public_code
    assert audit.events[0].http_status == status
    assert audit.events[0].code == code


def test_blocked_query_hides_internals_but_audit_keeps_them() -> None:
    audit = MemoryAuditLog()
    result = failure(
        "validate",
        "RESTRICTED_COLUMN",
        "Column 'salary' is restricted",
        sql="SELECT salary FROM employees",
    )
    with client_for(StubService(result), audit) as client:
        response = client.post("/v1/ask", json={"question": "salaries"})
    assert "salary" not in response.text
    assert response.json()["error"]["reason"] == "RESTRICTED_COLUMN"
    assert audit.events[0].sql == "SELECT salary FROM employees"
    assert "salary" in (audit.events[0].message or "")


def test_llm_outage_sets_retry_after() -> None:
    with client_for(StubService(failure("generate", "LLM_ERROR")), MemoryAuditLog()) as client:
        response = client.post("/v1/ask", json={"question": "q"})
    assert response.headers["Retry-After"] == "5"


def test_unexpected_exception_is_a_safe_500_and_is_audited() -> None:
    audit = MemoryAuditLog()
    stub = StubService(raises=RuntimeError("password=hunter2"))
    with client_for(stub, audit, raise_server_exceptions=False) as client:
        response = client.post("/v1/ask", json={"question": "q"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL"
    assert "hunter2" not in response.text
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert audit.events[0].code == "INTERNAL"


def test_busy_returns_429_and_never_calls_the_service() -> None:
    audit = MemoryAuditLog()
    stub = StubService()
    with client_for(stub, audit, slots=1) as client:
        client.app.state.slots.acquire()  # type: ignore[attr-defined]
        response = client.post("/v1/ask", json={"question": "q"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "BUSY"
    assert response.headers["Retry-After"] == "2"
    assert stub.questions == []
    assert audit.events[0].code == "BUSY"


def test_slot_is_released_after_a_failure() -> None:
    stub = StubService(failure("execute", "TIMEOUT"))
    with client_for(stub, MemoryAuditLog(), slots=1) as client:
        for _ in range(3):
            assert client.post("/v1/ask", json={"question": "q"}).status_code == 504


def test_unknown_route_uses_the_envelope() -> None:
    with client_for(StubService(), MemoryAuditLog()) as client:
        response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_ready_reflects_database() -> None:
    with client_for(StubService(ready=True), MemoryAuditLog()) as client:
        assert client.get("/ready").status_code == 200
    with client_for(StubService(ready=False), MemoryAuditLog()) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NOT_READY"


def test_schema_endpoint() -> None:
    with client_for(StubService(), MemoryAuditLog()) as client:
        response = client.get("/v1/schema")
    assert response.json() == {"tables": {"orders": ["id", "status"]}}


def event(question: str = "q") -> AuditEvent:
    return AuditEvent.build(request_id="r1", question=question, http_status=200, latency_ms=1.0)


def test_jsonl_audit_creates_dirs_and_writes_valid_lines(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "audit.jsonl"
    log = JsonlAuditLog(path)
    log.write(event())
    log.write(event())
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["request_id"] == "r1"


def test_hostile_newlines_cannot_forge_log_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).write(event('hi"}\n{"request_id": "forged"'))
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["request_id"] == "r1"


def test_concurrent_audit_writes_stay_whole(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(path)
    threads = [threading.Thread(target=log.write, args=(event(f"q{i}"),)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 40
    assert all(json.loads(line)["request_id"] == "r1" for line in lines)


def test_audit_failure_never_raises(tmp_path: Path) -> None:
    JsonlAuditLog(tmp_path).write(event())  # the path is a directory: open() fails


def test_audit_truncates_long_questions() -> None:
    assert len(event("x" * 5000).question) == 500
