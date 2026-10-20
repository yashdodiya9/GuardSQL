from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Callable

from guardsql.llm.errors import LLMError
from guardsql.llm.types import LLMClient, LLMRequest, LLMResponse


class RetryingClient:
    """Retries transient failures (timeouts, outages, rate limits) with exponential backoff.

    Auth errors, bad requests and truncation are never retried: a second try cannot fix them.
    A server that asks us to wait longer than `max_delay_s` is not waited for: an interactive
    request is better off failing fast than hanging.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        max_attempts: int = 3,
        base_delay_s: float = 1.0,
        max_delay_s: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._inner = inner
        self._max_attempts = max_attempts
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._sleep = sleep
        self._jitter = jitter

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def generate(self, request: LLMRequest) -> LLMResponse:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._inner.generate(request)
            except LLMError as exc:
                if not exc.retryable or attempt == self._max_attempts:
                    raise
                if exc.retry_after_s is not None and exc.retry_after_s > self._max_delay_s:
                    raise
                backoff = self._base_delay_s * 2 ** (attempt - 1)
                wait = exc.retry_after_s if exc.retry_after_s is not None else backoff
                # Jitter stops many clients from retrying in lockstep.
                self._sleep(min(wait * (1 + 0.25 * self._jitter()), self._max_delay_s))
                continue
            return dataclasses.replace(response, attempts=attempt)
        raise AssertionError("unreachable")
