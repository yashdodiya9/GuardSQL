import pytest
from pydantic import ValidationError

from guardsql.config import Settings
from guardsql.llm.factory import build_client
from guardsql.llm.fake import FakeLLM
from guardsql.llm.retry import RetryingClient
from guardsql.llm.types import LLMRequest


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_to_local_ollama_with_a_retry_wrapper() -> None:
    client = build_client(make())
    assert isinstance(client, RetryingClient)
    assert (client.provider, client.model) == ("ollama", "qwen2.5-coder:7b")


def test_gemini_selected_by_configuration() -> None:
    client = build_client(make(llm_provider="gemini", gemini_api_key="k", gemini_model="m-1"))
    assert (client.provider, client.model) == ("gemini", "m-1")


def test_gemini_without_a_key_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_client(make(llm_provider="gemini"))


def test_fake_provider_answers_with_the_configured_text_every_time() -> None:
    client = build_client(make(llm_provider="fake", llm_fake_response="SELECT 42"))
    assert isinstance(client, FakeLLM)
    request = LLMRequest(system="s", user="u")
    assert client.generate(request).text == "SELECT 42"
    assert client.generate(request).text == "SELECT 42"


def test_unknown_provider_is_rejected_when_settings_load() -> None:
    with pytest.raises(ValidationError):
        make(llm_provider="openai")


def test_api_key_is_never_shown_in_repr_or_str() -> None:
    settings = make(gemini_api_key="AIza-very-secret")
    assert "AIza-very-secret" not in repr(settings)
    assert "AIza-very-secret" not in str(settings)
    assert "AIza-very-secret" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_timeout_s": 0},
        {"llm_max_attempts": 0},
        {"llm_max_attempts": 7},
        {"llm_temperature": -1},
        {"llm_max_output_tokens": 1},
        {"ollama_num_ctx": 10},
    ],
)
def test_nonsense_llm_settings_fail_at_startup(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**overrides)
