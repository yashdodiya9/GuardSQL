"""Query cache: remember validated SQL for repeated questions, never rows.

A hit skips the LLM but not the guardrails: the SQL is re-validated, cost-gated and executed
again, so rows are always fresh and a stricter validator or policy takes effect immediately.
Only exact questions match (after normalization). A semantic match could return the right SQL
for the wrong question, which no downstream check can catch.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from guardsql.execution.executor import ExecutionResult
from guardsql.guardrails.ast_validator import validate_sql
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.hallucination.confidence import Confidence
from guardsql.hallucination.pipeline import Asker, PipelineResult, Runner
from guardsql.llm.prompts import needs_current_date

log = logging.getLogger(__name__)


def cache_fingerprint(*parts: str) -> str:
    """Hash of everything that changes what SQL the pipeline would produce."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")  # separator, so ("ab", "c") and ("a", "bc") differ
    return digest.hexdigest()


def normalize_question(question: str) -> str:
    return " ".join(question.lower().split()).rstrip("?.! ")


def _empty(execution: ExecutionResult) -> bool:
    # Same definition as the pipeline's empty-result signal.
    if not execution.rows:
        return True
    return len(execution.rows) == 1 and all(value is None for value in execution.rows[0])


def _cacheable(result: PipelineResult) -> bool:
    return (
        result.ok
        and result.sql is not None
        and result.execution is not None
        and not _empty(result.execution)
        and result.confidence is not Confidence.LOW
    )


@dataclass
class _Entry:
    sql: str
    template: PipelineResult  # the answer minus its rows
    expires_at: float


class CachingPipeline:
    def __init__(
        self,
        inner: Asker,
        runner: Runner,
        policy: SchemaPolicy,
        *,
        fingerprint: str,
        max_entries: int = 512,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._runner = runner
        self._policy = policy
        self._fingerprint = fingerprint
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def ask(self, question: str) -> PipelineResult:
        if needs_current_date(question):
            return self._inner.ask(question)  # its SQL embeds today's date: never cache
        key = (self._fingerprint, normalize_question(question))
        entry = self._lookup(key)
        if entry is not None:
            replayed = self._replay(entry)
            if replayed is not None:
                with self._lock:
                    self._hits += 1
                return replayed
            self._evict(key)
        with self._lock:
            self._misses += 1
        result = self._inner.ask(question)
        if _cacheable(result) and result.sql is not None:
            self._put(
                key,
                _Entry(
                    sql=result.sql,
                    template=dataclasses.replace(result, execution=None),
                    expires_at=self._clock() + self._ttl,
                ),
            )
        return result

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "size": len(self._store)}

    def _lookup(self, key: tuple[str, str]) -> _Entry | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return entry

    def _replay(self, entry: _Entry) -> PipelineResult | None:
        validation = validate_sql(entry.sql, self._policy)
        if not validation.ok or validation.sql is None:
            log.info("cached SQL no longer validates (%s); evicting", validation.code)
            return None
        execution = self._runner.execute(validation)
        if not execution.ok or _empty(execution):
            log.info("cached SQL no longer yields rows; evicting")
            return None
        return dataclasses.replace(
            entry.template,
            sql=validation.sql,
            execution=execution,
            llm_calls=0,
            input_tokens=0,
            output_tokens=0,
            usage_complete=True,
            escalated=False,
            escalation_reason=None,
            strong_calls=0,
            strong_input_tokens=0,
            strong_output_tokens=0,
            cache_hit=True,
        )

    def _put(self, key: tuple[str, str], entry: _Entry) -> None:
        with self._lock:
            self._store[key] = entry
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def _evict(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._store.pop(key, None)
