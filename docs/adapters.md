# Writing a game adapter

Support for a game is one class: an *adapter*. It says what the game can do and how to
reach a server, and implements the calls for it. LANPilot generates the "add server" form,
polls the status, stores the map list, checks permissions and writes the audit log; none of
that is the adapter's business.

The reference for the interface is [architecture.md](architecture.md#adapter-interface).
This page walks through a complete adapter. Its code is executed by the project's tests, so
it stays correct.

## The example game

"Lineworld" is an imaginary game whose dedicated server has a text protocol on a TCP port.
A client sends one line and gets one line back:

| Request | Answer |
|---|---|
| `STATUS` | `OK <server name>\|<map>\|<players>\|<max players>` |
| `MAPS <password>` | `OK <map>,<map>,…` |
| `MAP <password> <map>` | `OK` |
| anything with a wrong password | `ERR auth` |
| an unknown map | `ERR nomap` |

## The adapter

<!-- example: adapter -->
```python
import asyncio
import re
from collections.abc import Sequence

from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    ProtocolError,
    ServerStatus,
    ServerUnreachableError,
)

# What a map name may look like. Checked before a name is put into a command.
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_LINE = 64 * 1024


class LineworldConnection(ConnectionSettings):
    """How to reach a server. The "add server" form is generated from these fields."""

    port: int = Field(default=4000, title="Port", ge=1, le=65535)
    password: SecretStr = Field(
        title="Admin password",
        description="The value of admin_password in the server's lineworld.cfg.",
    )


class LineworldAdapter(GameAdapter):
    id = "lineworld"
    name = "Lineworld"
    family = "lineworld"
    capabilities = frozenset({Capability.STATUS, Capability.MAP_LIST, Capability.MAP_CHANGE})
    connection_schema = LineworldConnection

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, LineworldConnection)
        self._settings = connection

    async def _ask(self, line: str) -> str:
        """Send one line and return the payload of the answer."""
        try:
            reader, writer = await asyncio.open_connection(
                self._settings.host, self._settings.port, limit=MAX_LINE
            )
        except OSError as exc:
            raise ServerUnreachableError("The server did not accept the connection.") from exc
        try:
            writer.write(line.encode() + b"\n")
            await writer.drain()
            answer = (await reader.readline()).decode(errors="replace").strip()
        except (OSError, ValueError) as exc:
            raise ProtocolError("The server sent an answer LANPilot cannot read.") from exc
        finally:
            writer.close()

        status, _, payload = answer.partition(" ")
        if status == "OK":
            return payload
        if answer == "ERR auth":
            raise AuthenticationError("The server refused the admin password.")
        if answer == "ERR nomap":
            raise CommandRejectedError("The server does not have this map.")
        raise ProtocolError("The server sent an answer LANPilot does not understand.")

    async def query(self) -> ServerStatus:
        fields = (await self._ask("STATUS")).split("|")
        try:
            name, current, players, max_players = fields
            return ServerStatus(
                name=name[:100],
                players=int(players),
                max_players=int(max_players),
                map=MapRef(current) if MAP_NAME.fullmatch(current) else None,
            )
        except ValueError as exc:
            raise ProtocolError("The status answer has an unexpected format.") from exc

    async def list_maps(self) -> Sequence[MapRef]:
        password = self._settings.password.get_secret_value()
        names = (await self._ask(f"MAPS {password}")).split(",")
        return [MapRef(name) for name in names if MAP_NAME.fullmatch(name)]

    async def change_map(self, target: MapRef) -> None:
        if not MAP_NAME.fullmatch(target.key):
            raise CommandRejectedError("This map name cannot be sent to the server.")
        password = self._settings.password.get_secret_value()
        await self._ask(f"MAP {password} {target.key}")
```

What to take from it:

- **`capabilities` and the methods must agree.** LANPilot refuses an adapter that declares
  a capability without implementing its method, or the other way round.
- **Secrets are `SecretStr` fields.** They are stored encrypted, never shown again, and kept
  out of the log. Never put a password into an exception message.
- **Raise the adapter errors.** Their messages are shown to operators:
  `ServerUnreachableError`, `AuthenticationError`, `CommandRejectedError`, `ProtocolError`.
- **Distrust the server.** Bound what you read (`limit=`), tolerate garbage, and drop
  entries that do not look like what you expect, as `list_maps()` does.
- **Validate before you send.** LANPilot only passes maps from the stored list to
  `change_map()`, but that list came from the server. Check every value that goes into a
  command against a strict pattern, so a crafted map name cannot smuggle in a second
  command.
- **Do not add timeouts around whole calls.** LANPilot limits every call; an adapter only
  needs timeouts where its protocol has no end marker.
- LANPilot keeps one adapter instance per server and calls it from one task at a time. An
  adapter may therefore keep a connection open; release it in `close()`.
- **Map images** are looked up online with the identifiers the adapter declares in the
  optional class attribute `image_lookup`, e.g. `{"gametracker": "css", "gamebanana": "2",
  "steam_app": "730"}` (GameTracker's game slug, GameBanana's game id, Steam app id) or
  `{"tmx": "tmnf.exchange"}` (TrackMania Exchange sites). A map whose `MapRef.extra` carries
  a `workshop_id` is looked up in the Steam Workshop, one with a `uid` on TMX.
- **Maps the server does not list** (e.g. Trackmania tracks outside the playlist): declare
  `Capability.MANUAL_MAPS` and implement `look_up_map(key)`, which asks the server whether
  it has that map and returns it as a `MapRef`. Administrators can then add such maps on
  the server's "Maps" page; `change_map()` must cope with them.
- **Players** (`Capability.PLAYERS`, `KICK`, `BAN`): `list_players()` returns `Player`
  entries whose `id` is what `kick()` and `ban()` need, unique on the server. Mark bots with
  `bot=True` and set `can_kick`/`can_ban` to `False` for entries the protocol cannot remove;
  the interface then shows no button for them. LANPilot lists the players again right before
  a kick or ban and passes the fresh entry. The `reason` is free text from an operator:
  leave it out or reduce it to harmless characters before it goes into a command.
- **Console** (`Capability.CONSOLE`): `console(command)` sends one line as it is and returns
  the server's answer. Only administrators can use it, and only when the console is switched
  on in the configuration.
- **Stock map names** for the map picker can be added to the catalogue in
  `src/lanpilot/catalog/<family>.toml`: a table named after the adapter id with
  `map_key = "Display name"` lines.

## Testing it

Tests must not need a real game server. Write a small simulator of the protocol and run the
contract check against it. `check_adapter()` exercises every capability the adapter declares
and verifies that everything else is refused.

<!-- example: test -->
```python
import asyncio

from pydantic import SecretStr

from lanpilot.testing.contract import check_adapter

MAPS = ["meadow", "canyon", "harbor"]


async def lineworld_simulator(password: str) -> asyncio.Server:
    current = [MAPS[0]]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        words = (await reader.readline()).decode().split()
        if words == ["STATUS"]:
            answer = f"OK Test server|{current[0]}|2|8"
        elif len(words) >= 2 and words[1] != password:
            answer = "ERR auth"
        elif words[0] == "MAPS":
            answer = "OK " + ",".join(MAPS)
        elif words[0] == "MAP" and words[2] in MAPS:
            current[0] = words[2]
            answer = "OK"
        else:
            answer = "ERR nomap"
        writer.write(answer.encode() + b"\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_lineworld_fulfils_the_contract() -> None:
    server = await lineworld_simulator("s3cret")
    port = server.sockets[0].getsockname()[1]
    connection = LineworldConnection(host="127.0.0.1", port=port, password=SecretStr("s3cret"))
    try:
        await check_adapter(LineworldAdapter(connection))
    finally:
        server.close()
        await server.wait_closed()
```

Add tests of your own for what the contract cannot know: wrong passwords, malformed
answers, and every quirk of the real server you ran into.

## Registering it

Adapters are found through the entry point group `lanpilot.adapters`.

For an adapter that ships with LANPilot, put the module under `src/lanpilot/games/` and add
a line to `pyproject.toml`:

```toml
[project.entry-points."lanpilot.adapters"]
lineworld = "lanpilot.games.lineworld:LineworldAdapter"
```

An adapter can also live in its own Python package with the same section in its
`pyproject.toml`. Installing that package into LANPilot's environment makes the game
appear under "Add a server"; LANPilot itself is not changed. A package that fails to load
is logged and skipped.

## Before you open a pull request

1. The adapter, and a transport module under `core/transports/` if other games share the
   protocol.
2. A simulator or recorded answers, a test calling `check_adapter()`, and tests for errors.
3. `docs/games/<game>.md` with what you verified against a **real** server: versions,
   ports, how to enable remote control, exact commands, quirks.
4. A line in `CHANGELOG.md`.

Games without any remote control are welcome too: an adapter with only `STATUS` puts them
on the dashboard.
