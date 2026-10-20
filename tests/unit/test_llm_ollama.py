import json
from typing import Any

import httpx
import pytest

from guardsql.llm.errors import LLMError, LLMErrorKind
from guardsql.llm.ollama import OllamaClient
from guardsql.llm.types import LLMRequest

REQUEST = LLMRequest(system="SYS", user="USER", max_output_tokens=200, temperature=0.0)
OK_BODY = {
    "model": "qwen2.5-coder:7b",
    "message": {"role": "assistant", "content": "```sql\nSELECT 1\n```"},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 120,
    "eval_count": 15,
}


def make(handler: Any, **kwargs: Any) -> OllamaClient:
    return OllamaClient(
        "http://ollama.test:11434/",
        "qwen2.5-coder:7b",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_sends_the_documented_chat_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    make(handler, num_ctx=8192).generate(REQUEST)
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://ollama.test:11434/api/chat"  # trailing slash tolerated
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5-coder:7b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert body["options"] == {"temperature": 0.0, "num_predict": 200, "num_ctx": 8192}


def test_parses_text_tokens_and_latency() -> None:
    response = make(lambda r: httpx.Response(200, json=OK_BODY)).generate(REQUEST)
    assert response.text == "```sql\nSELECT 1\n```"
    assert (response.provider, response.model) == ("ollama", "qwen2.5-coder:7b")
    assert (response.input_tokens, response.output_tokens) == (120, 15)
    assert response.latency_ms >= 0 and response.attempts == 1


def test_missing_usage_is_none_not_zero() -> None:
    body = {"message": {"role": "assistant", "content": "SELECT 1"}, "done": True}
    response = make(lambda r: httpx.Response(200, json=body)).generate(REQUEST)
    assert response.input_tokens is None and response.output_tokens is None


def expect(handler: Any, kind: LLMErrorKind) -> LLMError:
    with pytest.raises(LLMError) as info:
        make(handler).generate(REQUEST)
    assert info.value.kind is kind
    return info.value


def test_hitting_the_token_limit_is_truncation_not_a_partial_answer() -> None:
    body = {**OK_BODY, "done_reason": "length"}
    expect(lambda r: httpx.Response(200, json=body), LLMErrorKind.TRUNCATED)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>nope</html>"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"done": True}),
        httpx.Response(200, json={"message": {"content": 5}}),
        httpx.Response(200, json={"message": {"content": "   "}}),
    ],
)
def test_malformed_answers_are_bad_response(response: httpx.Response) -> None:
    expect(lambda r: response, LLMErrorKind.BAD_RESPONSE)


def test_server_not_running_gives_an_actionable_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    error = expect(handler, LLMErrorKind.UNAVAILABLE)
    assert "ollama serve" in error.message and "http://ollama.test:11434" in error.message
    assert error.retryable


def test_slow_model_is_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    assert expect(handler, LLMErrorKind.TIMEOUT).retryable


def test_unknown_model_tells_you_how_to_pull_it() -> None:
    body = {"error": "model 'qwen2.5-coder:7b' not found"}
    error = expect(lambda r: httpx.Response(404, json=body), LLMErrorKind.REJECTED)
    assert "ollama pull qwen2.5-coder:7b" in error.message
    assert not error.retryable


def test_server_errors_are_retryable() -> None:
    assert expect(lambda r: httpx.Response(503), LLMErrorKind.UNAVAILABLE).retryable
