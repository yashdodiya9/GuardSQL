from __future__ import annotations

import httpx

from guardsql.config import Settings
from guardsql.llm.fake import FakeLLM
from guardsql.llm.gemini import GeminiClient
from guardsql.llm.ollama import OllamaClient
from guardsql.llm.retry import RetryingClient
from guardsql.llm.types import LLMClient


def build_client(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> LLMClient:
    """Choose the provider from configuration. Fails at startup if it is misconfigured."""
    if settings.llm_provider == "fake":
        return FakeLLM.constant(settings.llm_fake_response)
    inner: LLMClient
    if settings.llm_provider == "ollama":
        inner = OllamaClient(
            settings.ollama_base_url,
            settings.ollama_model,
            num_ctx=settings.ollama_num_ctx,
            timeout_s=settings.llm_timeout_s,
            transport=transport,
        )
    else:
        inner = GeminiClient(
            settings.gemini_api_key.get_secret_value(),
            settings.gemini_model,
            timeout_s=settings.llm_timeout_s,
            transport=transport,
        )
    return RetryingClient(inner, max_attempts=settings.llm_max_attempts)


def with_model(settings: Settings, model: str) -> Settings:
    """A copy of the settings that points the same provider at a different model."""
    field = {"ollama": "ollama_model", "gemini": "gemini_model"}.get(settings.llm_provider)
    if field is None:
        return settings
    return settings.model_copy(update={field: model})
