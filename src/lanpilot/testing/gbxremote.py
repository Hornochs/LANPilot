"""Simulator of the GBXRemote interface of TrackMania Forever and Sunrise servers, for tests.

It answers the way the measured servers did (docs/games/trackmania.md), TrackMania Forever by
default and TrackMania Sunrise with ``game="sunrise"``. Forever: nothing works before
``Authenticate``, a wrong password is the fault "Password incorrect.",
``ChooseNextChallenge`` moves the chosen track behind the current one, ``NextChallenge`` and
``RestartChallenge`` answer "Change in progress." while ``loading`` is set, ``system.multicall``
returns ``[result]`` or a fault struct per call. Sunrise: status methods answer without a
login, the round restart is ``ChallengeRestart``, ``GetCurrentChallengeInfo`` fails with "No
challenge currently loaded." while ``loading`` is set, and there is no
``CheckChallengeForCurrentServerParams``.

Players (``GetPlayerList``, ``GetCurrentRanking``, ``Kick``, ``Ban``) follow the servers'
method lists and the faults Sunrise gave for unknown logins; no player was on the test
servers. Sunrise's methods take no message and no struct version ("Too many items in
array").

    async with GbxRemoteSimulator(passwords={"Admin": "secret"}) as server:
        adapter = TrackmaniaForeverAdapter(
            TrackmaniaForeverConnection(host="127.0.0.1", port=server.port, password="secret")
        )
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import struct
import xmlrpc.client
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Literal

# "Anyone" is a connection without login.
LEVELS = ("Anyone", "User", "Admin", "SuperAdmin")
GAME_MODES = {0: "Rounds", 1: "TimeAttack", 2: "Team", 3: "Laps", 4: "Stunts", 5: "Cup"}


def track(name: str, environment: str = "Stadium", author: str = "Nadeo") -> dict[str, Any]:
    folder = "Campaigns/Nations/Green"
    return {
        "UId": hashlib.sha1(name.encode()).hexdigest()[:27],  # noqa: S324 - not security
        "Name": name,
        "FileName": f"{folder}/{name}.Challenge.Gbx",
        "Environnement": environment,
        "Author": author,
        "GoldTime": 30000,
        "CopperPrice": 1000,
    }


DEFAULT_TRACKS = tuple(
    track(name) for name in ("B11-Race", "B12-Race", "B03-Race", "$fffB05$o-Race", "A01-Race")
)


class GbxRemoteSimulator:
    def __init__(
        self,
        *,
        passwords: Mapping[str, str] | None = None,
        tracks: tuple[dict[str, Any], ...] = DEFAULT_TRACKS,
        server_name: str = "$o$f00Simulated$z TMNF",
        players: int = 3,
        max_players: int = 32,
        game_mode: int = 1,
        join_password: str = "",
        disk_tracks: tuple[dict[str, Any], ...] = (),
        game: Literal["forever", "sunrise"] = "forever",
    ) -> None:
        self.game = game
        self.passwords = dict(passwords or {"Admin": "secret"})
        self.tracks = [dict(item) for item in tracks]
        #: Track files on the server's disk that are not in the playlist.
        self.disk_tracks = [dict(item) for item in disk_tracks]
        #: Environments the server's pack mask allows (TMNF: Stadium only).
        self.environments = {"Stadium"}
        self.current = 0
        self.next_index = 1 % len(self.tracks)
        self.server_name = server_name
        #: Players on the server: login, nickname, team, score.
        self.roster: list[dict[str, Any]] = [
            {"Login": f"player{n}", "NickName": f"$f00Player {n}", "TeamId": -1, "Score": 10 * n}
            for n in range(players)
        ]
        #: Kicks and bans as (method, login, message) tuples.
        self.removed: list[tuple[str, str, str]] = []
        self.max_players = max_players
        self.game_mode = game_mode
        self.join_password = join_password
        #: Methods called after a successful login, in order (multicall entries included).
        self.calls: list[str] = []
        self._caller = "Anyone"
        self.bad_passwords = 0
        self.connections = 0
        self.restarts: list[str] = []
        #: Answer nothing at all, like a server that hangs.
        self.silent = False
        #: Send this many callback frames before every answer.
        self.callbacks_before_answer = 0
        #: Refuse NextChallenge and RestartChallenge, as while a track is being loaded.
        self.loading = False
        #: Greeting sent on connect.
        self.greeting = b"GBXRemote 2"
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for writer in list(self._writers):
            writer.close()

    def drop_connections(self) -> None:
        """Close every open connection, as a restarting server would."""
        for writer in list(self._writers):
            writer.close()

    async def __aenter__(self) -> GbxRemoteSimulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def current_track(self) -> dict[str, Any]:
        return self.tracks[self.current]

    # --- protocol ----------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        level: str | None = None
        try:
            writer.write(struct.pack("<I", len(self.greeting)) + self.greeting)
            while True:
                size, handle = struct.unpack("<II", await reader.readexactly(8))
                params, method = xmlrpc.client.loads(await reader.readexactly(size))
                if self.silent:
                    continue
                if method == "Authenticate":
                    login, password = (str(value) for value in params)
                    if self.passwords.get(login) == password:
                        level = login
                        answer: Any = (True,)
                    else:
                        self.bad_passwords += 1
                        answer = xmlrpc.client.Fault(-1000, "Password incorrect.")
                else:
                    answer = self._dispatch(level, method or "", params)
                for number in range(self.callbacks_before_answer):
                    note = xmlrpc.client.dumps((number,), "TrackMania.Echo").encode()
                    writer.write(struct.pack("<II", len(note), 7) + note)
                data = xmlrpc.client.dumps(answer, methodresponse=True, allow_none=False)
                body = data.encode()
                writer.write(struct.pack("<II", len(body), handle) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _dispatch(
        self, level: str | None, method: str, params: tuple[Any, ...]
    ) -> tuple[Any] | xmlrpc.client.Fault:
        level = level or "Anyone"
        if method == "system.multicall":
            results: list[Any] = []
            for entry in params[0]:
                single = self._dispatch(level, entry["methodName"], tuple(entry["params"]))
                if isinstance(single, xmlrpc.client.Fault):
                    results.append(
                        {"faultCode": single.faultCode, "faultString": single.faultString}
                    )
                else:
                    results.append([single[0]])
            return (results,)
        handler = self._methods().get(method)
        if handler is None:
            return xmlrpc.client.Fault(-506, f"Method '{method}' not defined")
        needed, function = handler
        if LEVELS.index(level) < LEVELS.index(needed):
            return xmlrpc.client.Fault(-1000, "Permission denied.")
        self.calls.append(method)
        self._caller = level
        result = function(*params)
        return result if isinstance(result, xmlrpc.client.Fault) else (result,)

    def _methods(self) -> dict[str, tuple[str, Callable[..., Any]]]:
        sunrise = self.game == "sunrise"
        # Sunrise answers its status methods without a login; Forever wants one.
        read = "Anyone" if sunrise else "User"
        methods: dict[str, tuple[str, Callable[..., Any]]] = {
            "GetVersion": (read, lambda: {"Name": "TmSunrise" if sunrise else "TmForever"}),
            "GetServerName": (read, lambda: self.server_name),
            "GetCurrentChallengeInfo": (read, self._current_info),
            "GetCurrentChallengeIndex": (read, lambda: self.current),
            "GetNextChallengeIndex": (read, lambda: self.next_index),
            "GetChallengeList": (read, self._challenge_list),
            "GetPlayerList": (read, self._player_list),
            "GetMaxPlayers": (read, lambda: {"CurrentValue": self.max_players}),
            "GetGameMode": (read, lambda: self.game_mode),
            "GetServerPassword": (read, self._password),
            "GetCurrentRanking": (read, self._ranking),
            "Kick": ("Admin", lambda *params: self._remove("Kick", *params)),
            "Ban": ("Admin", lambda *params: self._remove("Ban", *params)),
            "GetNetworkStats": ("SuperAdmin", lambda: {"Uptime": 21}),
            "GetChallengeInfo": (read, self._challenge_info),
            "InsertChallenge": ("Admin", self._insert),
            "ChooseNextChallenge": ("Admin", self._choose),
            "NextChallenge": ("Admin", self._next),
            "ChallengeRestart" if sunrise else "RestartChallenge": ("Admin", self._restart),
        }
        if not sunrise:
            methods["CheckChallengeForCurrentServerParams"] = ("User", self._check)
        return methods

    def _password(self) -> str:
        # Below Admin the server only says whether there is one.
        if LEVELS.index(self._caller) >= LEVELS.index("Admin"):
            return self.join_password
        return "Password required" if self.join_password else "No password"

    def _current_info(self) -> dict[str, Any] | xmlrpc.client.Fault:
        if self.loading and self.game == "sunrise":
            return xmlrpc.client.Fault(-1000, "No challenge currently loaded.")
        return dict(self.current_track)

    def _challenge_list(self, count: int, start: int) -> list[dict[str, Any]]:
        return [
            {
                key: item[key]
                for key in ("UId", "Name", "FileName", "Environnement", "Author")
                if key in item
            }
            for item in self.tracks[start : start + count]
        ]

    def _player_list(
        self, count: int, start: int, *version: int
    ) -> list[dict[str, Any]] | xmlrpc.client.Fault:
        if self.game == "sunrise" and version:
            return xmlrpc.client.Fault(-502, "Too many items in array")
        players = []
        for number, entry in enumerate(self.roster):
            player = {
                "Login": entry["Login"],
                "NickName": entry["NickName"],
                "PlayerId": number + 1,
                "TeamId": entry["TeamId"],
                "LadderRanking": 0,
            }
            if self.game == "sunrise":
                player.update(IsSpectator=False, IsInOfficialMode=False)
            else:
                player.update(SpectatorStatus=0, Flags=0)
            players.append(player)
        return players[start : start + count]

    def _ranking(self, count: int, start: int) -> list[dict[str, Any]]:
        ranking = sorted(self.roster, key=lambda entry: -entry["Score"])
        return [
            {
                "Login": entry["Login"],
                "NickName": entry["NickName"],
                "PlayerId": number + 1,
                "Rank": number + 1,
                "BestTime": -1,
                "Score": entry["Score"],
                "NbrLapsFinished": 0,
            }
            for number, entry in enumerate(ranking)
        ][start : start + count]

    def _remove(self, method: str, login: str, *message: str) -> bool | xmlrpc.client.Fault:
        if self.game == "sunrise" and message:
            return xmlrpc.client.Fault(-502, "Too many items in array")
        entry = next((entry for entry in self.roster if entry["Login"] == login), None)
        if entry is None:
            return xmlrpc.client.Fault(-1000, "Login unknown.")
        self.roster.remove(entry)
        self.removed.append((method, login, message[0] if message else ""))
        return True

    def _choose(self, filename: str) -> bool | xmlrpc.client.Fault:
        position = next(
            (i for i, item in enumerate(self.tracks) if item["FileName"] == filename), None
        )
        if position is None:
            if self._on_disk(filename) is not None:
                return xmlrpc.client.Fault(-1000, "Challenge not in the selection.")
            unknown = "Challenge unknown." if self.game == "sunrise" else "Track unknown."
            return xmlrpc.client.Fault(-1000, unknown)
        if position != self.current:
            # Like the real server: the chosen track moves right behind the current one.
            chosen = self.tracks.pop(position)
            if position < self.current:
                self.current -= 1
            self.tracks.insert(self.current + 1, chosen)
            self.next_index = self.current + 1
        else:
            self.next_index = self.current
        return True

    def _on_disk(self, filename: str) -> dict[str, Any] | None:
        return next((item for item in self.disk_tracks if item["FileName"] == filename), None)

    def _challenge_info(self, filename: str) -> dict[str, Any] | xmlrpc.client.Fault:
        for item in [*self.tracks, *self.disk_tracks]:
            if item["FileName"] == filename:
                return dict(item)
        return xmlrpc.client.Fault(-1000, "Challenge unknown.")

    def _check(self, filename: str) -> bool | xmlrpc.client.Fault:
        found = self._challenge_info(filename)
        if isinstance(found, xmlrpc.client.Fault):
            return found
        if found["Environnement"] not in self.environments:
            return xmlrpc.client.Fault(-1000, "The track doesn't match the server packmask.")
        return True

    def _insert(self, filename: str) -> bool | xmlrpc.client.Fault:
        if any(item["FileName"] == filename for item in self.tracks):
            return xmlrpc.client.Fault(-1000, "Challenge already added.")
        found = self._on_disk(filename)
        if found is None:
            return xmlrpc.client.Fault(-1000, "Challenge unknown.")
        if found["Environnement"] not in self.environments:
            return xmlrpc.client.Fault(-1000, "The track doesn't match the server packmask.")
        # Like the real server: inserted right behind the current track, which comes next.
        self.tracks.insert(self.current + 1, dict(found))
        self.next_index = self.current + 1
        return True

    def _next(self) -> bool | xmlrpc.client.Fault:
        if self.loading:
            if self.game == "sunrise":
                return True  # Sunrise says yes, and nothing happens
            return xmlrpc.client.Fault(-1000, "Change in progress.")
        if self.next_index == self.current:
            self.restarts.append("hard")
        self.current = self.next_index
        self.next_index = (self.current + 1) % len(self.tracks)
        return True

    def _restart(self) -> bool | xmlrpc.client.Fault:
        if self.loading:
            if self.game == "sunrise":
                return True
            return xmlrpc.client.Fault(-1000, "Change in progress.")
        self.restarts.append("soft")
        return True


__all__ = ["GAME_MODES", "GbxRemoteSimulator", "track"]
