import pytest
from fastapi.testclient import TestClient

from lanpilot.auth.roles import Role
from tests.conftest import PASSWORD, ClientFactory, Query, UserFactory, csrf_token, sign_in

NEW_PASSWORD = "an entirely new passphrase"


def change_password(client: TestClient, **fields: str) -> int:
    data = {
        "csrf_token": csrf_token(client),
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "new_password_repeat": NEW_PASSWORD,
        **fields,
    }
    return client.post("/account/password", data=data).status_code


def test_change_password_signs_out_other_sessions(
    make_client: ClientFactory, make_user: UserFactory, query: Query
) -> None:
    make_user("alice")
    here, elsewhere = make_client(), make_client()
    sign_in(here, "alice")
    sign_in(elsewhere, "alice")

    assert change_password(here) == 303

    assert here.get("/account").status_code == 200  # this browser got a fresh session
    assert elsewhere.get("/account").status_code == 303
    assert query("SELECT count(*) FROM sessions") == [(1,)]
    fresh = make_client()
    assert fresh.post("/login", data={"username": "alice", "password": PASSWORD}).status_code == 401
    sign_in(fresh, "alice", NEW_PASSWORD)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"current_password": "not-my-password"}, "current password is wrong"),
        ({"new_password_repeat": "something different"}, "do not match"),
        ({"new_password": "short", "new_password_repeat": "short"}, "at least 10 characters"),
        ({"new_password": "alice", "new_password_repeat": "alice"}, "at least 10 characters"),
    ],
)
def test_change_password_rejections(
    client: TestClient, make_user: UserFactory, fields: dict[str, str], message: str
) -> None:
    make_user("alice")
    sign_in(client, "alice")
    data = {
        "csrf_token": csrf_token(client),
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "new_password_repeat": NEW_PASSWORD,
        **fields,
    }

    response = client.post("/account/password", data=data)

    assert response.status_code == 400
    assert message in response.text
    assert client.get("/account").status_code == 200  # still signed in, password unchanged


def test_csrf_token_is_required_and_checked(client: TestClient, make_user: UserFactory) -> None:
    make_user("alice")
    sign_in(client, "alice")
    fields = {
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "new_password_repeat": NEW_PASSWORD,
    }

    assert client.post("/account/password", data=fields).status_code == 403
    forged = client.post("/account/password", data={**fields, "csrf_token": "forged"})
    assert forged.status_code == 403
    assert "expired" in forged.text


def test_csrf_token_is_accepted_as_a_header(client: TestClient, make_user: UserFactory) -> None:
    make_user("alice")
    sign_in(client, "alice")
    fields = {
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "new_password_repeat": NEW_PASSWORD,
    }

    response = client.post(
        "/account/password", data=fields, headers={"X-CSRF-Token": csrf_token(client)}
    )

    assert response.status_code == 303


def test_csrf_token_of_another_session_is_rejected(
    make_client: ClientFactory, make_user: UserFactory
) -> None:
    make_user("alice")
    make_user("bob")
    alice, bob = make_client(), make_client()
    sign_in(alice, "alice")
    sign_in(bob, "bob")

    response = alice.post("/logout", data={"csrf_token": csrf_token(bob)})

    assert response.status_code == 403


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
def test_user_management_is_admin_only(
    client: TestClient, make_user: UserFactory, query: Query, role: Role
) -> None:
    make_user("alice", role)
    make_user("root", Role.ADMIN)
    sign_in(client, "alice")
    token = csrf_token(client)
    [(root_id,)] = query("SELECT id FROM users WHERE username = 'root'")

    assert client.get("/admin/users").status_code == 403
    attempts = [
        ("/admin/users", {"username": "eve", "password": PASSWORD, "role": "admin"}),
        (f"/admin/users/{root_id}/role", {"role": "viewer"}),
        (f"/admin/users/{root_id}/active", {"active": "0"}),
        (f"/admin/users/{root_id}/password", {"password": NEW_PASSWORD}),
        (f"/admin/users/{root_id}/delete", {}),
    ]
    for path, data in attempts:
        assert client.post(path, data={**data, "csrf_token": token}).status_code == 403
    assert query("SELECT username, role, is_active FROM users ORDER BY username") == [
        ("alice", role.value, 1),
        ("root", "admin", 1),
    ]
    assert "Users" not in client.get("/").text  # no navigation entry either


@pytest.fixture
def admin(client: TestClient, make_user: UserFactory) -> TestClient:
    make_user("root", Role.ADMIN)
    sign_in(client, "root")
    return client


def post(client: TestClient, path: str, **data: str) -> int:
    return client.post(path, data={**data, "csrf_token": csrf_token(client)}).status_code


