"""Append-only audit trail: one JSON object per line.

It records WHO asked WHAT and what the system DID (metadata), never the result rows.
A failure to write must never fail the user's request, so write() swallows OSError and logs it.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from guardsql.hallucination.pipeline import PipelineResult

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500
MAX_MESSAGE_CHARS = 300


@dataclass(frozen=True)
class AuditEvent:
    request_id: str
    ts: str
    question: str
    ok: bool
    http_status: int
    latency_ms: float
    stage: str | None = None
    code: str | None = None
    message: str | None = None
    sql: str | None = None
    confidence: str | None = None
    score: float | None = None
    samples_agreeing: int | None = None
    samples_requested: int | None = None
    repairs: int | None = None
    judge: str | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    row_count: int | None = None
    escalated: bool = False
    escalation_reason: str | None = None

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        question: str,
        http_status: int,
        latency_ms: float,
        result: PipelineResult | None = None,
        code: str | None = None,
    ) -> AuditEvent:
        base = {
            "request_id": request_id,
            "ts": datetime.now(UTC).isoformat(),
            "question": question[:MAX_QUESTION_CHARS],
            "http_status": http_status,
            "latency_ms": round(latency_ms, 1),
        }
        if result is None:  # the pipeline never returned: busy, or an unexpected crash
            return cls(ok=False, code=code, **base)  # type: ignore[arg-type]
        signals = result.signals
        return cls(
            ok=result.ok,
            stage=result.stage,
            code=result.code,
            message=(result.message or "")[:MAX_MESSAGE_CHARS] or None,
            sql=result.sql,
            confidence=str(result.confidence) if result.confidence else None,
            score=result.score,
            samples_agreeing=signals.samples_agreeing if signals else None,
            samples_requested=signals.samples_requested if signals else None,
            repairs=signals.repairs if signals else None,
            judge=str(signals.judge) if signals and signals.judge else None,
            llm_calls=result.llm_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            row_count=len(result.execution.rows) if result.execution else None,
            escalated=result.escalated,
            escalation_reason=result.escalation_reason,
            **base,  # type: ignore[arg-type]
        )


class AuditLog(Protocol):
    def write(self, event: AuditEvent) -> None: ...


class JsonlAuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def write(self, event: AuditEvent) -> None:
        # json.dumps escapes newlines, so a hostile question cannot forge extra log lines.
        line = json.dumps(asdict(event), default=str, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            log.exception("audit write failed (request %s)", event.request_id)


class MemoryAuditLog:
    """For tests."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)
