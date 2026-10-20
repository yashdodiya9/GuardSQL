"""Shared HTTP plumbing: turn every way a request can fail into an LLMError."""

from __future__ import annotations

from typing import Any

import httpx

from guardsql.llm.errors import LLMError, LLMErrorKind

_MAX_DETAIL_CHARS = 200


def error_detail(response: httpx.Response) -> str:
    """A short, safe description of an error body (Ollama: {"error": "..."},
    Gemini: {"error": {"message": "..."}})."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:_MAX_DETAIL_CHARS].strip()
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    if isinstance(error, str) and error:
        return error[:_MAX_DETAIL_CHARS]
    return f"HTTP {response.status_code}"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None  # missing, or an HTTP-date: fall back to our own backoff


def status_error(provider: str, response: httpx.Response) -> LLMError:
    code = response.status_code
    detail = error_detail(response)
    if code == 429:
        return LLMError(
            LLMErrorKind.RATE_LIMITED,
            f"{provider} rate limit reached. {detail}",
            retry_after_s=_retry_after(response),
        )
    if code in (401, 403):
        return LLMError(LLMErrorKind.AUTH, f"{provider} refused the credentials (HTTP {code}).")
    if code >= 500:
        return LLMError(LLMErrorKind.UNAVAILABLE, f"{provider} had a server error (HTTP {code}).")
    return LLMError(LLMErrorKind.REJECTED, f"{provider} rejected the request: {detail}")


def post_json(
    http: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    provider: str,
    unreachable_hint: str,
) -> Any:
    try:
        response = http.post(url, json=payload)
    except httpx.TimeoutException as exc:  # must come before TransportError (its parent)
        raise LLMError(LLMErrorKind.TIMEOUT, f"{provider} did not answer in time.") from exc
    except httpx.TransportError as exc:
        raise LLMError(LLMErrorKind.UNAVAILABLE, unreachable_hint) from exc
    if response.status_code >= 400:
        raise status_error(provider, response)
    try:
        return response.json()
    except ValueError as exc:
        raise LLMError(LLMErrorKind.BAD_RESPONSE, f"{provider} returned invalid JSON.") from exc


def as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