def test_admin_creates_a_user_who_can_sign_in(
    admin: TestClient, make_client: ClientFactory, query: Query
) -> None:
    assert post(admin, "/admin/users", username="Bob", password=PASSWORD, role="operator") == 303

    assert query("SELECT username, role FROM users WHERE username = 'bob'") == [("bob", "operator")]
    sign_in(make_client(), "bob")
    [(action, actor, detail)] = query(
        "SELECT action, actor_name, detail FROM audit_log WHERE action = 'user.create'"
    )
    assert (action, actor) == ("user.create", "root")
    assert PASSWORD not in detail


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"username": "root", "password": PASSWORD, "role": "viewer"}, "already exists"),
        ({"username": "x", "password": PASSWORD, "role": "viewer"}, "3 to 64 characters"),
        ({"username": "<b>bob</b>", "password": PASSWORD, "role": "viewer"}, "3 to 64 characters"),
        ({"username": "bob", "password": "short", "role": "viewer"}, "at least 10"),
    ],
)
def test_create_user_rejections(
    admin: TestClient, query: Query, data: dict[str, str], message: str
) -> None:
    response = admin.post("/admin/users", data={**data, "csrf_token": csrf_token(admin)})

    assert response.status_code == 400
    assert message in response.text
    assert query("SELECT count(*) FROM users") == [(1,)]


def test_unknown_role_is_rejected(admin: TestClient, query: Query) -> None:
    assert post(admin, "/admin/users", username="bob", password=PASSWORD, role="superuser") == 400
    assert query("SELECT count(*) FROM users") == [(1,)]


def test_last_admin_cannot_be_demoted_disabled_or_deleted(admin: TestClient, query: Query) -> None:
    [(root_id,)] = query("SELECT id FROM users")

    assert post(admin, f"/admin/users/{root_id}/role", role="viewer") == 400
    assert post(admin, f"/admin/users/{root_id}/active", active="0") == 400
    assert post(admin, f"/admin/users/{root_id}/delete") == 400

    assert query("SELECT role, is_active FROM users") == [("admin", 1)]
    assert admin.get("/admin/users").status_code == 200


def test_admin_can_step_down_once_another_admin_exists(
    admin: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("second", Role.ADMIN)
    [(root_id,)] = query("SELECT id FROM users WHERE username = 'root'")

    assert post(admin, f"/admin/users/{root_id}/role", role="viewer") == 303

    assert admin.get("/admin/users").status_code == 403  # the change applies immediately


def test_disabled_admin_does_not_count_as_a_remaining_admin(
    admin: TestClient, make_user: UserFactory, query: Query
) -> None:
    make_user("second", Role.ADMIN)
    query("UPDATE users SET is_active = 0 WHERE username = 'second'")
    [(root_id,)] = query("SELECT id FROM users WHERE username = 'root'")

    assert post(admin, f"/admin/users/{root_id}/role", role="operator") == 400


def test_disabling_a_user_ends_their_sessions(
    admin: TestClient, make_client: ClientFactory, make_user: UserFactory, query: Query
) -> None:
    make_user("bob")
    bob = make_client()
    sign_in(bob, "bob")
    [(bob_id,)] = query("SELECT id FROM users WHERE username = 'bob'")

    assert post(admin, f"/admin/users/{bob_id}/active", active="0") == 303
    assert bob.get("/").status_code == 303

    assert post(admin, f"/admin/users/{bob_id}/active", active="1") == 303
    sign_in(bob, "bob")


def test_password_reset_ends_sessions_and_replaces_the_password(
    admin: TestClient, make_client: ClientFactory, make_user: UserFactory, query: Query
) -> None:
    make_user("bob")
    bob = make_client()
    sign_in(bob, "bob")
    [(bob_id,)] = query("SELECT id FROM users WHERE username = 'bob'")

    assert post(admin, f"/admin/users/{bob_id}/password", password=NEW_PASSWORD) == 303

    assert bob.get("/").status_code == 303
    assert bob.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 401
    sign_in(bob, "bob", NEW_PASSWORD)


def test_admin_cannot_reset_own_password_without_knowing_it(
    admin: TestClient, query: Query
) -> None:
    [(root_id,)] = query("SELECT id FROM users")

    assert post(admin, f"/admin/users/{root_id}/password", password=NEW_PASSWORD) == 400


def test_delete_user(
    admin: TestClient, make_client: ClientFactory, make_user: UserFactory, query: Query
) -> None:
    make_user("bob")
    bob = make_client()
    sign_in(bob, "bob")
    [(bob_id,)] = query("SELECT id FROM users WHERE username = 'bob'")
    [(root_id,)] = query("SELECT id FROM users WHERE username = 'root'")

    assert post(admin, f"/admin/users/{root_id}/delete") == 400  # not yourself
    assert post(admin, f"/admin/users/{bob_id}/delete") == 303

    assert query("SELECT username FROM users") == [("root",)]
    assert bob.get("/").status_code == 303
    # Audit entries about and by the deleted user survive.
    assert query("SELECT count(*) FROM audit_log WHERE actor_name = 'bob'") == [(1,)]
    assert query("SELECT count(*) FROM audit_log WHERE action = 'user.delete'") == [(1,)]


def test_actions_on_unknown_users_return_404(admin: TestClient) -> None:
    assert post(admin, "/admin/users/9999/role", role="viewer") == 404
    assert post(admin, "/admin/users/9999/delete") == 404
    assert post(admin, "/admin/users/not-a-number/delete") == 422


def test_user_list_shows_accounts_but_no_hashes(admin: TestClient, make_user: UserFactory) -> None:
    make_user("bob", Role.OPERATOR)

    page = admin.get("/admin/users").text

    assert "bob" in page
    assert "(you)" in page
    assert "argon2" not in page
