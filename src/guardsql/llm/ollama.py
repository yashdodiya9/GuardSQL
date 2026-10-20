"""Local models through Ollama's /api/chat. Free, offline, and no rate limits."""

from __future__ import annotations

import time
from typing import Any

import httpx

from guardsql.llm.errors import LLMError, LLMErrorKind
from guardsql.llm.http import as_int, post_json
from guardsql.llm.types import LLMRequest, LLMResponse


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 4096,
        timeout_s: float = 60.0,
        keep_alive: str = "10m",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive
        # Connecting to a local server is instant: fail fast if it is not running.
        # Reading can be slow: the first call loads the model into memory.
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    @property
    def provider(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._http.close()

    def generate(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "stream": False,  # Ollama streams by default
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
                "num_ctx": self._num_ctx,
            },
        }
        started = time.perf_counter()
        try:
            data = post_json(
                self._http,
                "/api/chat",
                payload,
                provider="ollama",
                unreachable_hint=(
                    f"Could not reach Ollama at {self._base_url}. Is `ollama serve` running?"
                ),
            )
        except LLMError as exc:
            if exc.kind is LLMErrorKind.REJECTED and "not found" in exc.message.lower():
                raise LLMError(
                    LLMErrorKind.REJECTED, f"{exc.message} Run: ollama pull {self._model}"
                ) from exc
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        return self._parse(data, latency_ms)

    def _parse(self, data: Any, latency_ms: float) -> LLMResponse:
        message = data.get("message") if isinstance(data, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMError(LLMErrorKind.BAD_RESPONSE, "ollama response has no message content.")
        if data.get("done_reason") == "length":
            raise LLMError(
                LLMErrorKind.TRUNCATED,
                "The model hit the output token limit before finishing the SQL.",
            )
        if not content.strip():
            raise LLMError(LLMErrorKind.BAD_RESPONSE, "ollama returned an empty answer.")
        return LLMResponse(
            text=content,
            provider="ollama",
            model=self._model,
            input_tokens=as_int(data.get("prompt_eval_count")),
            output_tokens=as_int(data.get("eval_count")),
            latency_ms=latency_ms,
        )
