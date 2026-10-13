import json
import re

import pytest
from fastapi.testclient import TestClient

from lanpilot.auth.roles import Role
from lanpilot.core import secrets
from tests.conftest import SECRET_KEY, ClientFactory, Query, UserFactory, csrf_token, sign_in

STORED_PASSWORD = "S3cret-Rcon-Passw0rd"
BOX = secrets.SecretBox(secrets._decode_key(SECRET_KEY, "test"))


@pytest.fixture
def admin(make_client: ClientFactory, make_user: UserFactory) -> TestClient:
    make_user("root", Role.ADMIN)
    client = make_client(LANPILOT_ENABLE_DEMO_ADAPTER="true")
    sign_in(client, "root")
    return client


def demo_form(**overrides: str) -> dict[str, str]:
    fields = {
        "adapter": "demo",
        "name": "Demo One",
        "enabled": "1",
        "c_host": "demo.lan",
        "c_port": "27015",
        "c_password": "demo",
        "c_behaviour": "normal",
        "c_players": "3",
    }
    fields.update(overrides)
    return {key: value for key, value in fields.items() if value is not None}


def submit(client: TestClient, path: str, **fields: str | None) -> tuple[int, str]:
    data = {key: value for key, value in fields.items() if value is not None}
    response = client.post(path, data={**data, "csrf_token": csrf_token(client)})
    return response.status_code, response.text


def create(client: TestClient, **overrides: str | None) -> tuple[int, str]:
    return submit(client, "/admin/servers/new", **{**demo_form(), **overrides})


def decrypt(row: tuple[str, str, int, str]) -> dict[str, str]:
    adapter_id, host, port, credentials = row
    return BOX.decrypt(credentials, context=secrets.server_context(adapter_id, host, port))


SELECT_SECRET = "SELECT adapter_id, host, port, credentials FROM servers"


def test_create_server_stores_settings_and_encrypted_credentials(
    admin: TestClient, query: Query
) -> None:
    status, _ = create(admin, c_password=STORED_PASSWORD)

    assert status == 303
    [(name, adapter, host, port, settings, credentials, enabled)] = query(
        "SELECT name, adapter_id, host, port, settings, credentials, enabled FROM servers"
    )
    assert (name, adapter, host, port, enabled) == ("Demo One", "demo", "demo.lan", 27015, 1)
    assert json.loads(settings) == {"behaviour": "normal", "players": 3}
    assert credentials.startswith("v1:")
    assert STORED_PASSWORD not in credentials + settings
    assert decrypt(query(SELECT_SECRET)[0]) == {"password": STORED_PASSWORD}


def test_stored_password_never_appears_in_any_page(admin: TestClient, query: Query) -> None:
    create(admin, c_password=STORED_PASSWORD)
    [(server_id,)] = query("SELECT id FROM servers")

    for path in ("/", "/admin/servers", f"/admin/servers/{server_id}/edit"):
        page = admin.get(path)
        assert page.status_code == 200
        assert STORED_PASSWORD not in page.text
    edit = admin.get(f"/admin/servers/{server_id}/edit").text
    assert re.search(r'name="c_password" type="password"[^>]*placeholder="unchanged"', edit)
    assert 'value="demo.lan"' in edit


def test_form_is_generated_from_the_connection_schema(admin: TestClient) -> None:
    page = admin.get("/admin/servers/new?adapter=demo").text

    assert 'name="c_host" type="text" value="demo.invalid"' in page
    assert 'name="c_port" type="number" value="27015"' in page
    assert 'min="1"' in page and 'max="65535"' in page
    assert 'name="c_password" type="password"' in page
    assert '<option value="slow">slow</option>' in page
    assert "Remote-control password" in page
    assert "The simulated server accepts the password" in page


def test_game_selection_lists_offered_adapters(admin: TestClient) -> None:
    page = admin.get("/admin/servers/new").text

    assert 'href="/admin/servers/new?adapter=demo"' in page
    assert "Demo server (simulated)" in page


