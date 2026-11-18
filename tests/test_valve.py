"""The GoldSrc and Source games beyond Counter-Strike, against the simulators.

The engine code is tested with Counter-Strike (test_goldsrc.py, test_source.py); this file
checks what each further game sets: its id, its round restart, its images and its LAN scan.
"""

from ipaddress import IPv4Network

import pytest
from pydantic import SecretStr

from lanpilot.core.adapters import (
    Capability,
    DiscoveredServer,
    RestartMode,
    UnsupportedError,
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.games.valve import common
from lanpilot.games.valve.goldsrc import GoldSrcAdapter, GoldSrcConnection
from lanpilot.games.valve.halflife import (
    ConditionZeroAdapter,
    DayOfDefeatAdapter,
    HalfLifeDeathmatchAdapter,
    OpposingForceAdapter,
    TeamFortressClassicAdapter,
)
from lanpilot.games.valve.source import SourceAdapter, SourceConnection
from lanpilot.games.valve.sourcegames import (
    DayOfDefeatSourceAdapter,
    HalfLife2DeathmatchAdapter,
    TeamFortress2Adapter,
)
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.goldsrc import GoldSrcSimulator
from lanpilot.testing.source import SourceSimulator

PASSWORD = "rcon-s3cret"
LOCAL = [IPv4Network("127.0.0.1/32")]

# Adapter, game folder, game name, app id and round restart, as the real servers reported
# them (docs/games/valve.md).
GOLDSRC_GAMES = [
    (HalfLifeDeathmatchAdapter, "valve", "Half-Life", 70, None),
    (TeamFortressClassicAdapter, "tfc", "Team Fortress Classic", 20, None),
    (DayOfDefeatAdapter, "dod", "Day of Defeat", 30, "mp_clan_restartround 1"),
    (OpposingForceAdapter, "gearbox", "Opposing Force", 50, None),
    (ConditionZeroAdapter, "czero", "Condition Zero", 80, "sv_restart 1"),
]


def goldsrc_simulator(folder: str, game: str, app_id: int) -> GoldSrcSimulator:
    return GoldSrcSimulator(
        password=PASSWORD, folder=folder, game=game, app_id=app_id, hostname=f"{game} server"
    )


def goldsrc_adapter(cls: type[GoldSrcAdapter], server: GoldSrcSimulator) -> GoldSrcAdapter:
    return cls(
        GoldSrcConnection(host="127.0.0.1", port=server.port, rcon_password=SecretStr(PASSWORD))
    )


@pytest.mark.parametrize(("cls", "folder", "game", "app_id", "soft"), GOLDSRC_GAMES)
async def test_goldsrc_game_fulfils_the_contract(
    cls: type[GoldSrcAdapter], folder: str, game: str, app_id: int, soft: str | None
) -> None:
    async with goldsrc_simulator(folder, game, app_id) as server:
        await check_adapter(goldsrc_adapter(cls, server))


@pytest.mark.parametrize(("cls", "folder", "game", "app_id", "soft"), GOLDSRC_GAMES)
def test_goldsrc_game_is_registered(
    cls: type[GoldSrcAdapter], folder: str, game: str, app_id: int, soft: str | None
) -> None:
    assert load_registry().get(cls.id) is cls
    assert (Capability.RESTART_SOFT in cls.capabilities) == (soft is not None)
    assert cls.soft_restart_command == soft


@pytest.mark.parametrize(("cls", "folder", "game", "app_id", "soft"), GOLDSRC_GAMES)
async def test_goldsrc_restarts(
    cls: type[GoldSrcAdapter], folder: str, game: str, app_id: int, soft: str | None
) -> None:
    async with goldsrc_simulator(folder, game, app_id) as server:
        adapter = goldsrc_adapter(cls, server)
        await adapter.restart(RestartMode.HARD)
        if soft is None:
            with pytest.raises(UnsupportedError):
                await adapter.restart(RestartMode.SOFT)
        else:
            await adapter.restart(RestartMode.SOFT)
    assert server.restarts == ["hard"] if soft is None else ["hard", "soft"]
    assert server.commands[-1] == ("restart" if soft is None else soft)


async def test_goldsrc_games_are_told_apart_by_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = [
        goldsrc_simulator(folder, game, app_id) for _, folder, game, app_id, _ in GOLDSRC_GAMES
    ]
    for server in servers:
        await server.start()
    try:
        monkeypatch.setattr(common, "QUERY_PORTS", tuple(server.port for server in servers))
        found = {cls.id: await cls.discover(LOCAL, 0.2) for cls, *_ in GOLDSRC_GAMES}
    finally:
        for server in servers:
            server.stop()
    assert found == {
        cls.id: [DiscoveredServer(cls.id, "127.0.0.1", server.port, name=f"{game} server")]
        for (cls, _, game, _, _), server in zip(GOLDSRC_GAMES, servers, strict=True)
    }


def test_image_sources_name_each_game() -> None:
    lookups = {cls.id: dict(cls.image_lookup) for cls, *_ in GOLDSRC_GAMES}
    assert lookups == {
        "hldm": {"gametracker": "hl", "gamebanana": "34"},
        "tfc": {"gametracker": "tfc", "gamebanana": "284"},
        "dod": {"gametracker": "dod", "gamebanana": "11"},
        "opfor": {"gamebanana": "262"},
        "czero": {"gametracker": "czero", "gamebanana": "7"},
    }


# --- Source --------------------------------------------------------------------------------

# Adapter and what its server answers to the status query (docs/games/valve.md).
SOURCE_GAMES = [
    (DayOfDefeatSourceAdapter, ("dod", "Day of Defeat", 300, "11003710")),
    (HalfLife2DeathmatchAdapter, ("hl2mp", "Half-Life 2 Deathmatch", 320, "10889068")),
    (TeamFortress2Adapter, ("tf", "Team Fortress", 440, "11138802")),
]


def source_simulator(identity: tuple[str, str, int, str]) -> SourceSimulator:
    return SourceSimulator(
        password=PASSWORD, game="css", identity=identity, hostname=f"{identity[1]} server"
    )


def source_adapter(cls: type[SourceAdapter], server: SourceSimulator) -> SourceAdapter:
    connection = SourceConnection(
        host="127.0.0.1", port=server.port, rcon_password=SecretStr(PASSWORD)
    )
    return cls(connection)


@pytest.mark.parametrize(("cls", "identity"), SOURCE_GAMES)
async def test_source_game_fulfils_the_contract(
    cls: type[SourceAdapter], identity: tuple[str, str, int, str]
) -> None:
    async with source_simulator(identity) as server:
        adapter = source_adapter(cls, server)
        try:
            await check_adapter(adapter)
        finally:
            await adapter.close()


@pytest.mark.parametrize(("cls", "identity"), SOURCE_GAMES)
async def test_source_game_restarts_the_match_and_reloads_the_level(
    cls: type[SourceAdapter], identity: tuple[str, str, int, str]
) -> None:
    assert load_registry().get(cls.id) is cls
    async with source_simulator(identity) as server:
        adapter = source_adapter(cls, server)
        try:
            await adapter.restart(RestartMode.SOFT)
            await adapter.restart(RestartMode.HARD)
        finally:
            await adapter.close()
    assert server.commands == ["mp_restartgame 1", f"changelevel {server.maps[0]}"]


async def test_source_games_are_told_apart_by_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = [source_simulator(identity) for _, identity in SOURCE_GAMES]
    for server in servers:
        await server.start()
    try:
        monkeypatch.setattr(common, "QUERY_PORTS", tuple(server.port for server in servers))
        found = {cls.id: await cls.discover(LOCAL, 0.2) for cls, _ in SOURCE_GAMES}
    finally:
        for server in servers:
            server.stop()
    assert found == {
        cls.id: [DiscoveredServer(cls.id, "127.0.0.1", server.port, name=f"{identity[1]} server")]
        for (cls, identity), server in zip(SOURCE_GAMES, servers, strict=True)
    }


def test_source_image_sources_name_each_game() -> None:
    lookups = {cls.id: dict(cls.image_lookup) for cls, _ in SOURCE_GAMES}
    assert lookups == {
        "dods": {"gametracker": "dods", "gamebanana": "10"},
        "hl2dm": {"gametracker": "hl2dm", "gamebanana": "5"},
        "tf2": {"gametracker": "tf2", "gamebanana": "297"},
    }
