import pytest
from fastapi.testclient import TestClient

from lanpilot.web.middleware import hostname_of
from tests.conftest import ClientFactory

LOGIN = {"username": "nobody", "password": "irrelevant-password"}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("lanpilot.test", "lanpilot.test"),
        ("LANPilot.Test:8443", "lanpilot.test"),
        ("10.0.0.5:9030", "10.0.0.5"),
        ("[fd00::1]:9030", "fd00::1"),
        ("[::1]", "::1"),
        ("", ""),
    ],
)
def test_hostname_of(header: str, expected: str) -> None:
    assert hostname_of(header) == expected


def test_foreign_host_header_is_rejected(client: TestClient) -> None:
    response = client.get("/login", headers={"Host": "evil.example"})

    assert response.status_code == 400
    assert "evil.example" not in response.text


def test_public_host_is_accepted_with_any_port(client: TestClient) -> None:
    assert client.get("/login", headers={"Host": "lanpilot.test:8443"}).status_code == 200


def test_allowed_hosts_are_accepted(make_client: ClientFactory) -> None:
    client = make_client(LANPILOT_SERVER__ALLOWED_HOSTS="10.0.0.5")

    assert client.get("/login", headers={"Host": "10.0.0.5:9030"}).status_code == 200
    assert client.get("/login", headers={"Host": "10.0.0.6:9030"}).status_code == 400


def test_health_endpoint_is_exempt_from_the_host_check(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Host": "127.0.0.1:9030"}).status_code == 200


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "https://lanpilot.test.evil.example"]
)
def test_post_from_foreign_origin_is_rejected(client: TestClient, origin: str) -> None:
    response = client.post("/login", data=LOGIN, headers={"Origin": origin})

    assert response.status_code == 403


def test_post_without_origin_and_referer_is_rejected(make_client: ClientFactory) -> None:
    client = make_client()
    del client.headers["Origin"]

    assert client.post("/login", data=LOGIN).status_code == 403


def test_post_with_only_a_same_site_referer_is_accepted(make_client: ClientFactory) -> None:
    client = make_client()
    del client.headers["Origin"]

    response = client.post("/login", data=LOGIN, headers={"Referer": "https://lanpilot.test/login"})

    assert response.status_code == 401  # reached the login handler, credentials are wrong


def test_post_with_a_foreign_referer_is_rejected(make_client: ClientFactory) -> None:
    client = make_client()
    del client.headers["Origin"]

    response = client.post(
        "/login", data=LOGIN, headers={"Referer": "https://evil.example/lanpilot.test"}
    )

    assert response.status_code == 403


def test_origin_matching_an_allowed_host_is_accepted(make_client: ClientFactory) -> None:
    client = make_client(LANPILOT_SERVER__ALLOWED_HOSTS="10.0.0.5")

    response = client.post(
        "/login",
        data=LOGIN,
        headers={"Host": "10.0.0.5:9030", "Origin": "http://10.0.0.5:9030"},
    )

    assert response.status_code == 401


def test_public_origin_is_accepted_when_the_proxy_drops_the_port(
    make_client: ClientFactory,
) -> None:
    # Nginx' "$host" forwards the host name without the port.
    client = make_client(LANPILOT_SERVER__PUBLIC_URL="https://lanpilot.test:8443")

    response = client.post(
        "/login",
        data=LOGIN,
        headers={"Host": "lanpilot.test", "Origin": "https://lanpilot.test:8443"},
    )

    assert response.status_code == 401


def test_safe_methods_need_no_origin(make_client: ClientFactory) -> None:
    client = make_client()
    del client.headers["Origin"]

    assert client.get("/login").status_code == 200


def test_security_headers_on_pages(client: TestClient) -> None:
    headers = client.get("/login").headers

    policy = headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"


def test_security_headers_on_error_responses(client: TestClient) -> None:
    rejected = client.get("/login", headers={"Host": "evil.example"})

    assert rejected.headers["x-content-type-options"] == "nosniff"


def test_static_files_are_served_and_cacheable(client: TestClient) -> None:
    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("cache-control") != "no-store"


def test_static_mount_does_not_escape_its_directory(client: TestClient) -> None:
    assert client.get("/static/../templates/base.html").status_code in (303, 404)
    assert client.get("/static/%2e%2e/templates/base.html").status_code == 404


def test_unknown_page_renders_the_error_page(client: TestClient) -> None:
    response = client.get("/no-such-page")

    assert response.status_code == 404
    assert "Page not found" in response.text
    assert response.headers["content-type"].startswith("text/html")
