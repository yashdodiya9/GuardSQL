import pytest

from guardsql.llm.errors import LLMError, LLMErrorKind
from guardsql.llm.fake import FakeLLM
from guardsql.llm.retry import RetryingClient
from guardsql.llm.types import LLMRequest

REQUEST = LLMRequest(system="s", user="u")
RATE = LLMError(LLMErrorKind.RATE_LIMITED, "slow down")
DOWN = LLMError(LLMErrorKind.UNAVAILABLE, "down")


def wrap(fake: FakeLLM, sleeps: list[float], **kwargs: float) -> RetryingClient:
    return RetryingClient(fake, sleep=sleeps.append, jitter=lambda: 0.0, **kwargs)


def test_retries_transient_errors_then_succeeds_and_reports_attempts() -> None:
    fake = FakeLLM([DOWN, RATE, "SELECT 1"])
    sleeps: list[float] = []
    response = wrap(fake, sleeps).generate(REQUEST)
    assert response.text == "SELECT 1" and response.attempts == 3
    assert sleeps == [1.0, 2.0]  # exponential: 1s, then 2s
    assert len(fake.requests) == 3


def test_gives_up_after_max_attempts_with_the_last_error() -> None:
    fake = FakeLLM([DOWN, DOWN, RATE])
    with pytest.raises(LLMError) as info:
        wrap(fake, []).generate(REQUEST)
    assert info.value.kind is LLMErrorKind.RATE_LIMITED
    assert len(fake.requests) == 3


@pytest.mark.parametrize(
    "kind",
    [
        LLMErrorKind.AUTH,
        LLMErrorKind.REJECTED,
        LLMErrorKind.TRUNCATED,
        LLMErrorKind.BLOCKED,
        LLMErrorKind.BAD_RESPONSE,
    ],
)
def test_permanent_errors_are_not_retried(kind: LLMErrorKind) -> None:
    fake = FakeLLM([LLMError(kind, "no"), "never reached"])
    sleeps: list[float] = []
    with pytest.raises(LLMError):
        wrap(fake, sleeps).generate(REQUEST)
    assert len(fake.requests) == 1 and sleeps == []


def test_waits_as_long_as_the_server_asked() -> None:
    fake = FakeLLM([LLMError(LLMErrorKind.RATE_LIMITED, "wait", retry_after_s=7.0), "ok"])
    sleeps: list[float] = []
    wrap(fake, sleeps).generate(REQUEST)
    assert sleeps == [7.0]


def test_does_not_wait_for_a_server_that_asks_for_too_long() -> None:
    fake = FakeLLM([LLMError(LLMErrorKind.RATE_LIMITED, "wait", retry_after_s=300.0), "ok"])
    sleeps: list[float] = []
    with pytest.raises(LLMError):
        wrap(fake, sleeps, max_delay_s=20.0).generate(REQUEST)
    assert sleeps == [] and len(fake.requests) == 1


def test_backoff_is_capped_and_jittered() -> None:
    fake = FakeLLM([DOWN, DOWN, DOWN, DOWN, "ok"])
    sleeps: list[float] = []
    client = RetryingClient(
        fake,
        max_attempts=5,
        base_delay_s=10.0,
        max_delay_s=25.0,
        sleep=sleeps.append,
        jitter=lambda: 1.0,
    )
    client.generate(REQUEST)
    assert sleeps == [12.5, 25.0, 25.0, 25.0]  # 10*1.25, then capped at max_delay_s


def test_exposes_inner_provider_and_model() -> None:
    client = wrap(FakeLLM(["x"]), [])
    assert (client.provider, client.model) == ("fake", "fake-1")
