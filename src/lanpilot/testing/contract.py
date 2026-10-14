"""Contract every game adapter has to fulfil.

Call :func:`check_adapter` from a test with an adapter instance that is connected to a
protocol simulator (or a real server). It exercises exactly the capabilities the adapter
declares and verifies that the rest is refused properly.

    async def test_contract(simulator):
        adapter = MyAdapter(MyConnection(host=simulator.host, port=simulator.port, ...))
        await check_adapter(adapter)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from lanpilot.core.adapters import (
    Capability,
    GameAdapter,
    MapRef,
    Player,
    RestartMode,
    ServerStatus,
    UnsupportedError,
    validate_adapter_class,
)


async def _refuses(adapter: GameAdapter, capability: Capability) -> None:
    nobody = Player(id="1", name="nobody")
    calls: dict[Capability, Callable[[], Awaitable[object]]] = {
        Capability.MAP_LIST: adapter.list_maps,
        Capability.MAP_CHANGE: lambda: adapter.change_map(MapRef("no_such_map")),
        Capability.PLAYERS: adapter.list_players,
        Capability.KICK: lambda: adapter.kick(nobody),
        Capability.BAN: lambda: adapter.ban(nobody),
        Capability.CONSOLE: lambda: adapter.console("status"),
        Capability.MANUAL_MAPS: lambda: adapter.look_up_map("no_such_map"),
    }
    if capability not in calls:
        return
    try:
        await calls[capability]()
    except UnsupportedError:
        return
    raise AssertionError(
        f"{capability} is not declared, but the call did not raise UnsupportedError"
    )


async def check_adapter(adapter: GameAdapter) -> None:
    """Run the contract checks; raises ``AssertionError`` describing the first violation.

    The checks change the server's state (map, restart), so point the adapter at a
    simulator or a server that is yours to disturb.
    """
    validate_adapter_class(type(adapter))

    status = await adapter.query()
    assert isinstance(status, ServerStatus), "query() must return a ServerStatus"
    assert status.players >= 0, "players must not be negative"
    assert status.max_players >= 0, "max_players must not be negative"
    if adapter.supports(Capability.LOAD_STATS):
        assert status.load, "LOAD_STATS is declared, but query() returned no load values"

    if adapter.supports(Capability.MAP_LIST):
        maps = list(await adapter.list_maps())
        assert maps, "list_maps() returned no maps"
        assert all(isinstance(item, MapRef) and item.key for item in maps), "maps need a key"
        keys = [(item.key, item.game_type) for item in maps]
        assert len(keys) == len(set(keys)), "list_maps() returned duplicate maps"

        if adapter.supports(Capability.MAP_CHANGE):
            current = status.map.key if status.map else None
            target = next((item for item in maps if item.key != current), maps[0])
            await adapter.change_map(target)
            after = await adapter.query()
            assert after.map is not None and after.map.key == target.key, (
                "after change_map() the status still reports another map"
            )

    for mode, capability in (
        (RestartMode.SOFT, Capability.RESTART_SOFT),
        (RestartMode.HARD, Capability.RESTART_HARD),
    ):
        if adapter.supports(capability):
            await adapter.restart(mode)
        else:
            try:
                await adapter.restart(mode)
            except UnsupportedError:
                pass
            else:
                raise AssertionError(f"restart({mode}) must raise UnsupportedError")

    if adapter.supports(Capability.PLAYERS):
        players = list(await adapter.list_players())
        assert all(isinstance(item, Player) and item.id for item in players), "players need an id"
        ids = [item.id for item in players]
        assert len(ids) == len(set(ids)), "player ids must be unique"

    for capability in Capability:
        if not adapter.supports(capability):
            await _refuses(adapter, capability)

    await adapter.close()
    await adapter.close()  # closing twice must be harmless
