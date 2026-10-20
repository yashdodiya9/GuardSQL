"""Provider-neutral request/response types. Nothing in here knows about a vendor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    max_output_tokens: int = 512
    temperature: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    # None when the provider did not report usage. Never guessed.
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float
    # How many HTTP calls it took (retries burn rate-limit quota, so we count them).
    attempts: int = 1


class LLMClient(Protocol):
    """The only thing the rest of the app depends on."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...
