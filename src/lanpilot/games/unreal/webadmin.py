"""What UT99 and UT2004 share: status over the GameSpy query, control over the WebAdmin.

Maps belong to game types: the list holds every installed map for every game type the
WebAdmin offers (``current_game`` lists the game types, ``defaults_maps`` the maps in and
out of each type's rotation), and a change loads map and game type together. Neither game's
WebAdmin can restart just the round, so only the hard restart is offered. How the two games
differ is described in docs/games/unreal-tournament.md.

Players come from the WebAdmin's player page (``current_players``), a table with a kick and
a ban checkbox per player named after the player's id; a kick or ban posts that page with
the box ticked. The WebAdmins have no field for a reason. A ban is permanent: the server
stores it in its ini file.

The console posts a command to ``current_console``; the server writes the answer into the
WebAdmin's message log (``current_console_log``, lines starting with "> "), which also
collects chat. The answer is what the log gained while the command ran.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import ClassVar

from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    Player,
    ProtocolError,
    RestartMode,
    ServerStatus,
    UnsupportedError,
)
from lanpilot.core.transports.uweb import (
    Cell,
    Page,
    UWebAdmin,
    find_table,
    parse,
    table,
    text_lines,
)
from lanpilot.games.unreal.common import (
    GAME_CLASS,
    MAP_NAME,
    MAX_NAME_LENGTH,
    parse_count,
    query_status,
    split_options,
    valid_options,
)

ROOT = "/ServerAdmin"
GAME_CLASS_KEY = "game_class"
OPTIONS_KEY = "options"


class WebAdminConnection(ConnectionSettings):
    """The fields both games need; each game sets its own defaults and help texts."""

    query_port: int = Field(title="Query port", ge=1, le=65535)
    username: str = Field(default="admin", title="WebAdmin user", min_length=1, max_length=64)
    password: SecretStr = Field(title="WebAdmin password")


class WebAdminAdapter(GameAdapter):
    """Not registered itself."""

    family = "unreal"
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.MAP_CHANGE,
            Capability.RESTART_HARD,
            Capability.PLAYERS,
            Capability.KICK,
            Capability.BAN,
            Capability.CONSOLE,
        }
    )
    #: The extension the WebAdmin puts after map names (UT99: ".unr").
    map_extension: ClassVar[str] = ""
    #: The player page's checkboxes: name prefix (followed by the player id) and value.
    kick_box: ClassVar[tuple[str, str]] = ("", "")
    ban_box: ClassVar[tuple[str, str]] = ("", "")

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, WebAdminConnection)  # noqa: S101 - see base class
        self._host = connection.host
        self._query_port = connection.query_port
        self._web = UWebAdmin(
            connection.host,
            connection.port,
            connection.username,
            connection.password.get_secret_value(),
        )
        # "xdeathmatch" -> "DeathMatch", learnt from the WebAdmin's game type list.
        self._game_names: dict[str, str] = {}

    async def query(self) -> ServerStatus:
        return await query_status(self._host, self._query_port, self._game_names)

    async def list_maps(self) -> Sequence[MapRef]:
        page = parse(await self._web.get(f"{ROOT}/current_game"))
        game_types = [
            option
            for option in page.options("GameTypeSelect")
            if GAME_CLASS.fullmatch(option.value) and option.label.isprintable()
        ]
        if not game_types:
            raise ProtocolError("The WebAdmin did not list any game types.")
        maps: list[MapRef] = []
        for game_type in game_types:
            label = game_type.label[:100] or game_type.value
            self._game_names[game_type.value.rsplit(".", 1)[1].lower()] = label
            lists = parse(
                await self._web.get(f"{ROOT}/defaults_maps", {"GameType": game_type.value})
            )
            seen: set[str] = set()
            # In the rotation first, then the other installed maps of this game type.
            for option in [
                *lists.options("IncludeMapsSelect"),
                *lists.options("ExcludeMapsSelect"),
            ]:
                name, options = split_options(self._without_extension(option.value))
                if not MAP_NAME.fullmatch(name) or not valid_options(options):
                    continue
                if name.lower() in seen:  # the lists differ in case: DM-RANKIN, DM-Rankin
                    continue
                seen.add(name.lower())
                extra = {GAME_CLASS_KEY: game_type.value}
                if options:
                    extra[OPTIONS_KEY] = options
                maps.append(MapRef(name, None, label, extra))
        return maps

    async def change_map(self, target: MapRef) -> None:
        game_class = target.extra.get(GAME_CLASS_KEY) or await self._current_class()
        options = target.extra.get(OPTIONS_KEY, "")
        self._check(game_class, target.key, options)
        await self._travel(game_class, target.key, options)

    async def restart(self, mode: RestartMode) -> None:
        if mode is RestartMode.SOFT:
            raise UnsupportedError(f"{self.name} cannot restart only the round")
        await self._reload()

    async def list_players(self) -> Sequence[Player]:
        return self._players(parse(await self._web.get(f"{ROOT}/current_players")))

    async def kick(self, player: Player, reason: str = "") -> None:
        await self._remove(player, self.kick_box)

    async def ban(self, player: Player, reason: str = "") -> None:
        await self._remove(player, self.ban_box)

    async def _remove(self, player: Player, box: tuple[str, str]) -> None:
        if not re.fullmatch(r"[0-9]{1,9}", player.id):
            raise CommandRejectedError("This player cannot be removed over the WebAdmin.")
        prefix, value = box
        # "Kick" is the page's submit button; the server acts on the ticked box alone.
        answer = await self._web.post(
            f"{ROOT}/current_players", {prefix + player.id: value, "Kick": "Kick"}
        )
        # The answer is the player page after the removal.
        if any(other.id == player.id for other in self._players(parse(answer))):
            raise CommandRejectedError("The server did not remove the player.")

    def _players(self, page: Page) -> list[Player]:
        columns, rows = table(page, "name", "ping", "score")
        if not columns:
            raise ProtocolError("The WebAdmin's player page has an unexpected format.")
        name_column = columns["name"]
        # UT99 does not escape names, so a name can close its row and open forged ones. A
        # page where that may have happened is refused rather than guessed at: a forged row
        # repeats another player's id (the kick would hit that player), and a name that
        # closes the table puts the boxes of the rows after it outside the player table.
        boxes = re.compile(
            f"(?:{re.escape(self.kick_box[0])}|{re.escape(self.ban_box[0])})[0-9]{{1,9}}"
        )
        on_page = Counter(name for name in page.input_names if boxes.fullmatch(name))
        _, whole_table = find_table(page, "name", "ping", "score")
        in_table = Counter(
            name
            for row in whole_table
            for cell in row
            for name in cell.inputs
            if boxes.fullmatch(name)
        )
        ids = [
            self._box_id(row[:name_column], prefix)
            for row in rows
            for prefix in (self.kick_box[0], self.ban_box[0])
        ]
        kick_ids = ids[0::2]
        ban_ids = ids[1::2]
        repeated = any(
            len(present) != len(set(present))
            for present in ([i for i in kick_ids if i], [i for i in ban_ids if i])
        )
        if on_page != in_table or repeated:
            raise ProtocolError(
                "The WebAdmin's player page does not add up; a player name may be forged."
            )
        players: list[Player] = []
        for number, row in enumerate(rows):
            # The checkboxes stand before the name, which a player chooses; boxes after it
            # could be forged with a crafted name (UT99 does not escape names).
            kick_id = self._box_id(row[:name_column], self.kick_box[0])
            ban_id = self._box_id(row[:name_column], self.ban_box[0])
            name = row[name_column].text
            bot = ban_id is None and name.endswith("(Bot)")
            if bot:
                name = name.removesuffix("(Bot)").rstrip()
            team = row[columns["team"]].text if "team" in columns else ""
            players.append(
                Player(
                    id=kick_id or ban_id or f"bot-{number}",
                    name=name[:MAX_NAME_LENGTH],
                    score=parse_count(row[columns["score"]].text, signed=True),
                    ping=parse_count(row[columns["ping"]].text),
                    team=team or None,
                    bot=bot,
                    can_kick=kick_id is not None,
                    can_ban=ban_id is not None,
                )
            )
        return players

    @staticmethod
    def _box_id(cells: Sequence[Cell], prefix: str) -> str | None:
        pattern = re.compile(re.escape(prefix) + r"([0-9]{1,9})")
        for cell in cells:
            for name in cell.inputs:
                match = pattern.fullmatch(name)
                if match:
                    return match.group(1)
        return None

    async def console(self, command: str) -> str:
        before = await self._console_log()
        await self._web.post(f"{ROOT}/current_console", {"SendText": command, "Send": "Send"})
        return "\n".join(new_lines(before, await self._console_log()))

    async def _console_log(self) -> list[str]:
        lines = text_lines(await self._web.get(f"{ROOT}/current_console_log")) or []
        return [line[1:].strip() for line in lines if line.startswith(">")]

    async def _current_class(self) -> str:
        page = parse(await self._web.get(f"{ROOT}/current_game"))
        selected = [option.value for option in page.options("GameTypeSelect") if option.selected]
        if not selected or not GAME_CLASS.fullmatch(selected[0]):
            raise ProtocolError("The WebAdmin did not tell which game type is running.")
        return selected[0]

    def _without_extension(self, value: str) -> str:
        extension = self.map_extension
        name, options = split_options(value)
        if extension and name.lower().endswith(extension):
            name = name[: -len(extension)]
        return name + options

    @staticmethod
    def _check(game_class: str, name: str, options: str) -> None:
        if (
            not GAME_CLASS.fullmatch(game_class)
            or not MAP_NAME.fullmatch(name)
            or not valid_options(options)
        ):
            raise CommandRejectedError("This map or game type cannot be sent to the server.")

    @staticmethod
    def _expect_wait(answer: str) -> None:
        # The WebAdmin answers a travel with a "Please Wait" page, also for unknown maps.
        if "please wait" not in answer.lower():
            raise CommandRejectedError("The WebAdmin did not accept the change.")

    async def _travel(self, game_class: str, name: str, options: str) -> None:
        raise NotImplementedError

    async def _reload(self) -> None:
        raise NotImplementedError


def new_lines(before: Sequence[str], after: Sequence[str]) -> list[str]:
    """The lines ``after`` gained over ``before``: the log is a window over the last
    messages, so the longest end of ``before`` that ``after`` starts with is left out."""
    for overlap in range(min(len(before), len(after)), 0, -1):
        if list(after[:overlap]) == list(before[-overlap:]):
            return list(after[overlap:])
    return list(after)