def test_demo_adapter_is_not_offered_unless_enabled(
    make_client: ClientFactory, make_user: UserFactory, query: Query
) -> None:
    make_user("root", Role.ADMIN)
    client = make_client()  # enable_demo_adapter defaults to false
    sign_in(client, "root")

    assert "adapter=demo" not in client.get("/admin/servers/new").text
    assert client.get("/admin/servers/new?adapter=demo").status_code == 404
    assert create(client)[0] == 404
    assert query("SELECT count(*) FROM servers") == [(0,)]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"c_host": "http://demo.lan/x"}, "host name or an IP address"),
        ({"c_port": "70000"}, "less than or equal to 65535"),
        ({"c_port": "abc"}, "valid integer"),
        ({"c_password": ""}, "Field required"),
        ({"c_behaviour": "chaotic"}, "Input should be"),
        ({"name": "   "}, "Give the server a name"),
        ({"name": "x" * 101}, "at most 100 characters"),
    ],
)
def test_invalid_input_is_reported_per_field(
    admin: TestClient, query: Query, overrides: dict[str, str], message: str
) -> None:
    status, page = create(admin, **overrides)

    assert status == 400
    assert message in page
    assert query("SELECT count(*) FROM servers") == [(0,)]


def test_rejected_form_keeps_entries_but_not_the_password(admin: TestClient) -> None:
    status, page = create(admin, c_port="70000", c_password=STORED_PASSWORD, c_host="keep.lan")

    assert status == 400
    assert 'value="keep.lan"' in page
    assert 'value="70000"' in page
    assert STORED_PASSWORD not in page


def test_form_fields_outside_the_schema_are_ignored(admin: TestClient, query: Query) -> None:
    status, _ = create(admin, c_admin="1", c_settings='{"x": 1}')

    assert status == 303
    assert query("SELECT settings FROM servers") == [('{"behaviour": "normal", "players": 3}',)]


def test_duplicate_name_is_rejected(admin: TestClient, query: Query) -> None:
    assert create(admin)[0] == 303

    status, page = create(admin, c_host="other.lan")

    assert status == 400
    assert "already exists" in page
    assert query("SELECT count(*) FROM servers") == [(1,)]


def test_edit_without_password_keeps_the_stored_one(admin: TestClient, query: Query) -> None:
    create(admin, c_password=STORED_PASSWORD)
    [(server_id,)] = query("SELECT id FROM servers")

    status, _ = submit(
        admin,
        f"/admin/servers/{server_id}/edit",
        **{**demo_form(), "name": "Renamed", "c_password": "", "c_players": "5", "enabled": None},
    )

    assert status == 303
    assert query("SELECT name, enabled, settings FROM servers") == [
        ("Renamed", 0, '{"behaviour": "normal", "players": 5}')
    ]
    assert decrypt(query(SELECT_SECRET)[0]) == {"password": STORED_PASSWORD}


def test_changing_the_address_requires_the_password_again(admin: TestClient, query: Query) -> None:
    create(admin, c_password=STORED_PASSWORD)
    [(server_id,)] = query("SELECT id FROM servers")
    path = f"/admin/servers/{server_id}/edit"

    status, page = submit(admin, path, **{**demo_form(), "c_host": "evil.lan", "c_password": ""})

    assert status == 400
    assert "The address changed, so this has to be entered again." in page
    assert query("SELECT host FROM servers") == [("demo.lan",)]

    status, _ = submit(
        admin, path, **{**demo_form(), "c_host": "moved.lan", "c_password": "new-pw-1"}
    )

    assert status == 303
    assert decrypt(query(SELECT_SECRET)[0]) == {"password": "new-pw-1"}
    assert query("SELECT host FROM servers") == [("moved.lan",)]


def test_credentials_copied_to_another_address_do_not_decrypt(
    admin: TestClient, query: Query
) -> None:
    """Someone with write access to the database repoints a server at their own host."""
    create(admin, c_password=STORED_PASSWORD)
    [(server_id,)] = query("SELECT id FROM servers")
    query("UPDATE servers SET host = 'attacker.lan'")

    status, page = submit(admin, f"/admin/servers/{server_id}/test")

    assert status == 200
    assert "cannot be decrypted" in page
    assert STORED_PASSWORD not in page


def test_connection_test_reports_status_and_remote_control(admin: TestClient, query: Query) -> None:
    create(admin)
    [(server_id,)] = query("SELECT id FROM servers")

    status, page = submit(admin, f"/admin/servers/{server_id}/test")

    assert status == 200
    assert "Connection test for Demo One" in page
    assert "Foundry, 3/6 players" in page
    assert "Remote control works: the server lists 6 maps." in page
    assert 'class="banner success"' in page


