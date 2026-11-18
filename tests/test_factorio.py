"""Factorio: the RCON transport in single-packet mode and the adapter (docs/games/factorio.md)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from lanpilot.core.actions import ACTION_TIMEOUT_SECONDS
from lanpilot.core.adapters import (
    CommandRejectedError,
    MapRef,
    Player,
    RestartMode,
    ServerUnreachableError,
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.adapters.schema import describe_fields
from lanpilot.core.transports.source_rcon import SourceRcon
from lanpilot.games.factorio import adapter as factorio_adapter
from lanpilot.games.factorio.adapter import (
    FactorioAdapter,
    FactorioConnection,
    parse_players,
    parse_time,
)
from lanpilot.i18n import Message
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.factorio import FactorioRconSimulator
from lanpilot.testing.lan import FactorioLanSimulator

PASSWORD = "factorio-s3cret"


@pytest.fixture
async def server() -> AsyncIterator[FactorioRconSimulator]:
    async with FactorioRconSimulator(password=PASSWORD) as simulator:
        yield simulator


def single_packet_rcon(server: FactorioRconSimulator) -> SourceRcon:
    return SourceRcon(
        "127.0.0.1", server.port, PASSWORD, timeout_seconds=0.5, single_packet_answers=True
    )


# --- transport -----------------------------------------------------------------------------


async def test_single_packet_mode_returns_answer_quickly(server: FactorioRconSimulator) -> None:
    rcon = single_packet_rcon(server)
    try:
        async with asyncio.timeout(1):
            assert await rcon.execute("/version") == "2.1.24\n"
    finally:
        await rcon.close()
    assert server.empty_commands == 0


async def test_single_packet_mode_keeps_connection(server: FactorioRconSimulator) -> None:
    rcon = single_packet_rcon(server)
    try:
        await rcon.execute("/version")
        await rcon.execute("/players online")
    finally:
        await rcon.close()
    assert server.connections == 1
    assert server.commands == ["/version", "/players online"]


async def test_single_packet_mode_empty_answer(server: FactorioRconSimulator) -> None:
    rcon = single_packet_rcon(server)
    try:
        assert await rcon.execute("/server-save") == ""
    finally:
        await rcon.close()


async def test_default_mode_against_factorio_times_out(server: FactorioRconSimulator) -> None:
    # Why the mode exists: Factorio never answers the empty end marker.
    rcon = SourceRcon("127.0.0.1", server.port, PASSWORD, timeout_seconds=0.5)
    try:
        with pytest.raises(ServerUnreachableError):
            await rcon.execute("/version")
    finally:
        await rcon.close()


# --- adapter -------------------------------------------------------------------------------

FORBIDDEN = [
    "/c game.print(1)",
    "/command game.print(1)",
    "/sc game.print(1)",
    "/silent-command game.print(1)",
    "/mc game.print(1)",
    "/measured-command game.print(1)",
    "/editor",
    "/cheat all",
    "/toggle-heavy-mode",
    "/delete-blueprint-library everybody confirm",
    "  /C game.print(1)",
    "/SC game.print(1)",
    "/c\tgame.print(1)",
    "hello everyone",
    "",
    "/quit",
    "  /QUIT",
]


def adapter(server: FactorioRconSimulator, *, restarts_itself: bool = False) -> FactorioAdapter:
    return FactorioAdapter(
        FactorioConnection(
            host="127.0.0.1",
            port=server.port,
            password=SecretStr(PASSWORD),
            restarts_itself=restarts_itself,
        )
    )


async def test_factorio_fulfils_the_contract(server: FactorioRconSimulator) -> None:
    await check_adapter(adapter(server, restarts_itself=True))


async def test_status(server: FactorioRconSimulator) -> None:
    factorio = adapter(server)
    try:
        status = await factorio.query()
    finally:
        await factorio.close()
    assert status.name == "Simulated Factorio"
    assert status.players == 1
    assert status.max_players == 0
    assert status.map == MapRef(
        "space-age", name="World 3490025918", extra={"steam_store_app": "645390"}
    )
    assert status.game_type == "Space Age"
    assert status.load == {}
    assert status.details == {
        "Version": "2.1.24",
        "Planets": "Nauvis\nVulcanus\nFulgora\nGleba\nAquilo",
        "Map age": "2413 h 53 min",
        "Players ever joined": "3",
        "Autosave": "every 10 min",
    }


async def test_status_without_space_age() -> None:
    async with FactorioRconSimulator(password=PASSWORD, space_age=False) as server:
        factorio = adapter(server)
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.game_type is None
    assert status.map == MapRef(
        "factorio", name="World 3490025918", extra={"steam_store_app": "427520"}
    )


async def test_players_strip_suffixes_and_drop_odd_names() -> None:
    online = ("Engineer", "Bob (admin)", "bad name", "x" * 61)
    async with FactorioRconSimulator(password=PASSWORD, online=online) as server:
        factorio = adapter(server)
        try:
            players = await factorio.list_players()
        finally:
            await factorio.close()
    assert [player.id for player in players] == ["Engineer", "Bob"]
    assert [player.name for player in players] == ["Engineer", "Bob"]


async def test_kick_and_ban_send_cleaned_reasons(server: FactorioRconSimulator) -> None:
    server.online.append("Bob")
    factorio = adapter(server)
    try:
        await factorio.kick(Player(id="Engineer", name="Engineer"), "spam; /c game.x\nnext")
        await factorio.ban(Player(id="Bob", name="Bob"), "")
    finally:
        await factorio.close()
    kick, ban = server.commands
    assert kick.startswith("/kick Engineer ")
    reason = kick.removeprefix("/kick Engineer ")
    assert ";" not in reason
    assert "/" not in reason
    assert "\n" not in reason
    assert reason == "spam c game.xnext"
    assert ban == "/ban Bob by LANPilot"
    assert server.banned == ["Bob"]


async def test_kick_unknown_player_is_rejected(server: FactorioRconSimulator) -> None:
    factorio = adapter(server)
    try:
        with pytest.raises(CommandRejectedError):
            await factorio.kick(Player(id="Nobody", name="Nobody"))
    finally:
        await factorio.close()


async def test_kick_refuses_a_name_that_could_change_the_command(
    server: FactorioRconSimulator,
) -> None:
    factorio = adapter(server)
    try:
        with pytest.raises(CommandRejectedError):
            await factorio.kick(Player(id="x /c game", name="x"))
    finally:
        await factorio.close()
    assert server.commands == []


@pytest.mark.parametrize("command", FORBIDDEN)
async def test_console_refuses_achievement_killers(
    server: FactorioRconSimulator, command: str
) -> None:
    factorio = adapter(server)
    try:
        with pytest.raises(CommandRejectedError):
            await factorio.console(command)
    finally:
        await factorio.close()
    assert server.commands == []


async def test_console_passes_safe_commands(server: FactorioRconSimulator) -> None:
    factorio = adapter(server)
    try:
        answer = await factorio.console("/players")
    finally:
        await factorio.close()
    assert "Engineer" in answer


async def test_restart_requires_opt_in(server: FactorioRconSimulator) -> None:
    factorio = adapter(server)
    try:
        with pytest.raises(CommandRejectedError):
            await factorio.restart(RestartMode.HARD)
    finally:
        await factorio.close()
    assert server.quit_received is False
    assert "/server-save" not in server.commands


async def test_restart_saves_then_quits(server: FactorioRconSimulator) -> None:
    factorio = adapter(server, restarts_itself=True)
    try:
        await factorio.restart(RestartMode.HARD)
    finally:
        await factorio.close()
    assert server.commands[-2:] == ["/server-save", "/quit"]
    assert server.quit_received is True


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        ("2413 hours and 1 minute", 2413.0167),
        ("5 minutes", 0.0833),
        ("1 day, 2 hours and 3 minutes", 26.05),
        ("30 seconds", 0.0083),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_time(text: str, hours: float | None) -> None:
    result = parse_time(text)
    if hours is None:
        assert result is None
    else:
        assert result == pytest.approx(hours, abs=0.001)


def test_factorio_is_registered() -> None:
    assert "factorio" in load_registry()
    fields = {field.name: field for field in describe_fields(FactorioConnection)}
    assert {"port", "password", "game_port", "restarts_itself"} <= set(fields)
    assert fields["restarts_itself"].kind == "checkbox"


# --- review fixes --------------------------------------------------------------------------

ONE_PLAYER_REPLY = (
    Path(__file__).parent / "fixtures" / "factorio" / "info_reply_1_player.bin"
).read_bytes()


async def test_restart_succeeds_when_quit_drops_the_connection() -> None:
    async with FactorioRconSimulator(password=PASSWORD, quit_closes=True) as server:
        factorio = adapter(server, restarts_itself=True)
        try:
            await factorio.restart(RestartMode.HARD)
        finally:
            await factorio.close()
    assert server.quit_received is True
    assert server.commands.count("/quit") == 1


async def test_restart_waits_for_a_slow_save(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factorio_adapter, "RCON_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(factorio_adapter, "SAVE_TIMEOUT_SECONDS", 3.0)
    async with FactorioRconSimulator(password=PASSWORD, save_delay=0.6) as server:
        factorio = adapter(server, restarts_itself=True)
        try:
            await factorio.restart(RestartMode.HARD)
        finally:
            await factorio.close()
    assert server.commands[-2:] == ["/server-save", "/quit"]
    assert server.quit_received is True


async def test_space_age_from_lan_info_on_a_fresh_save() -> None:
    # A new Space Age map has only reached Nauvis, so /evolution lists no other planet.
    async with (
        FactorioRconSimulator(password=PASSWORD, space_age=False) as server,
        FactorioLanSimulator(reply=ONE_PLAYER_REPLY) as game,
    ):
        factorio = FactorioAdapter(
            FactorioConnection(
                host="127.0.0.1",
                port=server.port,
                password=SecretStr(PASSWORD),
                game_port=game.port,
            )
        )
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.game_type == "Space Age"
    assert game.queries == [b"\x10"]


async def test_space_age_falls_back_to_evolution_without_lan_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factorio_adapter, "LAN_INFO_WAIT_SECONDS", 0.2)
    async with (
        FactorioRconSimulator(password=PASSWORD, space_age=True) as server,
        FactorioLanSimulator(reply=None) as game,
    ):
        factorio = FactorioAdapter(
            FactorioConnection(
                host="127.0.0.1",
                port=server.port,
                password=SecretStr(PASSWORD),
                game_port=game.port,
            )
        )
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.game_type == "Space Age"


async def test_the_edition_is_the_only_map(server: FactorioRconSimulator) -> None:
    factorio = adapter(server)
    try:
        maps = await factorio.list_maps()
    finally:
        await factorio.close()
    assert maps == [
        MapRef("space-age", name="World 3490025918", extra={"steam_store_app": "645390"})
    ]


async def test_odd_seed_answer_gives_no_map() -> None:
    async with FactorioRconSimulator(password=PASSWORD, seed="no seed here") as server:
        factorio = adapter(server)
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.map is None


async def test_mods_come_from_the_lan_info() -> None:
    async with (
        FactorioRconSimulator(password=PASSWORD, space_age=False) as server,
        FactorioLanSimulator(reply=ONE_PLAYER_REPLY) as game,
    ):
        factorio = FactorioAdapter(
            FactorioConnection(
                host="127.0.0.1",
                port=server.port,
                password=SecretStr(PASSWORD),
                game_port=game.port,
            )
        )
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.details["Mods"] == (
        "elevated-rails 2.1.24\nexample-mod 1.2.3\nrecycler 2.1.24\n"
        "quality 2.1.24\nspace-age 2.1.24"
    )
    assert status.details["Planets"] == "Nauvis"
    assert list(status.details) == [
        "Version",
        "Planets",
        "Mods",
        "Map age",
        "Players ever joined",
        "Autosave",
    ]


async def test_mod_surfaces_are_not_planets() -> None:
    surfaces = ("agent-limbo", "purple-lab")
    async with FactorioRconSimulator(password=PASSWORD, extra_surfaces=surfaces) as server:
        factorio = adapter(server)
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.details["Planets"] == "Nauvis\nVulcanus\nFulgora\nGleba\nAquilo"


async def test_autosave_off_is_left_out() -> None:
    async with FactorioRconSimulator(password=PASSWORD, autosave_minutes=0) as server:
        factorio = adapter(server)
        try:
            status = await factorio.query()
        finally:
            await factorio.close()
    assert "Autosave" not in status.details


# --- second review ---------------------------------------------------------------------


async def test_restart_does_not_quit_when_saving_takes_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factorio_adapter, "SAVE_TIMEOUT_SECONDS", 0.3)
    async with FactorioRconSimulator(password=PASSWORD, save_delay=1.0) as server:
        factorio = adapter(server, restarts_itself=True)
        try:
            with pytest.raises(CommandRejectedError):
                await factorio.restart(RestartMode.HARD)
        finally:
            await factorio.close()
    assert server.quit_received is False


def test_save_timeout_fits_into_an_action() -> None:
    assert factorio_adapter.SAVE_TIMEOUT_SECONDS + 3 <= ACTION_TIMEOUT_SECONDS


async def test_restart_does_not_quit_when_saving_fails() -> None:
    answer = "Error: cannot save the map: disk full\n"
    async with FactorioRconSimulator(password=PASSWORD, save_answer=answer) as server:
        factorio = adapter(server, restarts_itself=True)
        try:
            with pytest.raises(CommandRejectedError):
                await factorio.restart(RestartMode.HARD)
        finally:
            await factorio.close()
    assert server.quit_received is False


async def test_failed_stats_are_asked_again_at_the_next_poll() -> None:
    async with FactorioRconSimulator(password=PASSWORD, drops={"/evolution": 2}) as server:
        factorio = adapter(server)
        try:
            with pytest.raises(ServerUnreachableError):
                await factorio.query()
            status = await factorio.query()
        finally:
            await factorio.close()
    assert status.map is not None
    assert status.details["Planets"]


def test_long_lists_are_cut_at_whole_items() -> None:
    items = [f"mod-{number:02d} 1.0.0" for number in range(45)]
    text = factorio_adapter.format_list(items)
    lines = text.split("\n")
    assert lines[: factorio_adapter.MAX_LIST_ITEMS] == items[: factorio_adapter.MAX_LIST_ITEMS]
    assert lines[-1] == f"… and {45 - factorio_adapter.MAX_LIST_ITEMS} more"
    assert len(lines) == factorio_adapter.MAX_LIST_ITEMS + 1
    assert factorio_adapter.format_list(items[:3]) == "\n".join(items[:3])


async def test_detail_labels_and_texts_are_translatable(server: FactorioRconSimulator) -> None:
    factorio = adapter(server)
    try:
        status = await factorio.query()
    finally:
        await factorio.close()
    assert all(isinstance(name, Message) for name in status.details)
    assert isinstance(status.details["Map age"], Message)
    assert isinstance(status.details["Autosave"], Message)


@pytest.mark.parametrize("separator", ["\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"])
def test_players_split_at_line_feeds_only(separator: str) -> None:
    # A name holding a character str.splitlines() splits at must not forge a second player.
    answer = f"Online players (1):\n  Eve{separator}Bob (online)\n"
    assert parse_players(answer) == []
