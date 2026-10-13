"""Polling, the action layer, the server page, events and the audit view."""

import asyncio
import json
from collections.abc import Sequence
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from lanpilot.auth.roles import Role
from lanpilot.config import Settings
from lanpilot.core import maps, secrets, servers
from lanpilot.core.adapters import (
    Capability,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    ServerStatus,
)
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.events import RESYNC, SERVER_CHANGED, SERVERS_CHANGED, Event, EventBus
from lanpilot.core.runtime import INTERNAL_ERROR, ServerManager
from lanpilot.db.engine import create_engine, create_sessionmaker
from lanpilot.web.routes_events import event_stream
from tests.conftest import (
    SECRET_KEY,
    ClientFactory,
    Prepare,
    Query,
    UserFactory,
    csrf_token,
    sign_in,
)


@pytest.fixture
def site(make_client: ClientFactory, make_user: UserFactory) -> TestClient:
    """One running LANPilot with an admin, an operator and a viewer; signed in as admin."""
    make_user("root", Role.ADMIN)
    make_user("olga", Role.OPERATOR)
    make_user("vera", Role.VIEWER)
    client = make_client(LANPILOT_ENABLE_DEMO_ADAPTER="true")
    sign_in(client, "root")
    return client


def switch_user(client: TestClient, username: str) -> None:
    client.cookies.clear()
    sign_in(client, username)


def post(client: TestClient, path: str, **data: str) -> tuple[int, str]:
    response = client.post(path, data={**data, "csrf_token": csrf_token(client)})
    return response.status_code, response.text


def add_server(client: TestClient, query: Query, name: str = "Arena", **overrides: str) -> int:
    fields = {
        "adapter": "demo",
        "name": name,
        "enabled": "1",
        "c_host": "demo.lan",
        "c_port": "27015",
        "c_password": "demo",
        "c_behaviour": "normal",
        "c_players": "3",
        **overrides,
    }
    status, page = post(client, "/admin/servers/new", **{k: v for k, v in fields.items() if v})
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers WHERE name = ?", name)
    return int(server_id)


def map_id(query: Query, server_id: int, key: str) -> str:
    [(row_id,)] = query(
        "SELECT id FROM server_maps WHERE server_id = ? AND key = ?", server_id, key
    )
    return str(row_id)


def audit_rows(query: Query, action: str) -> list[tuple[str, int, dict[str, object]]]:
    rows = query(
        "SELECT actor_name, success, detail FROM audit_log WHERE action = ? ORDER BY id", action
    )
    return [(actor, success, json.loads(detail)) for actor, success, detail in rows]


# --- status ----------------------------------------------------------------------------


def test_new_server_is_polled_and_its_maps_are_stored(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)

    home = site.get("/").text
    assert f'href="/servers/{server_id}"' in home
    assert "online" in home
    assert "Foundry" in home
    assert "3/6" in home
    assert query("SELECT count(*), min(source) FROM server_maps") == [(6, "protocol")]

    page = site.get(f"/servers/{server_id}").text
    assert "Demo server demo.lan:27015" in page
    assert "Deathmatch" in page
    assert '<optgroup label="Capture the Flag">' in page
    assert "Twin Forts" in page


def test_offline_server_is_shown_with_its_error(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, c_behaviour="offline")

    assert "offline" in site.get("/").text
    page = site.get(f"/servers/{server_id}").text
    assert "The server did not answer." in page
    assert query("SELECT count(*) FROM server_maps") == [(0,)]


def test_wrong_password_is_reported_as_a_control_problem(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, c_password="not-the-password")

    assert "online" in site.get("/").text  # status needs no password
    page = site.get(f"/servers/{server_id}").text
    assert "remote control does not" in page
    assert "refused the remote-control password" in page
    assert "not-the-password" not in page


def test_disabled_server_is_not_polled(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, enabled="")

    assert "disabled" in site.get("/").text
    page = site.get(f"/servers/{server_id}").text
    assert "Change map" not in page
    assert query("SELECT count(*) FROM server_maps") == [(0,)]


