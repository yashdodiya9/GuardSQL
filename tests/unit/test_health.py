from fastapi.testclient import TestClient

from guardsql import __version__
from guardsql.main import app

client = TestClient(app)


def test_health_returns_ok_and_version() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
