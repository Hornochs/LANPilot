from importlib.metadata import EntryPoint
from ipaddress import IPv4Network
from typing import Literal

import pytest
from pydantic import Field, SecretStr, ValidationError

from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    Player,
    RestartMode,
    ServerStatus,
    ServerUnreachableError,
    UnsupportedError,
    validate_adapter_class,
)
from lanpilot.core.adapters.registry import AdapterRegistry, load_registry
from lanpilot.core.adapters.schema import (
    describe_errors,
    describe_fields,
    secret_field_names,
    split_values,
)
from lanpilot.games.demo import DemoAdapter, DemoConnection
from lanpilot.testing.contract import check_adapter


class StatusOnlyConnection(ConnectionSettings):
    port: int = Field(default=1234, ge=1, le=65535)


class StatusOnlyAdapter(GameAdapter):
    id = "status_only"
    name = "Status only"
    family = "test"
    capabilities = frozenset({Capability.STATUS})
    connection_schema = StatusOnlyConnection

    async def query(self) -> ServerStatus:
        return ServerStatus(name="x", players=0, max_players=8)


def demo(**overrides: object) -> DemoAdapter:
    values: dict[str, object] = {"password": "demo", **overrides}
    return DemoAdapter(DemoConnection.model_validate(values))


# --- connection settings -------------------------------------------------------------------


@pytest.mark.parametrize("host", ["10.0.0.5", "game.lan", "fd00::1", " cs.example.org "])
def test_valid_hosts(host: str) -> None:
    assert StatusOnlyConnection(host=host).host == host.strip()


@pytest.mark.parametrize(
    "host",
    ["", "http://game.lan", "game.lan/path", "user@game.lan", "a b", "game.lan;rm -rf", "x\ny"],
)
def test_invalid_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        StatusOnlyConnection(host=host)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_ports_are_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        StatusOnlyConnection(host="game.lan", port=port)


def test_unknown_connection_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        StatusOnlyConnection.model_validate({"host": "game.lan", "extra": 1})


# --- schema -> form ------------------------------------------------------------------------


def test_demo_schema_becomes_form_fields() -> None:
    fields = {field.name: field for field in describe_fields(DemoConnection)}

    assert list(fields) == ["host", "port", "password", "behaviour", "players"]
    assert fields["host"].kind == "text"
    assert fields["host"].max_length == 253
    assert (fields["port"].kind, fields["port"].minimum, fields["port"].maximum) == (
        "number",
        1,
        65535,
    )
    assert fields["port"].default == 27015
    assert fields["password"].kind == "password"
    assert fields["password"].secret
    assert fields["password"].required
    assert fields["password"].label == "Remote-control password"
    assert fields["behaviour"].kind == "select"
    assert fields["behaviour"].choices == ("normal", "slow", "offline")
    assert fields["behaviour"].default == "normal"


def test_secret_defaults_never_reach_the_form() -> None:
    class Leaky(ConnectionSettings):
        password: SecretStr = SecretStr("hunter2-default")
        optional_password: SecretStr | None = None
        admin: bool = True

    fields = {field.name: field for field in describe_fields(Leaky)}

    assert fields["password"].default is None
    assert "hunter2" not in repr(fields)
    assert fields["optional_password"].kind == "password"
    assert not fields["optional_password"].required
    assert (fields["admin"].kind, fields["admin"].default) == ("checkbox", True)


def test_unsupported_field_types_are_reported() -> None:
    class Odd(ConnectionSettings):
        ratio: float = 1.0

    with pytest.raises(TypeError, match="ratio"):
        describe_fields(Odd)


def test_values_are_split_into_public_and_secret() -> None:
    connection = DemoConnection(host="demo.lan", password=SecretStr("demo"))

    public, secret = split_values(connection)

    assert public == {"host": "demo.lan", "port": 27015, "behaviour": "normal", "players": 4}
    assert secret == {"password": "demo"}
    assert secret_field_names(DemoConnection) == ["password"]
    assert "demo" not in repr(connection).replace("demo.lan", "")


def test_validation_errors_are_described_without_the_input() -> None:
    with pytest.raises(ValidationError) as exc:
        DemoConnection.model_validate(
            {"host": "bad host", "port": "hunter2-not-a-port", "password": "x"}
        )

    messages = describe_errors(exc.value)

    assert set(messages) == {"host", "port"}
    assert "host name or an IP address" in messages["host"]
    assert "hunter2" not in str(messages)


# --- adapter class validation --------------------------------------------------------------


def test_valid_adapters_pass_validation() -> None:
    validate_adapter_class(StatusOnlyAdapter)
    validate_adapter_class(DemoAdapter)


def adapter_class(**attributes: object) -> type[GameAdapter]:
    async def query(self: GameAdapter) -> ServerStatus:
        return ServerStatus(name="x", players=0, max_players=8)

    body = {
        "id": "broken",
        "name": "Broken",
        "family": "test",
        "capabilities": frozenset({Capability.STATUS}),
        "connection_schema": StatusOnlyConnection,
        "query": query,
        **attributes,
    }
    return type("Broken", (GameAdapter,), {k: v for k, v in body.items() if v is not None})


