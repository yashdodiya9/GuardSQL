"""Google Gemini through the REST API (free tier available, rate-limited)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from guardsql.llm.errors import LLMError, LLMErrorKind
from guardsql.llm.http import as_int, post_json
from guardsql.llm.types import LLMRequest, LLMResponse

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 60.0,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set.")
        self._model = model
        # The key travels in a header, never in the URL, so it cannot end up in
        # access logs, proxy logs or exception messages that print the URL.
        self._http = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            headers={"x-goog-api-key": api_key},
            transport=transport,
        )

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._http.close()

    def generate(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
            },
        }
        started = time.perf_counter()
        data = post_json(
            self._http,
            f"models/{self._model}:generateContent",
            payload,
            provider="gemini",
            unreachable_hint="Could not reach the Gemini API. Check your network connection.",
        )
        latency_ms = (time.perf_counter() - started) * 1000
        return self._parse(data, latency_ms)

    def _parse(self, data: Any, latency_ms: float) -> LLMResponse:
        if not isinstance(data, dict):
            raise LLMError(LLMErrorKind.BAD_RESPONSE, "gemini response is not a JSON object.")
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback")
            reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
            if reason:
                raise LLMError(LLMErrorKind.BLOCKED, f"Prompt blocked by Gemini ({reason}).")
            raise LLMError(LLMErrorKind.BAD_RESPONSE, "gemini returned no candidates.")
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        finish = candidate.get("finishReason")
        content = candidate.get("content")
        parts = (content.get("parts") if isinstance(content, dict) else None) or []
        # Thinking models mark their reasoning parts with thought=true: not part of the answer.
        text = "".join(
            p["text"]
            for p in parts
            if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")
        )
        if finish == "MAX_TOKENS":
            raise LLMError(
                LLMErrorKind.TRUNCATED,
                "Gemini hit the output token limit before finishing the SQL "
                "(thinking tokens count towards it).",
            )
        if not text.strip():
            if finish in (None, "STOP"):
                raise LLMError(LLMErrorKind.BAD_RESPONSE, "gemini returned an empty answer.")
            raise LLMError(LLMErrorKind.BLOCKED, f"Gemini stopped without an answer ({finish}).")
        usage = data.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        answer_tokens = as_int(usage.get("candidatesTokenCount"))
        thought_tokens = as_int(usage.get("thoughtsTokenCount")) or 0
        return LLMResponse(
            text=text,
            provider="gemini",
            model=self._model,
            input_tokens=as_int(usage.get("promptTokenCount")),
            output_tokens=None if answer_tokens is None else answer_tokens + thought_tokens,
            latency_ms=latency_ms,
        )