def test_connection_test_reports_a_wrong_password(admin: TestClient, query: Query) -> None:
    create(admin, c_password=STORED_PASSWORD)
    [(server_id,)] = query("SELECT id FROM servers")

    _, page = submit(admin, f"/admin/servers/{server_id}/test")

    assert "Remote control failed: The server refused the remote-control password." in page
    assert 'class="banner error"' in page
    assert STORED_PASSWORD not in page


def test_connection_test_reports_an_unreachable_server(admin: TestClient, query: Query) -> None:
    create(admin, c_behaviour="offline")
    [(server_id,)] = query("SELECT id FROM servers")

    _, page = submit(admin, f"/admin/servers/{server_id}/test")

    assert "Status query failed: The server did not answer." in page
    assert 'class="banner error"' in page


def test_delete_server(admin: TestClient, query: Query) -> None:
    create(admin)
    [(server_id,)] = query("SELECT id FROM servers")

    assert submit(admin, f"/admin/servers/{server_id}/delete")[0] == 303

    assert query("SELECT count(*) FROM servers") == [(0,)]
    assert admin.get(f"/admin/servers/{server_id}/edit").status_code == 404


def test_server_of_a_removed_adapter_can_still_be_deleted(admin: TestClient, query: Query) -> None:
    create(admin)
    [(server_id,)] = query("SELECT id FROM servers")
    query("UPDATE servers SET adapter_id = 'uninstalled_game'")

    listing = admin.get("/admin/servers").text
    assert "not installed (uninstalled_game)" in listing
    assert admin.get(f"/admin/servers/{server_id}/edit").status_code == 404
    assert admin.get("/").status_code == 200
    assert submit(admin, f"/admin/servers/{server_id}/delete")[0] == 303


def test_server_names_are_escaped(admin: TestClient) -> None:
    create(admin, name="<script>alert(1)</script>")

    for path in ("/", "/admin/servers"):
        page = admin.get(path).text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_changes_are_audited_without_secrets(admin: TestClient, query: Query) -> None:
    create(admin, c_password=STORED_PASSWORD)
    [(server_id,)] = query("SELECT id FROM servers")
    submit(
        admin,
        f"/admin/servers/{server_id}/edit",
        **{**demo_form(), "name": "Renamed", "c_password": ""},
    )
    submit(admin, f"/admin/servers/{server_id}/delete")

    rows = query(
        "SELECT action, actor_name, target_id, detail FROM audit_log"
        " WHERE action LIKE 'server.%' ORDER BY id"
    )

    assert [(action, actor, target) for action, actor, target, _ in rows] == [
        ("server.create", "root", str(server_id)),
        ("server.update", "root", str(server_id)),
        ("server.delete", "root", str(server_id)),
    ]
    assert json.loads(rows[0][3]) == {
        "name": "Demo One",
        "adapter": "demo",
        "host": "demo.lan",
        "port": 27015,
    }
    assert json.loads(rows[1][3])["after"]["name"] == "Renamed"
    assert STORED_PASSWORD not in str(rows)


def test_csrf_token_is_required(admin: TestClient, query: Query) -> None:
    assert admin.post("/admin/servers/new", data=demo_form()).status_code == 403
    assert query("SELECT count(*) FROM servers") == [(0,)]


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
def test_server_setup_is_admin_only(
    admin: TestClient,
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
    role: Role,
) -> None:
    create(admin)
    [(server_id,)] = query("SELECT id FROM servers")
    make_user("alice", role)
    client = make_client(LANPILOT_ENABLE_DEMO_ADAPTER="true")
    sign_in(client, "alice")

    for path in ("/admin/servers", "/admin/servers/new", f"/admin/servers/{server_id}/edit"):
        assert client.get(path).status_code == 403
    for path, fields in (
        ("/admin/servers/new", demo_form(name="Intruder")),
        (f"/admin/servers/{server_id}/edit", demo_form(name="Hijacked")),
        (f"/admin/servers/{server_id}/delete", {}),
        (f"/admin/servers/{server_id}/test", {}),
    ):
        assert submit(client, path, **fields)[0] == 403
    assert query("SELECT name FROM servers") == [("Demo One",)]
    home = client.get("/").text
    assert "Demo One" in home  # everyone signed in may see the servers ...
    assert "Server setup" not in home  # ... but not manage them
