from fastapi.testclient import TestClient

from lanpilot.app import create_app
from lanpilot.config import Settings


def test_healthz_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_error_without_database(settings: Settings) -> None:
    # No migration has run and the data directory does not exist, so SQLite cannot open the file.
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "error"}


def test_api_documentation_is_not_exposed(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
