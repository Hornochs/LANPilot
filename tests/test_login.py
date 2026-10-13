import json

import pytest
from fastapi.testclient import TestClient

from lanpilot.auth.roles import Role
from lanpilot.auth.sessions import hash_token
from lanpilot.web.routes_auth import safe_next
from tests.conftest import PASSWORD, ClientFactory, Query, UserFactory, csrf_token, sign_in

COOKIE = "__Host-lanpilot_session"


def test_pages_redirect_to_login_when_signed_out(client: TestClient) -> None:
    assert client.get("/").headers["location"] == "/login"

    response = client.get("/admin/users?sort=name")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fadmin%2Fusers%3Fsort%3Dname"


def test_unsafe_requests_get_401_instead_of_a_redirect(client: TestClient) -> None:
    assert client.post("/account/password", data={}).status_code == 401
    assert client.post("/logout").status_code == 401
    assert client.get("/", headers={"HX-Request": "true"}).status_code == 401


def test_successful_login_sets_a_hardened_cookie(
    client: TestClient, make_user: UserFactory
) -> None:
    make_user("alice")

    response = client.post(
        "/login", data={"username": "Alice ", "password": PASSWORD, "next": "/account"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=604800"):
        assert attribute in cookie
    assert "Domain" not in cookie
    assert client.get("/").status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(
    client: TestClient, make_user: UserFactory
) -> None:
    make_user("alice")

    wrong = client.post("/login", data={"username": "alice", "password": "wrong-password-1"})
    unknown = client.post("/login", data={"username": "mallory", "password": "wrong-password-1"})

    assert wrong.status_code == unknown.status_code == 401
    assert "Wrong user name or password." in wrong.text
    assert "Wrong user name or password." in unknown.text
    assert "set-cookie" not in wrong.headers


def test_disabled_user_cannot_sign_in(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    query("UPDATE users SET is_active = 0 WHERE username = 'alice'")

    response = client.post("/login", data={"username": "alice", "password": PASSWORD})

    assert response.status_code == 401


def test_typed_user_name_is_escaped_on_the_error_page(client: TestClient) -> None:
    payload = '"><script>alert(1)</script>'

    response = client.post("/login", data={"username": payload, "password": "irrelevant-pw"})

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/account", "/account"),
        ("/admin/users?x=1", "/admin/users?x=1"),
        ("//evil.example", "/"),
        ("https://evil.example", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("/ok\r\nSet-Cookie: x=1", "/"),
        ("", "/"),
    ],
)
def test_safe_next(value: str, expected: str) -> None:
    assert safe_next(value) == expected


def test_login_does_not_redirect_off_site(client: TestClient, make_user: UserFactory) -> None:
    make_user("alice")

    response = client.post(
        "/login", data={"username": "alice", "password": PASSWORD, "next": "//evil.example/x"}
    )

    assert response.headers["location"] == "/"


def test_only_a_hash_of_the_session_token_is_stored(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    token = client.cookies[COOKIE]

    [(stored,)] = query("SELECT token_hash FROM sessions")

    assert stored == hash_token(token)
    assert token not in stored


def test_logout_requires_the_csrf_token_and_ends_the_session(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    token = client.cookies[COOKIE]

    assert client.post("/logout").status_code == 403
    assert query("SELECT count(*) FROM sessions") == [(1,)]

    response = client.post("/logout", data={"csrf_token": csrf_token(client)})

    assert response.status_code == 303
    assert response.headers["location"] == "/login?notice=signed_out"
    assert query("SELECT count(*) FROM sessions") == [(0,)]
    # A copy of the old cookie is worthless after logout.
    client.cookies.set(COOKIE, token)
    assert client.get("/").status_code == 303


def test_signing_in_again_replaces_the_old_session(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    first = client.cookies[COOKIE]

    sign_in(client, "alice")

    assert client.cookies[COOKIE] != first
    assert query("SELECT token_hash FROM sessions") == [(hash_token(client.cookies[COOKIE]),)]


def test_session_expires_after_inactivity(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    query("UPDATE sessions SET last_seen_at = datetime('now', '-13 hours')")

    assert client.get("/").status_code == 303
    assert query("SELECT count(*) FROM sessions") == [(0,)]


def test_session_expires_at_its_absolute_lifetime(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    query("UPDATE sessions SET expires_at = datetime('now', '-1 minute')")

    assert client.get("/").status_code == 303


def test_session_of_a_disabled_user_stops_working(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    query("UPDATE users SET is_active = 0")

    assert client.get("/").status_code == 303


def test_forged_cookie_values_are_ignored(client: TestClient) -> None:
    for value in ("guess", "x" * 5000, "' OR 1=1 --"):
        client.cookies.set(COOKIE, value)
        assert client.get("/").status_code == 303


def test_login_is_throttled_even_for_the_right_password(
    client: TestClient, make_user: UserFactory
) -> None:
    make_user("alice")
    for _ in range(5):
        client.post("/login", data={"username": "alice", "password": "wrong-password-1"})

    response = client.post("/login", data={"username": "alice", "password": PASSWORD})

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert "Too many failed attempts" in response.text
    assert "set-cookie" not in response.headers


def test_logins_are_audited_without_passwords(
    client: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    client.post("/login", data={"username": "alice", "password": "wrong-password-1"})
    sign_in(client, "alice")

    rows = query("SELECT actor_name, action, success, detail FROM audit_log ORDER BY id")

    assert [(name, action, success) for name, action, success, _ in rows] == [
        ("alice", "login", 0),
        ("alice", "login", 1),
    ]
    assert json.loads(rows[0][3]) == {"reason": "bad credentials"}
    assert "wrong-password-1" not in str(rows)
    assert PASSWORD not in str(rows)


def test_signed_in_user_is_sent_away_from_the_login_page(
    client: TestClient, make_user: UserFactory
) -> None:
    make_user("alice")
    sign_in(client, "alice")

    assert client.get("/login?next=/account").headers["location"] == "/account"


def test_cookie_and_redirects_under_a_sub_path(
    make_client: ClientFactory, make_user: UserFactory
) -> None:
    make_user("alice")
    client = make_client(LANPILOT_SERVER__PUBLIC_URL="https://lanpilot.test/pilot")

    # The client's base URL already ends in /pilot, so "/" requests /pilot/.
    assert client.get("/").headers["location"] == "/pilot/login"
    assert client.get("/account").headers["location"] == "/pilot/login?next=%2Faccount"
    response = client.post(
        "/login", data={"username": "alice", "password": PASSWORD, "next": "/account"}
    )

    assert response.headers["location"] == "/pilot/account"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Secure-lanpilot_session=")
    assert "Path=/pilot" in cookie
    page = client.get("/")
    assert page.status_code == 200
    assert 'href="/pilot/static/css/app.css"' in page.text


def test_plain_http_deployment_works_and_warns(
    make_client: ClientFactory, make_user: UserFactory
) -> None:
    make_user("alice")
    client = make_client(LANPILOT_SERVER__PUBLIC_URL="http://10.0.0.5:9030")

    assert "not reached over HTTPS" in client.get("/login").text
    response = client.post("/login", data={"username": "alice", "password": PASSWORD})

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("lanpilot_session=")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
    assert client.get("/").status_code == 200


def test_https_deployment_shows_no_transport_warning(client: TestClient) -> None:
    assert "not reached over HTTPS" not in client.get("/login").text


def test_roles_are_ordered() -> None:
    assert Role.ADMIN.allows(Role.OPERATOR)
    assert Role.OPERATOR.allows(Role.VIEWER)
    assert not Role.VIEWER.allows(Role.OPERATOR)
    assert not Role.OPERATOR.allows(Role.ADMIN)
