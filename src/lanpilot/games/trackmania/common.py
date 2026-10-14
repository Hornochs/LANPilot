"""What the Trackmania games of the "challenge" generation share (GBXRemote 2): TrackMania
Sunrise and Nations ESWC (2006) and TrackMania Forever (2008).

The map list is the playlist (``GetChallengeList``); a map's key is the track's file name.
Administrators can add track files that are on the server but not in the playlist
(``look_up_map``); changing to one inserts it into the playlist first. Players are named by
their login; a ban lasts until the server restarts (it is not written to the blacklist).
Protocol facts and how they were measured: docs/games/trackmania.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal

from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    Player,
    ProtocolError,
    RestartMode,
    ServerStatus,
)
from lanpilot.core.transports.gbxremote import PERMISSION_DENIED, GbxFaultError, GbxRemote

GAME_MODES = {0: "Rounds", 1: "Time Attack", 2: "Team", 3: "Laps", 4: "Stunts", 5: "Cup"}
TRACK_UNKNOWN = "Track unknown."  # TMF ChooseNextChallenge: no such file
CHALLENGE_UNKNOWN = "Challenge unknown."  # no such file (TMF GetChallengeInfo, Sunrise all)
NOT_IN_SELECTION = "Challenge not in the selection."  # the file exists, not in the playlist
# TMF: answer to NextChallenge and RestartChallenge while a track is being loaded (about 6 s).
CHANGE_IN_PROGRESS = "Change in progress."
# Sunrise: answer to GetCurrentChallengeInfo for about a second while a track is loaded.
NOTHING_LOADED = "No challenge currently loaded."
# What GetServerPassword says to a client that may not read the password itself.
NO_PASSWORD = "No password"  # noqa: S105 - the server's sentence
PASSWORD_REQUIRED = "Password required"  # noqa: S105 - the server's sentence
LOGIN_UNKNOWN = "Login unknown."  # Kick, Ban: nobody with this login is on the server
MAX_PLAYERS = 255
TEAMS = {0: "Blue", 1: "Red"}
PAGE_SIZE = 500
MAX_TRACKS = 5000
MAX_NAME_LENGTH = 100
MAX_FILENAME_LENGTH = 255
# Trackmania formatting: "$$" is a dollar sign; "$" followed by three hex digits is a colour,
# "$l[...]"/"$h[...]" a link, and "$" followed by one other character a style.
FORMATTING = re.compile(r"\$(\$|[0-9a-fA-F]{3}|[lLhHpP](?:\[[^\]]*\])?|.?)")
UID = re.compile(r"[A-Za-z0-9_-]{1,40}")


class ChallengeConnection(ConnectionSettings):
    port: int = Field(
        default=5000,
        title="XML-RPC port",
        description="xmlrpc_port in the dedicated server's configuration; "
        "xmlrpc_allowremote must allow LANPilot.",
        ge=1,
        le=65535,
    )
    login: Literal["Admin", "SuperAdmin"] = Field(
        default="Admin",
        title="Login",
        description="Admin can change tracks and restart; SuperAdmin could also stop the server.",
    )
    password: SecretStr = Field(
        title="Password",
        description="The password of that login in the <authorization_levels> of the "
        "dedicated server's configuration.",
    )


def plain(text: str) -> str:
    """A Trackmania name without its formatting codes."""
    return FORMATTING.sub(lambda match: "$" if match.group(1) == "$" else "", text).strip()


class ChallengeAdapter(GameAdapter):
    """GBXRemote 2 with "challenge" methods. Not registered itself."""

    family = "trackmania"
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.MAP_CHANGE,
            Capability.RESTART_SOFT,
            Capability.RESTART_HARD,
            Capability.MANUAL_MAPS,
            Capability.PLAYERS,
            Capability.KICK,
            Capability.BAN,
        }
    )
    connection_schema = ChallengeConnection
    #: Whether the server answers status methods only after a login (TMF does, Sunrise not).
    status_needs_login: ClassVar[bool] = True
    #: The method that restarts the round on the same track.
    soft_restart_method: ClassVar[str] = "RestartChallenge"
    #: A method that tells whether the server can run a track file, if the game has one.
    check_method: ClassVar[str | None] = None
    #: Extra parameters of GetPlayerList (TMF: the struct version) and whether Kick and Ban
    #: take a message for the player (Sunrise: no; a second parameter is a fault there).
    player_list_params: ClassVar[tuple[int, ...]] = ()
    message_parameter: ClassVar[bool] = False

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, ChallengeConnection)  # noqa: S101 - see base class
        self._client = GbxRemote(
            connection.host,
            connection.port,
            connection.login,
            connection.password.get_secret_value(),
        )
        # Without a login where the game allows it: a wrong password then shows up as a
        # control problem, not as "offline".
        self._status_client = (
            self._client
            if self.status_needs_login
            else GbxRemote(connection.host, connection.port, None, "")
        )

    async def close(self) -> None:
        await self._client.close()
        if self._status_client is not self._client:
            await self._status_client.close()

    async def _call(self, method: str, *params: Any) -> Any:
        try:
            return await self._client.call(method, *params)
        except GbxFaultError as fault:
            raise _translate(fault) from fault

    async def query(self) -> ServerStatus:
        try:
            results = await self._status_client.multicall(
                [
                    ("GetServerName", []),
                    ("GetCurrentChallengeInfo", []),
                    ("GetPlayerList", [255, 0]),
                    ("GetMaxPlayers", []),
                    ("GetGameMode", []),
                    ("GetServerPassword", []),
                ]
            )
        except GbxFaultError as fault:
            raise _translate(fault) from fault
        name, challenge, players, max_players, mode, password = results
        for result in (name, players, max_players):
            if isinstance(result, GbxFaultError):
                raise _translate(result)
        if isinstance(challenge, GbxFaultError) and challenge.text != NOTHING_LOADED:
            raise _translate(challenge)
        if not isinstance(players, list) or not isinstance(max_players, dict):
            raise ProtocolError("The server's status answer has an unexpected format.")
        return ServerStatus(
            name=plain(str(name))[:MAX_NAME_LENGTH],
            players=len(players),
            max_players=_int(max_players.get("CurrentValue")),
            # None for a moment while a track is loaded.
            map=to_ref(challenge) if isinstance(challenge, Mapping) else None,
            game_type=GAME_MODES.get(mode) if isinstance(mode, int) else None,
            password_protected=password_flag(password),
        )

    async def list_maps(self) -> Sequence[MapRef]:
        tracks: list[MapRef] = []
        start = 0  # counts what the server sent, usable or not
        while start < MAX_TRACKS:
            page = await self._call("GetChallengeList", PAGE_SIZE, start)
            if not isinstance(page, list):
                raise ProtocolError("The server's track list has an unexpected format.")
            tracks += [ref for item in page if (ref := to_ref(item)) is not None]
            start += len(page)
            if len(page) < PAGE_SIZE:
                break
        return tracks

    async def look_up_map(self, key: str) -> MapRef:
        """A track file the server has, whether in the playlist or not."""
        if not valid_filename(key):
            raise CommandRejectedError(
                "Enter the track's file name relative to the server's Tracks directory, "
                "e.g. Challenges/My Challenges/Fun.Challenge.Gbx."
            )
        if self.check_method is not None:
            # GetChallengeInfo also knows tracks the server cannot run (e.g. environments
            # its pack mask does not allow); this check refuses those with the reason.
            try:
                await self._client.call(self.check_method, key)
            except GbxFaultError as fault:
                if fault.text in (TRACK_UNKNOWN, CHALLENGE_UNKNOWN):
                    raise _translate(fault) from fault
                raise CommandRejectedError(f"The server cannot run this track: {fault}") from fault
        found = to_ref(await self._call("GetChallengeInfo", key))
        if found is None:
            raise ProtocolError("The server's answer about this track has an unexpected format.")
        return found

    async def change_map(self, target: MapRef) -> None:
        await self._load(target.key)

    async def restart(self, mode: RestartMode) -> None:
        if mode is RestartMode.SOFT:
            # The round starts over on the same track; it is not loaded again.
            await self._call(self.soft_restart_method)
            return
        current = to_ref(await self._call("GetCurrentChallengeInfo"))
        if current is None:
            raise ProtocolError("The server did not report which track is running.")
        await self._load(current.key)

    async def list_players(self) -> Sequence[Player]:
        players, ranking = await self._client.multicall(
            [
                ("GetPlayerList", [MAX_PLAYERS, 0, *self.player_list_params]),
                ("GetCurrentRanking", [MAX_PLAYERS, 0]),
            ]
        )
        if isinstance(players, GbxFaultError):
            raise _translate(players)
        if not isinstance(players, list):
            raise ProtocolError("The server's player list has an unexpected format.")
        scores: dict[str, int] = {}
        if isinstance(ranking, list):  # in team mode the ranking lists the teams
            for entry in ranking:
                if isinstance(entry, Mapping) and isinstance(entry.get("Score"), int):
                    scores[str(entry.get("Login"))] = entry["Score"]
        return [player for item in players if (player := to_player(item, scores)) is not None]

    async def kick(self, player: Player, reason: str = "") -> None:
        await self._remove("Kick", player, reason)

    async def ban(self, player: Player, reason: str = "") -> None:
        await self._remove("Ban", player, reason)

    async def _remove(self, method: str, player: Player, reason: str) -> None:
        if not valid_login(player.id):
            raise CommandRejectedError("This player cannot be removed.")
        params = [player.id, reason] if self.message_parameter and reason else [player.id]
        await self._call(method, *params)

    async def _load(self, filename: str) -> None:
        if not valid_filename(filename):
            raise CommandRejectedError("This track name cannot be sent to the server.")
        # ChooseNextChallenge moves the track behind the current one in the playlist;
        # these games have no way to jump without changing the order.
        try:
            await self._client.call("ChooseNextChallenge", filename)
        except GbxFaultError as fault:
            if fault.text != NOT_IN_SELECTION:
                raise _translate(fault) from fault
            # A track added by hand: put it into the playlist, right behind the current one.
            await self._call("InsertChallenge", filename)
            await self._call("ChooseNextChallenge", filename)
        await self._call("NextChallenge")


def to_player(item: Any, scores: Mapping[str, int]) -> Player | None:
    """A player from a PlayerInfo struct; ``None`` if it cannot be used."""
    if not isinstance(item, Mapping):
        return None
    login, nickname, team = item.get("Login"), item.get("NickName"), item.get("TeamId")
    if not isinstance(login, str) or not valid_login(login):
        return None
    name = plain(nickname)[:MAX_NAME_LENGTH] if isinstance(nickname, str) else ""
    return Player(
        id=login,
        name=name or login,
        score=scores.get(login),
        team=TEAMS.get(team) if isinstance(team, int) else None,
    )


def valid_login(login: str) -> bool:
    # LAN logins look like "name/10.0.0.5:2350"; they travel as XML-RPC strings.
    return 0 < len(login) <= 64 and login.isprintable() and not any(c.isspace() for c in login)


def valid_filename(filename: str) -> bool:
    return (
        0 < len(filename) <= MAX_FILENAME_LENGTH
        and filename.isprintable()
        and ".." not in filename.replace("\\", "/").split("/")
        and filename.lower().endswith(".gbx")
    )


def to_ref(item: Any) -> MapRef | None:
    """A track from a challenge struct; ``None`` if it cannot be used."""
    if not isinstance(item, Mapping):
        return None
    filename, name = item.get("FileName"), item.get("Name")
    if not isinstance(filename, str) or not valid_filename(filename):
        return None
    extra = {}
    uid, environment = item.get("UId"), item.get("Environnement")
    if isinstance(uid, str) and UID.fullmatch(uid):
        extra["uid"] = uid
    if isinstance(environment, str) and environment.isalnum():
        extra["environment"] = environment[:30]
    label = plain(name)[:MAX_NAME_LENGTH] if isinstance(name, str) else ""
    return MapRef(filename, label or None, None, extra)


def password_flag(answer: object) -> bool | None:
    """Whether a join password is set, from GetServerPassword. Clients that may not read the
    password get a sentence instead; an administrator gets the password, which is not kept."""
    if not isinstance(answer, str):
        return None
    if answer == NO_PASSWORD:
        return False
    if answer == PASSWORD_REQUIRED:
        return True
    return bool(answer)


def _int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _translate(fault: GbxFaultError) -> Exception:
    if fault.text in (TRACK_UNKNOWN, CHALLENGE_UNKNOWN):
        return CommandRejectedError("The server does not have this track.")
    if fault.text == CHANGE_IN_PROGRESS:
        return CommandRejectedError(
            "The server is still loading a track and ignored this. Try again in a few seconds."
        )
    if fault.text == LOGIN_UNKNOWN:
        return CommandRejectedError("This player is no longer on the server.")
    if fault.text == PERMISSION_DENIED:
        return AuthenticationError("The login is not allowed to do this; use the Admin login.")
    return fault
