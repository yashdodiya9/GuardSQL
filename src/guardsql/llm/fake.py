"""A scripted model for tests and for running the app with no model installed."""

from __future__ import annotations

import time
from collections.abc import Sequence

from guardsql.llm.errors import LLMError
from guardsql.llm.types import LLMRequest, LLMResponse


class FakeLLM:
    def __init__(self, responses: Sequence[str | LLMError], *, repeat_last: bool = False) -> None:
        self._responses = list(responses)
        self._repeat_last = repeat_last
        self.requests: list[LLMRequest] = []

    @classmethod
    def constant(cls, text: str) -> FakeLLM:
        return cls([text], repeat_last=True)

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-1"

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        started = time.perf_counter()
        if not self._responses:
            raise AssertionError("FakeLLM was called more times than it has scripted responses.")
        item = self._responses[0] if self._repeat_last else self._responses.pop(0)
        if isinstance(item, LLMError):
            raise item
        return LLMResponse(
            text=item,
            provider="fake",
            model="fake-1",
            # Rough 4-characters-per-token estimate, only so cost code has numbers to work with.
            input_tokens=(len(request.system) + len(request.user)) // 4,
            output_tokens=len(item) // 4,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