def test_edit_and_delete_are_picked_up(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    post(
        site,
        f"/admin/servers/{server_id}/edit",
        name="Renamed Arena",
        enabled="1",
        c_host="demo.lan",
        c_port="27015",
        c_behaviour="normal",
        c_players="5",
    )
    home = site.get("/").text
    assert "Renamed Arena" in home
    assert "5/6" in home

    post(site, f"/admin/servers/{server_id}/delete")

    assert site.get(f"/servers/{server_id}").status_code == 404
    assert "Renamed Arena" not in site.get("/").text
    assert query("SELECT count(*) FROM server_maps") == [(0,)]  # removed with the server


def test_unknown_server_is_404(site: TestClient) -> None:
    assert site.get("/servers/999").status_code == 404
    assert post(site, "/servers/999/restart", mode="soft")[0] == 404
    assert post(site, "/servers/999/map", map_id="1")[0] in (400, 404)


# --- actions ---------------------------------------------------------------------------


def test_operator_changes_the_map(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    switch_user(site, "olga")

    response = site.post(
        f"/servers/{server_id}/map",
        data={"map_id": map_id(query, server_id, "ctf_canyon"), "csrf_token": csrf_token(site)},
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/servers/{server_id}?notice=map_changed"
    page = site.get(response.headers["location"]).text
    assert "The map change was sent to the server." in page
    assert "<dd>Canyon" in page
    assert audit_rows(query, "server.change_map") == [
        ("olga", 1, {"map": "ctf_canyon", "game_type": "Capture the Flag", "server": "Arena"})
    ]


def test_map_must_come_from_this_servers_list(site: TestClient, query: Query) -> None:
    first = add_server(site, query, "First")
    second = add_server(site, query, "Second", c_host="other.lan")
    foreign = map_id(query, second, "ctf_canyon")

    status, page = post(site, f"/servers/{first}/map", map_id=foreign)

    assert status == 400
    assert "Pick a map from this server" in page
    assert post(site, f"/servers/{first}/map", map_id="999999")[0] == 400
    assert post(site, f"/servers/{first}/map", map_id="dm_foundry; quit")[0] == 422
    assert audit_rows(query, "server.change_map") == []
    assert "<dd>Foundry" in site.get(f"/servers/{first}").text


@pytest.mark.parametrize(
    ("mode", "notice"), [("soft", "restarted_soft"), ("hard", "restarted_hard")]
)
def test_operator_restarts(site: TestClient, query: Query, mode: str, notice: str) -> None:
    server_id = add_server(site, query)
    switch_user(site, "olga")

    response = site.post(
        f"/servers/{server_id}/restart", data={"mode": mode, "csrf_token": csrf_token(site)}
    )

    assert response.headers["location"] == f"/servers/{server_id}?notice={notice}"
    assert audit_rows(query, "server.restart") == [("olga", 1, {"mode": mode, "server": "Arena"})]


def test_unknown_restart_mode_is_rejected(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)

    assert post(site, f"/servers/{server_id}/restart", mode="quit")[0] == 422
    assert audit_rows(query, "server.restart") == []


def test_refresh_maps(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    query("DELETE FROM server_maps WHERE key LIKE 'ctf_%'")

    status, _ = post(site, f"/servers/{server_id}/maps/refresh")

    assert status == 303
    assert query("SELECT count(*) FROM server_maps") == [(6,)]
    assert audit_rows(query, "server.refresh_maps") == [("root", 1, {"server": "Arena"})]


def test_failed_action_is_shown_and_audited(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, c_password="not-the-password")

    status, page = post(site, f"/servers/{server_id}/restart", mode="soft")

    assert status == 502
    assert "The server refused the remote-control password." in page
    assert "not-the-password" not in page
    [(actor, success, detail)] = audit_rows(query, "server.restart")
    assert (actor, success) == ("root", 0)
    assert detail == {
        "mode": "soft",
        "error": "The server refused the remote-control password.",
    }


def test_action_on_unreachable_server_fails_cleanly(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, c_behaviour="offline")

    status, page = post(site, f"/servers/{server_id}/restart", mode="hard")

    assert status == 502
    assert "The server did not answer." in page


def test_action_on_disabled_server_is_refused(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, enabled="")

    status, page = post(site, f"/servers/{server_id}/restart", mode="soft")

    assert status == 409
    assert "This server is disabled." in page


def test_viewer_sees_status_but_cannot_act(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    foundry = map_id(query, server_id, "dm_reactor")
    switch_user(site, "vera")

    page = site.get(f"/servers/{server_id}").text
    assert "Foundry" in page
    assert "Change map" not in page
    assert "Restart" not in page
    assert post(site, f"/servers/{server_id}/map", map_id=foundry)[0] == 403
    assert post(site, f"/servers/{server_id}/restart", mode="soft")[0] == 403
    assert post(site, f"/servers/{server_id}/maps/refresh")[0] == 403
    assert query("SELECT count(*) FROM audit_log WHERE action LIKE 'server.restart'") == [(0,)]


def test_actions_need_the_csrf_token(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)

    assert site.post(f"/servers/{server_id}/restart", data={"mode": "soft"}).status_code == 403
    assert audit_rows(query, "server.restart") == []


# --- audit view ------------------------------------------------------------------------


def test_audit_log_page(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, name="<b>Arena</b>")
    post(site, f"/servers/{server_id}/restart", mode="soft")

    page = site.get("/admin/audit").text

    assert "server.restart" in page
    assert "server.create" in page
    assert "mode: soft" in page
    assert "<b>Arena</b>" not in page
    assert "&lt;b&gt;Arena&lt;/b&gt;" in page

    switch_user(site, "olga")
    assert site.get("/admin/audit").status_code == 403


# --- events ----------------------------------------------------------------------------


def test_event_stream_requires_a_session(site: TestClient) -> None:
    site.cookies.clear()

    assert site.get("/events").status_code == 401


async def collect(bus: EventBus, publish: Sequence[Event], *, signed_in: bool = True) -> list[str]:
    async def still_signed_in() -> bool:
        return signed_in

    frames: list[str] = []
    stream = event_stream(bus, still_signed_in=still_signed_in, keepalive_seconds=0.05)
    async for frame in stream:
        frames.append(frame)
        if len(frames) == 1:
            for event in publish:
                bus.publish(event)
        if len(frames) >= len(publish) + 2:
            break
    await stream.aclose()
    return frames


async def test_event_stream_frames() -> None:
    bus = EventBus()

    frames = await collect(bus, [Event(SERVER_CHANGED, 3), Event(SERVERS_CHANGED)])

    assert frames == [
        "retry: 5000\n: connected\n\n",
        "event: server-3\ndata: 3\n\n",
        "event: servers\ndata: \n\n",
        ": keepalive\n\n",
    ]
    assert bus.subscriber_count == 0  # unsubscribed when the stream ends


async def test_event_stream_ends_when_the_session_is_gone() -> None:
    frames = await collect(EventBus(), [], signed_in=False)

    assert frames == ["retry: 5000\n: connected\n\n"]


async def test_slow_subscriber_gets_a_resync_instead_of_a_backlog() -> None:
    bus = EventBus(max_queue=3)
    with bus.subscribe() as queue:
        for index in range(10):
            bus.publish(Event(SERVER_CHANGED, index))

        received = [queue.get_nowait() for _ in range(queue.qsize())]

    assert received[0] == Event(RESYNC)
    assert len(received) <= 3


# --- map storage -----------------------------------------------------------------------


async def test_reported_maps_are_filtered_before_storing(
    settings: Settings, prepare_database: Prepare
) -> None:
    prepare_database(settings)
    engine = create_engine(settings.database_path)
    reported = [
        MapRef("de_dust2", "Dust II", "Bomb"),
        MapRef("de_dust2", "duplicate", "Bomb"),
        MapRef("de_dust2", None, "Hostage"),  # same key in another game type is distinct
        MapRef("", "no key"),
        MapRef("bad\x00key"),
        MapRef("line\nbreak"),
        MapRef("x" * 201),
        MapRef("ok_map", "evil\x1bname", None, {"uid": "u" * 500}),
    ]
    try:
        async with create_sessionmaker(engine)() as db:
            box = secrets.SecretBox(secrets._decode_key(SECRET_KEY, "test"))
            from lanpilot.games.demo import DemoAdapter

            server = await servers.create_server(
                db, box, DemoAdapter, servers.ServerInput("S", {"password": "demo"})
            )
            count = await maps.replace_reported(db, server.id, reported)
            await db.commit()
            stored = await maps.list_maps(db, server.id)
    finally:
        await engine.dispose()

    assert count == 3
    assert [(row.key, row.name, row.game_type) for row in stored] == [
        ("ok_map", None, ""),
        ("de_dust2", "Dust II", "Bomb"),
        ("de_dust2", None, "Hostage"),
    ]
    assert len(json.loads(stored[0].extra)["uid"]) == 200


# --- manager ---------------------------------------------------------------------------


class FlakyConnection(ConnectionSettings):
    port: int = 1


class FlakyAdapter(GameAdapter):
    """Raises an unexpected exception on every second query, like a buggy adapter would."""

    id = "flaky"
    name = "Flaky"
    family = "test"
    capabilities = frozenset({Capability.STATUS})
    connection_schema = FlakyConnection
    calls: ClassVar[int] = 0
    closed: ClassVar[int] = 0

    async def query(self) -> ServerStatus:
        type(self).calls += 1
        if type(self).calls % 2 == 0:
            raise RuntimeError("adapter bug")
        return ServerStatus(name="flaky", players=1, max_players=2)

    async def close(self) -> None:
        type(self).closed += 1


async def test_manager_survives_adapter_bugs_and_publishes_changes(
    settings: Settings, prepare_database: Prepare, caplog: pytest.LogCaptureFixture
) -> None:
    prepare_database(settings)
    FlakyAdapter.calls = FlakyAdapter.closed = 0
    registry = AdapterRegistry()
    registry.register(FlakyAdapter)
    box = secrets.SecretBox(secrets._decode_key(SECRET_KEY, "test"))
    engine = create_engine(settings.database_path)
    sessionmaker = create_sessionmaker(engine)
    bus = EventBus()
    manager = ServerManager(
        sessionmaker=sessionmaker, registry=registry, box=box, bus=bus, interval=3600, timeout=1
    )
    try:
        async with sessionmaker() as db:
            server = await servers.create_server(
                db, box, FlakyAdapter, servers.ServerInput("Flaky One", {"host": "flaky.lan"})
            )
            await db.commit()
        with bus.subscribe() as queue:
            await manager.reload(server.id)
            state = manager.state(server.id)
            assert state is not None and state.online is True

            await manager.poll_now(server.id)  # second query raises RuntimeError
            state = manager.state(server.id)
            assert state is not None
            assert (state.online, state.error) == (False, INTERNAL_ERROR)
            assert "adapter bug" in caplog.text

            await manager.poll_now(server.id)  # the loop is still alive and recovers
            state = manager.state(server.id)
            assert state is not None and state.online is True

            kinds = [queue.get_nowait().kind for _ in range(queue.qsize())]
        assert kinds.count(SERVER_CHANGED) == 3
        assert SERVERS_CHANGED in kinds
        assert [item.name for item in manager.states()] == ["Flaky One"]
    finally:
        await manager.stop()
        await engine.dispose()

    assert FlakyAdapter.closed == 1
    assert manager.states() == []
    await asyncio.sleep(0)  # let the cancelled poll task finish


# --- dashboard and live fragments ------------------------------------------------------


def test_dashboard_shows_a_card_per_server_wired_for_live_updates(
    site: TestClient, query: Query
) -> None:
    server_id = add_server(site, query)

    home = site.get("/").text

    assert 'hx-ext="sse" sse-connect="/events"' in home
    assert 'hx-get="/cards"' in home
    assert f'id="server-{server_id}"' in home
    assert f'hx-get="/servers/{server_id}/card"' in home
    assert f'hx-trigger="sse:server-{server_id}"' in home
    assert '<progress value="3" max="6"' in home
    assert "Foundry" in home
    assert "/static/vendor/htmx/htmx.min.js" in home


def test_card_fragment_follows_the_state(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    post(site, f"/servers/{server_id}/map", map_id=map_id(query, server_id, "race_a01"))

    card = site.get(f"/servers/{server_id}/card").text

    assert card.lstrip().startswith("<article")
    assert "<html" not in card  # a fragment, not a page
    assert "A01 Race" in card
    assert "online" in card


def test_grid_and_live_fragments(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query, name="<i>Arena</i>")

    grid = site.get("/cards").text
    live = site.get(f"/servers/{server_id}/live").text

    assert grid.lstrip().startswith('<div id="server-grid"')
    assert "&lt;i&gt;Arena&lt;/i&gt;" in grid
    assert "<i>Arena</i>" not in grid + live
    assert live.lstrip().startswith('<div id="server-live"')
    assert f'hx-trigger="sse:server-{server_id}, sse:resync"' in live
    assert "<dt>Players</dt><dd>3/6</dd>" in live


def test_empty_dashboard_invites_the_admin_to_add_a_server(site: TestClient) -> None:
    assert 'href="/admin/servers/new"' in site.get("/cards").text

    switch_user(site, "vera")
    grid = site.get("/cards").text
    assert "An administrator can add them" in grid
    assert "/admin/servers/new" not in grid


def test_fragments_need_a_session_and_an_existing_server(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)

    assert site.get("/servers/999/card").status_code == 404
    assert site.get("/servers/999/live").status_code == 404

    site.cookies.clear()
    for path in ("/cards", f"/servers/{server_id}/card", f"/servers/{server_id}/live"):
        # What HTMX gets when the session has ended; app.js then reloads the page.
        assert site.get(path, headers={"HX-Request": "true"}).status_code == 401


def test_viewer_gets_live_fragments(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    switch_user(site, "vera")

    assert site.get(f"/servers/{server_id}/card").status_code == 200
    assert site.get(f"/servers/{server_id}/live").status_code == 200


def test_map_picker_is_marked_searchable(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)

    page = site.get(f"/servers/{server_id}").text

    assert '<select id="map_id" name="map_id" data-searchable' in page
    assert "/static/vendor/tom-select/tom-select.base.min.js" in page
    assert "/static/js/app.js" in page


def test_scripts_are_only_loaded_for_signed_in_users(site: TestClient) -> None:
    site.cookies.clear()

    assert "<script" not in site.get("/login").text


@pytest.mark.parametrize(
    "path",
    [
        "/static/vendor/htmx/htmx.min.js",
        "/static/vendor/htmx-ext-sse/sse.min.js",
        "/static/vendor/tom-select/tom-select.base.min.js",
        "/static/vendor/tom-select/tom-select.min.css",
        "/static/vendor/tom-select/LICENSE",
        "/static/js/app.js",
    ],
)
def test_frontend_files_are_shipped(site: TestClient, path: str) -> None:
    response = site.get(path)

    assert response.status_code == 200
    assert len(response.content) > 500


def test_pages_use_no_inline_scripts_styles_or_handlers(site: TestClient, query: Query) -> None:
    """The Content-Security-Policy would block them; the pages must not rely on any."""
    import re

    server_id = add_server(site, query)

    for path in ("/", f"/servers/{server_id}", "/admin/servers", "/admin/users", "/account"):
        page = site.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", page), path
        assert "<style" not in page, path
        assert not re.search(r'\sstyle="', page), path
        assert not re.search(r"\son[a-z]+=", page), path
