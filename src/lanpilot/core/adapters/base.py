"""The adapter interface: what LANPilot needs to know about and do with a game server.

One adapter class per game. An adapter declares its capabilities and a connection schema,
and implements the methods that belong to the capabilities it declared. LANPilot creates one
instance per configured server and keeps it for as long as the server is configured, so an
adapter may hold a connection open.

Everything a game server sends back is untrusted input. Adapters must parse defensively,
bound what they read, and never put text that came from a user or a server into a command
without validating it.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Network
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lanpilot.i18n import M

ADAPTER_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,31}")
HOST_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")


class Capability(StrEnum):
    STATUS = "status"  # query()
    MAP_LIST = "map_list"  # list_maps()
    MAP_CHANGE = "map_change"  # change_map()
    RESTART_SOFT = "restart_soft"  # restart(RestartMode.SOFT)
    RESTART_HARD = "restart_hard"  # restart(RestartMode.HARD)
    PLAYERS = "players"  # list_players()
    KICK = "kick"  # kick()
    BAN = "ban"  # ban()
    CONSOLE = "console"  # console()
    MANUAL_MAPS = "manual_maps"  # look_up_map(): maps the server has but does not list
    LOAD_STATS = "load_stats"  # query() fills ServerStatus.load
    DISCOVER = "discover"  # discover()


class RestartMode(StrEnum):
    SOFT = "soft"  # restart the round or match; players stay connected
    HARD = "hard"  # reload the level completely


class AdapterError(Exception):
    """Base class. The message is shown to operators, so it must not contain secrets."""


class ServerUnreachableError(AdapterError):
    """No answer: the server is down, the address is wrong, or a firewall is in the way."""


class AuthenticationError(AdapterError):
    """The server refused the remote-control password."""


class CommandRejectedError(AdapterError):
    """The server understood the request and refused it, for example an unknown map."""


class ProtocolError(AdapterError):
    """The server answered with something the adapter could not make sense of."""


class UnsupportedError(AdapterError):
    """The adapter does not have the capability that was asked for."""


@dataclass(frozen=True)
class MapRef:
    """A map as the server knows it.

    ``key`` is what is sent back to the server to select the map. ``extra`` carries
    identifiers that help finding a preview image, such as a Trackmania ``uid`` or a Steam
    ``workshop_id``.
    """

    key: str
    name: str | None = None
    game_type: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.key


@dataclass(frozen=True)
class Player:
    id: str  # what kick() and ban() need; unique on the server
    name: str
    score: int | None = None
    ping: int | None = None
    team: str | None = None
    bot: bool = False
    # False where the protocol offers no way for this entry (e.g. bots on UT99's WebAdmin).
    can_kick: bool = True
    can_ban: bool = True


@dataclass(frozen=True)
class ServerStatus:
    name: str
    players: int
    max_players: int
    map: MapRef | None = None
    game_type: str | None = None
    password_protected: bool | None = None
    # Whatever the protocol itself reports, e.g. {"cpu": 12.5, "fps": 512.0}.
    load: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveredServer:
    """A server found by a LAN scan.

    ``host`` and ``port`` are the values for the connection form; ``port`` is ``None`` when
    the scan cannot learn the remote-control port, so the form keeps its default. ``values``
    pre-fills further public connection fields, such as a query port.
    """

    adapter_id: str
    host: str
    port: int | None = None
    name: str | None = None
    game_type: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)


class ConnectionSettings(BaseModel):
    """Base class for an adapter's connection schema.

    Subclasses add the fields their protocol needs. Supported field types are ``str``,
    ``int``, ``bool``, ``Literal[...]`` of strings and ``SecretStr``; a ``title`` and a
    ``description`` become the label and help text of the generated form. ``SecretStr``
    fields are stored encrypted and are never sent back to the browser.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(
        title=M("Host"),
        description=M("IP address or host name of the game server."),
        min_length=1,
        max_length=253,
    )
    port: int = Field(title=M("Port"), ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        host = value.strip()
        if not HOST_PATTERN.fullmatch(host):
            raise ValueError(M("must be a host name or an IP address, without scheme or path"))
        return host


# Capability -> method that must be implemented when the capability is declared.
# LOAD_STATS has no method of its own: it promises that query() fills ServerStatus.load.
_CAPABILITY_METHODS: dict[Capability, str] = {
    Capability.STATUS: "query",
    Capability.MAP_LIST: "list_maps",
    Capability.MAP_CHANGE: "change_map",
    Capability.RESTART_SOFT: "restart",
    Capability.RESTART_HARD: "restart",
    Capability.PLAYERS: "list_players",
    Capability.KICK: "kick",
    Capability.BAN: "ban",
    Capability.CONSOLE: "console",
    Capability.MANUAL_MAPS: "look_up_map",
    Capability.DISCOVER: "discover",
}


class GameAdapter(ABC):
    """Base class for game adapters. See the module docstring and docs/architecture.md."""

    #: Stable identifier, stored with every server. Lower-case letters, digits, underscore.
    id: ClassVar[str]
    #: Name shown to users, e.g. "Counter-Strike 1.6".
    name: ClassVar[str]
    #: Group of related games, e.g. "counterstrike"; names the catalogue file. Map images
    #: are not shared within a family: they belong to one game (``id``).
    family: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    connection_schema: ClassVar[type[ConnectionSettings]]
    #: Identifiers online image sources need for this game, by source: e.g.
    #: ``{"gametracker": "css", "gamebanana": "2", "steam_app": "730"}``. Optional.
    image_lookup: ClassVar[Mapping[str, str]] = {}

    def __init__(self, connection: ConnectionSettings) -> None:
        if not isinstance(connection, self.connection_schema):
            raise TypeError(
                f"{type(self).__name__} needs {self.connection_schema.__name__}, "
                f"got {type(connection).__name__}"
            )
        self.connection = connection

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    async def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release connections. Called when the server is removed or LANPilot shuts down."""

    @abstractmethod
    async def query(self) -> ServerStatus:
        """Return the current status. Called frequently; keep it cheap."""

    async def list_maps(self) -> Sequence[MapRef]:
        """Return the maps the server can switch to."""
        raise UnsupportedError(M("{game} cannot list maps", game=self.name))

    async def change_map(self, target: MapRef) -> None:
        """Switch to ``target``, which LANPilot has checked to be in the server's map list."""
        raise UnsupportedError(M("{game} cannot change the map", game=self.name))

    async def restart(self, mode: RestartMode) -> None:
        raise UnsupportedError(M("{game} cannot restart", game=self.name))

    async def list_players(self) -> Sequence[Player]:
        raise UnsupportedError(M("{game} cannot list players", game=self.name))

    async def kick(self, player: Player, reason: str = "") -> None:
        raise UnsupportedError(M("{game} cannot kick players", game=self.name))

    async def ban(self, player: Player, reason: str = "") -> None:
        raise UnsupportedError(M("{game} cannot ban players", game=self.name))

    async def look_up_map(self, key: str) -> MapRef:
        """Check that the server has a map it does not list (a track outside the playlist)
        and return it. Raise ``CommandRejectedError`` if it has none of that name.
        ``change_map()`` must be able to switch to every map returned here."""
        raise UnsupportedError(M("{game} cannot add maps by hand", game=self.name))

    async def console(self, command: str) -> str:
        """Run a raw console command and return the server's answer. Administrators only."""
        raise UnsupportedError(M("{game} has no console", game=self.name))

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        """Find servers of this game in ``networks``, on request only, without passwords.

        ``wait`` is how long to wait for answers to a broadcast. Return soon after it plus
        the time follow-up queries need, report only servers whose address lies inside
        ``networks``, and never raise for a host that answers oddly: skip it.
        ``lanpilot.core.discovery`` has the helpers.
        """
        raise UnsupportedError(M("{game} cannot be discovered", game=cls.name))


def validate_adapter_class(cls: type[GameAdapter]) -> None:
    """Raise ``TypeError`` if an adapter class is inconsistent.

    Called when an adapter is registered, so a broken adapter never reaches a running
    LANPilot and the user interface can rely on ``capabilities`` matching what the adapter
    really implements.
    """
    from lanpilot.core.adapters.schema import describe_fields

    where = f"{cls.__module__}.{cls.__qualname__}"
    for attribute in ("id", "name", "family", "capabilities", "connection_schema"):
        if not hasattr(cls, attribute):
            raise TypeError(f"{where} does not define '{attribute}'")
    if not ADAPTER_ID_PATTERN.fullmatch(cls.id):
        raise TypeError(f"{where}: id {cls.id!r} must match {ADAPTER_ID_PATTERN.pattern}")
    if not isinstance(cls.capabilities, frozenset) or not all(
        isinstance(item, Capability) for item in cls.capabilities
    ):
        raise TypeError(f"{where}: capabilities must be a frozenset of Capability")
    if Capability.STATUS not in cls.capabilities:
        raise TypeError(f"{where}: every adapter must have Capability.STATUS")
    if not issubclass(cls.connection_schema, ConnectionSettings):
        raise TypeError(f"{where}: connection_schema must derive from ConnectionSettings")
    describe_fields(cls.connection_schema)  # raises on unsupported field types

    overriding = [base for base in cls.__mro__ if base not in (GameAdapter, ABC, object)]
    for method in sorted(set(_CAPABILITY_METHODS.values())):
        implemented = any(method in vars(base) for base in overriding)
        declared = any(
            capability in cls.capabilities
            for capability, name in _CAPABILITY_METHODS.items()
            if name == method
        )
        if declared and not implemented:
            raise TypeError(
                f"{where}: declares a capability for {method}() but does not implement it"
            )
        if implemented and not declared:
            raise TypeError(f"{where}: implements {method}() but declares no capability for it")