async def _list_maps(self: GameAdapter) -> list[MapRef]:
    return []


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        ({"id": "Has-Caps"}, "id"),
        ({"id": None}, "does not define 'id'"),
        ({"capabilities": frozenset()}, "Capability.STATUS"),
        ({"capabilities": {"status"}}, "frozenset of Capability"),
        ({"connection_schema": dict}, "connection_schema"),
        (
            {"capabilities": frozenset({Capability.STATUS, Capability.MAP_LIST})},
            r"list_maps\(\) but does not implement it",
        ),
        ({"list_maps": _list_maps}, r"implements list_maps\(\) but declares no capability"),
    ],
)
def test_inconsistent_adapter_classes_are_rejected(
    attributes: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        validate_adapter_class(adapter_class(**attributes))


def test_adapter_rejects_foreign_connection_settings() -> None:
    with pytest.raises(TypeError, match="needs DemoConnection"):
        DemoAdapter(StatusOnlyConnection(host="game.lan"))


# --- registry ------------------------------------------------------------------------------


def test_installed_adapters_are_found_through_entry_points() -> None:
    registry = load_registry()

    assert registry.get("demo") is DemoAdapter
    assert "demo" in registry
    assert DemoAdapter in registry.all()
    assert DemoAdapter not in registry.all(include_demo=False)


def test_registry_rejects_duplicates_and_non_adapters() -> None:
    registry = AdapterRegistry()
    registry.register(StatusOnlyAdapter)
    registry.register(StatusOnlyAdapter)  # registering the same class twice is harmless
    impostor = adapter_class(id="status_only")

    with pytest.raises(ValueError, match="already used"):
        registry.register(impostor)
    with pytest.raises(TypeError):
        registry.register(dict)  # type: ignore[arg-type]
    assert len(registry) == 1


def test_broken_plugin_does_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    registry = AdapterRegistry()
    points = [
        EntryPoint("missing", "lanpilot_no_such_module:Adapter", "lanpilot.adapters"),
        EntryPoint("not_an_adapter", "lanpilot.config:Settings", "lanpilot.adapters"),
        EntryPoint("demo", "lanpilot.games.demo:DemoAdapter", "lanpilot.adapters"),
    ]

    registry.load_entry_points(points)

    assert [adapter.id for adapter in registry.all()] == ["demo"]
    assert "could not load game adapter 'missing'" in caplog.text
    assert "could not load game adapter 'not_an_adapter'" in caplog.text


# --- demo adapter and the contract ---------------------------------------------------------


async def test_demo_adapter_fulfils_the_contract() -> None:
    await check_adapter(demo())


async def test_status_only_adapter_fulfils_the_contract() -> None:
    await check_adapter(StatusOnlyAdapter(StatusOnlyConnection(host="game.lan")))


async def test_contract_catches_an_adapter_that_ignores_map_changes() -> None:
    class Stubborn(DemoAdapter):
        id = "stubborn"

        async def change_map(self, target: MapRef) -> None:
            return None

    with pytest.raises(AssertionError, match="still reports another map"):
        await check_adapter(Stubborn(DemoConnection(password=SecretStr("demo"))))


async def test_demo_status_and_map_change() -> None:
    adapter = demo(players=2)

    status = await adapter.query()
    assert (status.players, status.max_players) == (2, 6)
    assert status.map is not None and status.map.key == "dm_foundry"
    assert status.load["fps"] == 512.0

    await adapter.change_map(MapRef("ctf_canyon"))
    status = await adapter.query()
    assert status.map is not None
    assert (status.map.key, status.map.label, status.game_type) == (
        "ctf_canyon",
        "Canyon",
        "Capture the Flag",
    )

    with pytest.raises(CommandRejectedError, match="no map named"):
        await adapter.change_map(MapRef("de_dust2"))


async def test_demo_players_kick_and_restart() -> None:
    adapter = demo(players=3)
    players = await adapter.list_players()
    assert [player.name for player in players] == ["Ada", "Grace", "Linus"]

    await adapter.kick(players[1])
    assert [player.name for player in await adapter.list_players()] == ["Ada", "Linus"]
    with pytest.raises(CommandRejectedError):
        await adapter.kick(Player(id="99", name="ghost"))

    await adapter.restart(RestartMode.SOFT)
    assert all(player.score == 0 for player in await adapter.list_players())
    assert adapter.restarts == [RestartMode.SOFT]


async def test_demo_console() -> None:
    adapter = demo()

    assert await adapter.console("echo hello world") == "hello world"
    assert "map: dm_foundry" in await adapter.console("status")
    assert await adapter.console("quit") == "Unknown command: quit"


async def test_demo_wrong_password_allows_status_but_not_control() -> None:
    adapter = demo(password="wrong-password")

    await adapter.query()
    with pytest.raises(AuthenticationError) as exc:
        await adapter.list_maps()
    assert "wrong-password" not in str(exc.value)


async def test_demo_offline_server_is_unreachable() -> None:
    with pytest.raises(ServerUnreachableError):
        await demo(behaviour="offline").query()


async def test_unsupported_calls_raise_unsupported_error() -> None:
    adapter = StatusOnlyAdapter(StatusOnlyConnection(host="game.lan"))

    with pytest.raises(UnsupportedError, match="cannot change the map"):
        await adapter.change_map(MapRef("x"))
    with pytest.raises(UnsupportedError):
        await adapter.console("status")
    with pytest.raises(UnsupportedError):
        await StatusOnlyAdapter.discover([IPv4Network("10.0.0.0/24")], 1.0)
    assert not adapter.supports(Capability.MAP_CHANGE)


def test_literal_type_is_available_for_schemas() -> None:
    class Choice(ConnectionSettings):
        mode: Literal["a", "b"] = "a"

    [_, _, mode] = describe_fields(Choice)
    assert mode.choices == ("a", "b")
