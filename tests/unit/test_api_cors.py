import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from guardsql.api.app import create_app
from guardsql.api.security import ClientAuth, RateLimiter, hash_key
from guardsql.config import Settings

UI = "https://ui.example.com"
KEY = "gsql_cors-test-key"


class NoService:
    """Enough of the service for routes that never ask a question."""

    def ping(self) -> bool:
        return True

    def schema(self) -> dict[str, list[str]]:
        return {"orders": ["id"]}

    def close(self) -> None:
        pass


def make_client(origins: tuple[str, ...]) -> TestClient:
    app = create_app(
        NoService(),  # type: ignore[arg-type]
        auth=ClientAuth({"alice": hash_key(KEY)}),
        limiter=RateLimiter(0, 1),
        cors_origins=origins,
    )
    return TestClient(app)


def preflight(client: TestClient, origin: str) -> object:
    return client.options(
        "/v1/ask",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key,content-type",
        },
    )


def test_no_origins_configured_means_no_cors_headers() -> None:
    with make_client(()) as client:
        response = client.get("/health", headers={"Origin": UI})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_allowed_origin_is_echoed_and_headers_are_exposed() -> None:
    with make_client((UI,)) as client:
        response = client.get("/health", headers={"Origin": UI})
    assert response.headers["access-control-allow-origin"] == UI
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "retry-after" in exposed
    assert "x-request-id" in exposed


def test_other_origins_get_no_cors_headers() -> None:
    with make_client((UI,)) as client:
        response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_preflight_succeeds_without_an_api_key() -> None:
    with make_client((UI,)) as client:
        response = preflight(client, UI)
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response.headers["access-control-allow-origin"] == UI  # type: ignore[attr-defined]
    allowed = response.headers["access-control-allow-headers"].lower()  # type: ignore[attr-defined]
    assert "x-api-key" in allowed


def test_preflight_from_another_origin_is_refused() -> None:
    with make_client((UI,)) as client:
        response = preflight(client, "https://evil.example")
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert "access-control-allow-origin" not in response.headers  # type: ignore[attr-defined]


def test_error_responses_carry_cors_headers_so_the_page_can_read_them() -> None:
    with make_client((UI,)) as client:
        response = client.get("/v1/schema", headers={"Origin": UI})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == UI
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_a_server_error_still_carries_cors_headers() -> None:
    class Exploding(NoService):
        def ask(self, question: str) -> object:
            raise RuntimeError("internal detail")

    app = create_app(
        Exploding(),  # type: ignore[arg-type]
        auth=ClientAuth({"alice": hash_key(KEY)}),
        limiter=RateLimiter(0, 1),
        cors_origins=(UI,),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/ask",
            json={"question": "q"},
            headers={"Origin": UI, "X-API-Key": KEY},
        )
    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == UI
    assert response.json()["error"]["code"] == "INTERNAL"
    assert "internal detail" not in response.text


def test_settings_parse_a_comma_separated_list() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins=f"{UI}, http://localhost:5173")
    assert settings.cors_origin_list == [UI, "http://localhost:5173"]
    assert Settings(_env_file=None).cors_origin_list == []


@pytest.mark.parametrize(
    "bad",
    ["*", "ui.example.com", f"{UI}/", f"{UI},*"],
    ids=["wildcard", "no-scheme", "trailing-slash", "wildcard-in-list"],
)
def test_settings_reject_unsafe_origins(bad: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_allowed_origins=bad)
