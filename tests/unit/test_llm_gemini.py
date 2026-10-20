import json
from typing import Any

import httpx
import pytest

from guardsql.llm.errors import LLMError, LLMErrorKind
from guardsql.llm.gemini import GeminiClient
from guardsql.llm.types import LLMRequest

KEY = "AIza-secret-key-123"
REQUEST = LLMRequest(system="SYS", user="USER", max_output_tokens=300, temperature=0.0)
OK_BODY = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "```sql\nSELECT 1\n```"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 10, "totalTokenCount": 210},
}


def make(handler: Any, **kwargs: Any) -> GeminiClient:
    return GeminiClient(
        KEY, "gemini-3.5-flash-lite", transport=httpx.MockTransport(handler), **kwargs
    )


def test_key_is_sent_in_a_header_and_never_in_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    make(handler).generate(REQUEST)
    (request,) = seen
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
    )


def test_request_body_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    make(handler).generate(REQUEST)
    body = json.loads(seen[0].content)
    assert body == {
        "systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "USER"}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 300},
    }
    assert KEY not in seen[0].content.decode()


def test_parses_text_and_tokens() -> None:
    response = make(lambda r: httpx.Response(200, json=OK_BODY)).generate(REQUEST)
    assert response.text == "```sql\nSELECT 1\n```"
    assert (response.provider, response.model) == ("gemini", "gemini-3.5-flash-lite")
    assert (response.input_tokens, response.output_tokens) == (200, 10)


def test_thinking_parts_are_dropped_and_their_tokens_are_counted() -> None:
    body = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Let me think about joins...", "thought": True},
                        {"text": "SELECT 1"},
                        {"text": " FROM orders"},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 50,
            "candidatesTokenCount": 8,
            "thoughtsTokenCount": 300,
        },
    }
    response = make(lambda r: httpx.Response(200, json=body)).generate(REQUEST)
    assert response.text == "SELECT 1 FROM orders"
    assert response.output_tokens == 308


def expect(handler: Any, kind: LLMErrorKind) -> LLMError:
    with pytest.raises(LLMError) as info:
        make(handler).generate(REQUEST)
    assert info.value.kind is kind
    assert KEY not in str(info.value)
    return info.value


def test_max_tokens_is_truncation() -> None:
    body = {
        "candidates": [
            {"content": {"parts": [{"text": "SELECT a, b,"}]}, "finishReason": "MAX_TOKENS"}
        ]
    }
    expect(lambda r: httpx.Response(200, json=body), LLMErrorKind.TRUNCATED)


def test_blocked_prompt() -> None:
    body = {"promptFeedback": {"blockReason": "SAFETY"}}
    error = expect(lambda r: httpx.Response(200, json=body), LLMErrorKind.BLOCKED)
    assert "SAFETY" in error.message


def test_candidate_without_text_and_a_safety_stop_is_blocked() -> None:
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
    expect(lambda r: httpx.Response(200, json=body), LLMErrorKind.BLOCKED)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json=[]),
        httpx.Response(200, text="oops"),
        httpx.Response(
            200, json={"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}
        ),
    ],
)
def test_malformed_answers_are_bad_response(response: httpx.Response) -> None:
    expect(lambda r: response, LLMErrorKind.BAD_RESPONSE)


def test_rate_limit_carries_retry_after() -> None:
    body = {
        "error": {
            "code": 429,
            "message": "Quota exceeded for this model.",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    error = expect(
        lambda r: httpx.Response(429, json=body, headers={"Retry-After": "12"}),
        LLMErrorKind.RATE_LIMITED,
    )
    assert error.retry_after_s == 12.0 and error.retryable
    assert "Quota exceeded" in error.message


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}])
def test_rate_limit_without_a_usable_retry_after(headers: dict[str, str]) -> None:
    error = expect(lambda r: httpx.Response(429, headers=headers), LLMErrorKind.RATE_LIMITED)
    assert error.retry_after_s is None


@pytest.mark.parametrize("status", [401, 403])
def test_bad_key_is_an_auth_error_that_is_not_retried(status: int) -> None:
    error = expect(
        lambda r: httpx.Response(status, json={"error": {"message": "bad"}}), LLMErrorKind.AUTH
    )
    assert not error.retryable


def test_other_client_errors_are_rejections_with_the_providers_reason() -> None:
    body = {"error": {"message": "models/nope is not found for API version v1beta"}}
    error = expect(lambda r: httpx.Response(404, json=body), LLMErrorKind.REJECTED)
    assert "is not found" in error.message and not error.retryable


def test_server_error_and_network_failures_are_retryable() -> None:
    assert expect(lambda r: httpx.Response(500), LLMErrorKind.UNAVAILABLE).retryable

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert expect(refuse, LLMErrorKind.UNAVAILABLE).retryable


def test_missing_key_fails_at_construction_not_at_first_request() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiClient("", "gemini-3.5-flash-lite")
