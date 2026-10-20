"""Unreal Tournament 3 (Unreal Engine 3): status and control over Epic's WebAdmin.

UT3's query is the LAN beacon on UDP 14001, which answers only queries sent from that port;
it cannot be used from the machine that runs the server and needs that port free elsewhere.
The status is therefore read from the WebAdmin as well, so it needs the password (as on
TrackMania Forever). Protocol facts and how they were measured:
docs/games/unreal-tournament.md.

The WebAdmin must allow HTTP authentication (``bHttpAuth=true`` in ``[WebAdmin.WebAdmin]``);
otherwise it only offers its login form.

Players: the status page's scoreboard lists everyone with score and a bot flag; the player
page (``current/players``) lists only human players, each with a form that names the player
by a key (player id, unique id and join time) and an action. A ban (by IP address) also
kicks; bots cannot be removed there.

The console (``/console``) answers on the same page, unless ``bConsoleEnabled=false`` in
``[WebAdmin.WebAdmin]`` switched it off (then the page does not exist).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network
from typing import ClassVar

from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    DiscoveredServer,
    GameAdapter,
    MapRef,
    Player,
    ProtocolError,
    RestartMode,
    ServerStatus,
    UnsupportedError,
)
from lanpilot.core.transports.uweb import (
    Page,
    PageNotFoundError,
    UWebAdmin,
    parse,
    table,
    text_lines,
)
from lanpilot.games.unreal.common import (
    GAME_CLASS,
    MAP_NAME,
    MAX_NAME_LENGTH,
    parse_count,
    split_options,
    valid_options,
)
from lanpilot.games.unreal.discovery import discover_ut3
from lanpilot.i18n import M

ROOT = "/ServerAdmin"
GAME_CLASS_KEY = "game_class"
OPTIONS_KEY = "options"
# "DM-Deck (Deck)" -> title "Deck"; "WAR-Avalanche?LinkSetup=Alt (Avalanche Two Fronts)".
LABEL_TITLE = re.compile(r".*\(([^()]{1,100})\)\s*")
PLAYERS = re.compile(r"(\d{1,4})\s*\((\d{1,4})\)")
LINK_SETUP = re.compile(r"\?LinkSetup=[^?]*", re.IGNORECASE)
CHANGING = "changing the current game"
# getPlayerKey(): PlayerID "_" unique id "_" CreationTime, e.g. "257_0x0000000000000000_31.42".
PLAYER_KEY = re.compile(r"[0-9]{1,9}_[0-9A-Za-z]{1,40}_[0-9.]{1,20}")
TEAMS = {"0": "Red", "1": "Blue"}


class UT3Connection(ConnectionSettings):
    port: int = Field(
        default=8075,
        title=M("WebAdmin port"),
        description=M("ListenPort in [IpDrv.WebServer] of UTWeb.ini (the game's default is 8080)."),
        ge=1,
        le=65535,
    )
    username: str = Field(
        default="admin",
        title=M("WebAdmin user"),
        description=M("With the standard access control the user is admin."),
        min_length=1,
        max_length=64,
    )
    password: SecretStr = Field(
        title=M("WebAdmin password"),
        description=M(
            "AdminPassword in [Engine.AccessControl] of UTGame.ini. The WebAdmin needs "
            "bHttpAuth=true in [WebAdmin.WebAdmin]."
        ),
    )


class UT3Adapter(GameAdapter):
    id = "ut3"
    name = "Unreal Tournament 3"
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
            Capability.DISCOVER,
        }
    )
    connection_schema = UT3Connection
    image_lookup: ClassVar[Mapping[str, str]] = {
        "unreal_archive": "Unreal Tournament 3",
        "liandri": "Unreal Tournament 3",
    }

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, UT3Connection)  # noqa: S101 - see base class
        self._web = UWebAdmin(
            connection.host,
            connection.port,
            connection.username,
            connection.password.get_secret_value(),
        )
        # Learnt from the map list: game type class -> name, map key -> title.
        self._game_names: dict[str, str] = {}
        self._titles: dict[str, str] = {}

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        return await discover_ut3(cls.id, networks, wait)

    async def _get(self, path: str) -> Page:
        return self._checked(parse(await self._web.get(path)))

    async def _post(self, path: str, fields: dict[str, str]) -> tuple[Page, str]:
        answer = await self._web.post(path, fields)
        return self._checked(parse(answer)), answer

    def _checked(self, page: Page) -> Page:
        # Without valid HTTP credentials UT3 answers with its login form, not with 401.
        if "token" in page.inputs and "username" in page.inputs:
            self._web.refuse()
            raise AuthenticationError(M("The server refused the WebAdmin user name or password."))
        return page

    async def query(self) -> ServerStatus:
        page = await self._get(f"{ROOT}/current")
        facts = page.definitions
        name, game, current = facts.get("Server Name"), facts.get("Game type"), facts.get("Map")
        if name is None or game is None or current is None:
            raise ProtocolError(M("The WebAdmin's status page has an unexpected format."))
        key, _ = split_options(current[0]) if current else ("", "")
        players = PLAYERS.fullmatch(" ".join(facts.get("Players (Max)", [])))
        game_class = game[-1] if game else ""
        return ServerStatus(
            name=(name[0] if name else "")[:MAX_NAME_LENGTH],
            players=int(players.group(1)) if players else 0,
            max_players=int(players.group(2)) if players else 0,
            map=MapRef(key, self._titles.get(key.lower())) if MAP_NAME.fullmatch(key) else None,
            game_type=self._game_names.get(game_class, game[0] if game else None),
        )

    async def list_maps(self) -> Sequence[MapRef]:
        page = await self._get(f"{ROOT}/current/change")
        game_types = [
            option
            for option in page.options("gametype")
            if GAME_CLASS.fullmatch(option.value) and option.label.isprintable()
        ]
        if not game_types:
            raise ProtocolError(M("The WebAdmin did not list any game types."))
        maps: list[MapRef] = []
        for game_type in game_types:
            label = game_type.label[:100] or game_type.value
            self._game_names[game_type.value] = label
            answer, _ = await self._post(
                f"{ROOT}/current/change/data", {"ajax": "1", "gametype": game_type.value}
            )
            seen: set[str] = set()
            for option in answer.options("map"):
                name, options = split_options(option.value)
                if not MAP_NAME.fullmatch(name) or not valid_options(options):
                    continue
                if (name + options).lower() in seen:
                    continue
                seen.add((name + options).lower())
                title = LABEL_TITLE.fullmatch(option.label)
                if title and not options:
                    self._titles[name.lower()] = title.group(1).strip()
                extra = {GAME_CLASS_KEY: game_type.value}
                if options:
                    extra[OPTIONS_KEY] = options
                maps.append(
                    MapRef(
                        name if not options else name + options,
                        title.group(1).strip() if title else None,
                        label,
                        extra,
                    )
                )
        return maps

    async def change_map(self, target: MapRef) -> None:
        name, options = split_options(target.key)
        options = target.extra.get(OPTIONS_KEY, options)
        game_class = target.extra.get(GAME_CLASS_KEY) or await self._current_class()
        await self._change(game_class, name, options)

    async def restart(self, mode: RestartMode) -> None:
        if mode is RestartMode.SOFT:
            raise UnsupportedError(M("{game} cannot restart only the round", game=self.name))
        page = await self._get(f"{ROOT}/current")
        current, game = page.definitions.get("Map"), page.definitions.get("Game type")
        if not current or not game:
            raise ProtocolError(M("The WebAdmin did not tell which game is running."))
        name, _ = split_options(current[0])
        # The status page leaves out the map's layout (?LinkSetup=...); the running URL
        # options in the change form have it, and a reload keeps it.
        await self._change(game[-1], name, None)

    async def list_players(self) -> Sequence[Player]:
        scoreboard = self._scoreboard(await self._get(f"{ROOT}/current"))
        humans = self._humans(await self._get(f"{ROOT}/current/players"))
        names = [name for name, _, _ in scoreboard]
        players = [
            Player(
                id=key,
                name=name,
                # The scoreboard has no key; a score is matched by a name that is unique.
                score=next((score for other, score, _ in scoreboard if other == name), None)
                if names.count(name) == 1
                else None,
                ping=ping,
                team=team,
            )
            for key, name, ping, team in humans
        ]
        players.extend(
            Player(
                id=f"bot-{number}",
                name=name,
                score=score,
                bot=True,
                can_kick=False,
                can_ban=False,
            )
            for number, (name, score, bot) in enumerate(scoreboard)
            if bot
        )
        return players

    async def kick(self, player: Player, reason: str = "") -> None:
        await self._remove(player, "kick")

    async def ban(self, player: Player, reason: str = "") -> None:
        await self._remove(player, "banip")

    async def _remove(self, player: Player, action: str) -> None:
        if not PLAYER_KEY.fullmatch(player.id):
            raise CommandRejectedError(M("This player cannot be removed over the WebAdmin."))
        page, _ = await self._post(
            f"{ROOT}/current/players",
            {"playerid": player.id.split("_", 1)[0], "playerkey": player.id, "action": action},
        )
        # The answer is the player page after the action (its messages are not localised
        # on every server, so the list is what tells).
        if any(key == player.id for key, _, _, _ in self._humans(page)):
            raise CommandRejectedError(M("The server did not remove the player."))

    async def console(self, command: str) -> str:
        try:
            _, answer = await self._post(f"{ROOT}/console", {"command": command})
        except PageNotFoundError as exc:
            raise UnsupportedError(
                M("The WebAdmin's console is switched off (bConsoleEnabled in UTWeb.ini).")
            ) from exc
        lines = text_lines(answer, "consoleResults")
        if lines is None:
            raise ProtocolError(M("The WebAdmin's console page has an unexpected format."))
        # The first line repeats the command ("> command").
        return "\n".join(lines[1:])

    @staticmethod
    def _scoreboard(page: Page) -> list[tuple[str, int | None, bool]]:
        """Name, score and bot flag of everyone on the status page's scoreboard."""
        columns, rows = table(page, "player name", "score", "bot")
        if not columns:
            raise ProtocolError(M("The WebAdmin's status page has an unexpected format."))
        return [
            (
                row[columns["player name"]].text[:MAX_NAME_LENGTH],
                parse_count(row[columns["score"]].text, signed=True),
                row[columns["bot"]].text.lower() == "true",
            )
            for row in rows
            if row[columns["bot"]].text  # not the "There are no players" row
        ]

    @staticmethod
    def _humans(page: Page) -> list[tuple[str, str, int | None, str | None]]:
        """Key, name, ping and team of the human players on the player page."""
        columns, rows = table(page, "player name", "ping", "controls")
        if not columns:
            raise ProtocolError(M("The WebAdmin's player page has an unexpected format."))
        humans = []
        for row in rows:
            key = row[columns["controls"]].inputs.get("playerkey")
            if key is None or not PLAYER_KEY.fullmatch(key):
                continue
            team = TEAMS.get(row[0].text) if columns.get("") == 0 else None
            humans.append(
                (
                    key,
                    row[columns["player name"]].text[:MAX_NAME_LENGTH],
                    parse_count(row[columns["ping"]].text),
                    team,
                )
            )
        return humans

    async def _current_class(self) -> str:
        game = (await self._get(f"{ROOT}/current")).definitions.get("Game type")
        if not game or not GAME_CLASS.fullmatch(game[-1]):
            raise ProtocolError(M("The WebAdmin did not tell which game type is running."))
        return game[-1]

    async def _change(self, game_class: str, name: str, options: str | None) -> None:
        """Load ``name`` with ``options``; ``None`` keeps the running map's layout."""
        form = await self._get(f"{ROOT}/current/change")
        # The form carries the running mutators and URL options. They are sent back so a
        # change keeps them, except the layout (?LinkSetup=), which belongs to the map.
        running = form.inputs.get("urlextra", "")
        if options is None:
            layouts = LINK_SETUP.findall(running)
            options = layouts[-1] if layouts else ""
        if (
            not GAME_CLASS.fullmatch(game_class)
            or not MAP_NAME.fullmatch(name)
            or not valid_options(options)
        ):
            raise CommandRejectedError(M("This map or game type cannot be sent to the server."))
        fields = {
            "gametype": game_class,
            "map": name + options,
            "action": "change",
            "mutatorGroupCount": form.inputs.get("mutatorGroupCount", "0"),
            "urlextra": LINK_SETUP.sub("", running),
        }
        for field_name, values in form.checked.items():
            if field_name.startswith("mutgroup") and values:
                fields[field_name] = values[0]
        _, answer = await self._post(f"{ROOT}/current/change", fields)
        if CHANGING not in answer.lower():
            raise CommandRejectedError(M("The WebAdmin did not accept the change."))
