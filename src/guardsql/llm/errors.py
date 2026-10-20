from __future__ import annotations

from enum import StrEnum


class LLMErrorKind(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"  # cannot connect, or the provider returned 5xx
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH = "AUTH"  # missing, invalid or unauthorised API key
    REJECTED = "REJECTED"  # other 4xx: bad request, unknown model
    BAD_RESPONSE = "BAD_RESPONSE"  # not JSON, or not the shape we expect, or empty
    TRUNCATED = "TRUNCATED"  # hit the output token limit: the SQL would be cut off
    BLOCKED = "BLOCKED"  # provider-side safety filter


RETRYABLE: frozenset[LLMErrorKind] = frozenset(
    {LLMErrorKind.UNAVAILABLE, LLMErrorKind.TIMEOUT, LLMErrorKind.RATE_LIMITED}
)


class LLMError(Exception):
    """Every provider failure is turned into this, so callers handle one error type.

    Messages are written for humans and must never contain credentials.
    """

    def __init__(
        self, kind: LLMErrorKind, message: str, *, retry_after_s: float | None = None
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE
