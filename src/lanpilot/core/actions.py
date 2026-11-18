"""The one place where actions on game servers are carried out.

Every action goes through ``Actions._perform``: it checks the role, the server and the
capability, runs the operation serialised with the poll loop, writes the audit entry
whether it worked or not, and asks for a fresh status. Routes do not talk to adapters
themselves.

Actions use a database session of their own, so a failure can be rolled back without
touching the objects the web request has loaded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot import audit, i18n
from lanpilot.auth.roles import Role
from lanpilot.core import maps
from lanpilot.core.adapters import (
    AdapterError,
    Capability,
    CommandRejectedError,
    GameAdapter,
    Player,
    RestartMode,
)
from lanpilot.core.runtime import INTERNAL_ERROR, ServerBusyError, ServerManager
from lanpilot.i18n import M

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_SECONDS = 15.0
# Player lists are shared for this long: every open server page asks for one on each live
# update, and each query takes the server's lock that actions need too.
PLAYER_LIST_MAX_AGE_SECONDS = 3.0
BUSY = M("The server is busy with other requests; try again in a moment.")
MAX_PLAYERS = 256
MAX_PLAYER_TEXT = 64
MAX_REASON_LENGTH = 100
MAX_COMMAND_LENGTH = 500
MAX_CONSOLE_OUTPUT = 64 * 1024

Operation = Callable[[GameAdapter, AsyncSession], Awaitable[str]]


class ActionError(Exception):
    """An action was refused or failed. The message is safe to show; ``status`` is the HTTP
    status that fits."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Actor:
    """Who is acting. Plain values, so it stays valid whatever happens to a session."""

    user_id: int
    username: str
    role: Role
    ip: str | None = None


# Console commands that set a password: only the command's name goes into the audit log,
# e.g. "rcon_password", "sv_password", "SetServerPassword" or "set ... AdminPassword".
SECRET_IN_COMMAND = re.compile(r"pass(word|wd)?|secret|key", re.IGNORECASE)


def audited_command(command: str) -> str:
    """The command as the audit log keeps it: at most 200 characters, and for a command
    that may carry a password only its first word."""
    if SECRET_IN_COMMAND.search(command):
        words = command.split(maxsplit=1)
        return f"{words[0][:64] if words else ''} (arguments not recorded)"
    return command[:200]


class Actions:
    def __init__(
        self,
        manager: ServerManager,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        console_enabled: bool = False,
    ) -> None:
        self._manager = manager
        self._sessionmaker = sessionmaker
        self._console_enabled = console_enabled
        self._player_lists: dict[int, tuple[float, list[Player]]] = {}
        self._player_queries: dict[int, asyncio.Task[list[Player]]] = {}

    @property
    def console_enabled(self) -> bool:
        return self._console_enabled

    async def _perform(
        self,
        actor: Actor,
        server_id: int,
        *,
        action: str,
        required_role: Role,
        capability: Capability,
        detail: Mapping[str, object],
        operation: Operation,
        db: AsyncSession,
    ) -> str:
        """Run ``operation`` and return its success message; raise ``ActionError`` otherwise."""

        async def record(extra: Mapping[str, object], *, success: bool) -> None:
            audit.record(
                db,
                action=action,
                actor_id=actor.user_id,
                actor_name=actor.username,
                target_type="server",
                target_id=server_id,
                detail={**detail, **extra},
                ip=actor.ip,
                success=success,
            )
            await db.commit()

        async def refused(message: str, status: int) -> ActionError:
            await db.rollback()  # drop whatever the operation had begun to change
            await record({"error": str(message)}, success=False)
            return ActionError(message, status=status)

        manager = self._manager
        if not actor.role.allows(required_role):
            raise await refused(M("This needs the {role} role.", role=required_role.value), 403)
        state = manager.state(server_id)
        if state is None:
            raise ActionError(M("This server does not exist."), status=404)
        if not state.enabled:
            raise await refused(M("This server is disabled."), 409)
        if not state.usable:
            raise await refused(state.error or M("This server is not set up correctly."), 409)
        if not state.can(capability):
            raise await refused(
                M("{adapter_name} does not support this.", adapter_name=state.adapter_name), 400
            )

        try:
            message = await manager.call(
                server_id, lambda adapter: operation(adapter, db), seconds=ACTION_TIMEOUT_SECONDS
            )
        except ServerBusyError:
            raise await refused(BUSY, 503) from None
        except TimeoutError:
            seconds = f"{ACTION_TIMEOUT_SECONDS:.0f}"
            raise await refused(
                M("The server did not answer within {seconds} seconds.", seconds=seconds), 504
            ) from None
        except AdapterError as exc:
            raise await refused(i18n.message_of(exc) or type(exc).__name__, 502) from exc
        except LookupError:
            raise ActionError(M("This server does not exist."), status=404) from None
        except Exception:
            logger.exception("action %s on server %s failed unexpectedly", action, server_id)
            raise await refused(INTERNAL_ERROR, 500) from None

        await record({"server": state.name}, success=True)
        manager.set_control_error(server_id, None)  # the password evidently works
        manager.poll_soon(server_id)
        return message

    async def change_map(self, actor: Actor, server_id: int, map_id: int) -> str:
        """Switch to a map from the server's stored list, identified by its row id."""
        async with self._sessionmaker() as db:
            row = await maps.get_map(db, server_id, map_id)
            if row is None:
                # Nothing was sent anywhere, so there is nothing to audit.
                raise ActionError(M("Pick a map from this server's list."), status=400)
            target = maps.to_ref(row)
            label = row.label

            async def operation(adapter: GameAdapter, _db: AsyncSession) -> str:
                await adapter.change_map(target)
                return f"Map changed to {label}."

            return await self._perform(
                actor,
                server_id,
                action="server.change_map",
                required_role=Role.OPERATOR,
                capability=Capability.MAP_CHANGE,
                detail={"map": row.key, "game_type": row.game_type},
                operation=operation,
                db=db,
            )

    async def restart(self, actor: Actor, server_id: int, mode: RestartMode) -> str:
        async def operation(adapter: GameAdapter, _db: AsyncSession) -> str:
            await adapter.restart(mode)
            if mode is RestartMode.SOFT:
                return "The match was restarted."
            return "The level was reloaded."

        soft = mode is RestartMode.SOFT
        async with self._sessionmaker() as db:
            return await self._perform(
                actor,
                server_id,
                action="server.restart",
                required_role=Role.OPERATOR,
                capability=Capability.RESTART_SOFT if soft else Capability.RESTART_HARD,
                detail={"mode": mode.value},
                operation=operation,
                db=db,
            )

    async def add_map(self, actor: Actor, server_id: int, key: str) -> str:
        """Add a map the server has but does not list, after the server confirmed it."""
        key = key.strip()
        if not key or len(key) > maps.MAX_KEY_LENGTH or not key.isprintable():
            raise ActionError(M("Enter the map's file name as the server knows it."), status=400)
        async with self._sessionmaker() as db:
            if await maps.find_map(db, server_id, key) is not None:
                raise ActionError(M("This map is already in the list."), status=409)

            async def operation(adapter: GameAdapter, db: AsyncSession) -> str:
                found = await adapter.look_up_map(key)
                try:
                    row = await maps.add_manual(db, server_id, found, adapter.id)
                except ValueError as exc:
                    raise CommandRejectedError(i18n.message_of(exc)) from None
                return f"{row.label} was added to the map list."

            return await self._perform(
                actor,
                server_id,
                action="server.add_map",
                required_role=Role.ADMIN,
                capability=Capability.MANUAL_MAPS,
                detail={"map": key[:200]},
                operation=operation,
                db=db,
            )

    async def refresh_maps(self, actor: Actor, server_id: int) -> str:
        """Ask the server for its maps again and replace the stored list."""

        async def operation(adapter: GameAdapter, db: AsyncSession) -> str:
            reported = await adapter.list_maps()
            count = await maps.replace_reported(db, server_id, reported, adapter.id)
            return f"The server lists {count} maps."

        async with self._sessionmaker() as db:
            return await self._perform(
                actor,
                server_id,
                action="server.refresh_maps",
                required_role=Role.OPERATOR,
                capability=Capability.MAP_LIST,
                detail={},
                operation=operation,
                db=db,
            )

    # --- players and console (phase 5) ----------------------------------------------------

    async def list_players(self, server_id: int) -> list[Player]:
        """The players on the server right now. Reading only: no role check beyond being
        signed in (done by the route), and no audit entry."""
        state = self._manager.state(server_id)
        if state is None:
            raise ActionError(M("This server does not exist."), status=404)
        if not state.usable or not state.can(Capability.PLAYERS):
            raise ActionError(
                M("{adapter_name} cannot list players.", adapter_name=state.adapter_name),
                status=400,
            )
        cached = self._player_lists.get(server_id)
        if cached is not None and time.monotonic() - cached[0] < PLAYER_LIST_MAX_AGE_SECONDS:
            return list(cached[1])
        # One query per server at a time; everyone who asks meanwhile shares its answer.
        query = self._player_queries.get(server_id)
        if query is None:
            query = asyncio.create_task(self._query_players(server_id))
            self._player_queries[server_id] = query
            query.add_done_callback(lambda done: self._query_finished(server_id, done))
        # Shielded: one caller giving up must not cancel the query for the others.
        return list(await asyncio.shield(query))

    async def _query_players(self, server_id: int) -> list[Player]:
        try:
            reported = await self._manager.call(
                server_id, lambda adapter: adapter.list_players(), seconds=ACTION_TIMEOUT_SECONDS
            )
        except ServerBusyError:
            raise ActionError(BUSY, status=503) from None
        except TimeoutError:
            raise ActionError(M("The server did not answer in time."), status=504) from None
        except AdapterError as exc:
            raise ActionError(i18n.message_of(exc) or type(exc).__name__, status=502) from exc
        except LookupError:
            raise ActionError(M("This server does not exist."), status=404) from None
        players = clean_players(reported)
        self._player_lists[server_id] = (time.monotonic(), players)
        return players

    def _query_finished(self, server_id: int, query: asyncio.Task[list[Player]]) -> None:
        if self._player_queries.get(server_id) is query:
            del self._player_queries[server_id]
        if not query.cancelled():
            query.exception()  # retrieved, even if every caller gave up waiting

    def forget_players(self, server_id: int) -> None:
        """Drop the shared player list, e.g. after a kick, so the next page shows the change."""
        self._player_lists.pop(server_id, None)

    async def kick(self, actor: Actor, server_id: int, player_id: str, reason: str) -> str:
        return await self._remove_player(actor, server_id, player_id, reason, ban=False)

    async def ban(self, actor: Actor, server_id: int, player_id: str, reason: str) -> str:
        return await self._remove_player(actor, server_id, player_id, reason, ban=True)

    async def _remove_player(
        self, actor: Actor, server_id: int, player_id: str, reason: str, *, ban: bool
    ) -> str:
        reason = clean_text(reason, MAX_REASON_LENGTH)
        detail: dict[str, object] = {"player_id": player_id[:MAX_PLAYER_TEXT], "reason": reason}

        async def operation(adapter: GameAdapter, _db: AsyncSession) -> str:
            # Only someone who is on the server right now: ids can be reused later.
            players = clean_players(await adapter.list_players())
            target = next((player for player in players if player.id == player_id), None)
            if target is None:
                raise CommandRejectedError(M("This player is no longer on the server."))
            detail["player"] = target.name
            if not (target.can_ban if ban else target.can_kick):
                text = (
                    M("{name} cannot be banned on this server.", name=target.name)
                    if ban
                    else M("{name} cannot be kicked on this server.", name=target.name)
                )
                raise CommandRejectedError(text)
            if ban:
                await adapter.ban(target, reason)
                return f"{target.name} was banned."
            await adapter.kick(target, reason)
            return f"{target.name} was kicked."

        async with self._sessionmaker() as db:
            try:
                return await self._perform(
                    actor,
                    server_id,
                    action="server.ban" if ban else "server.kick",
                    required_role=Role.ADMIN if ban else Role.OPERATOR,
                    capability=Capability.BAN if ban else Capability.KICK,
                    detail=detail,
                    operation=operation,
                    db=db,
                )
            finally:
                self.forget_players(server_id)

    async def console(self, actor: Actor, server_id: int, command: str) -> str:
        """Run a raw console command and return what the server answered. Administrators
        only, and only when the console is switched on in the configuration."""
        if not self._console_enabled:
            raise ActionError(
                M("The console is switched off (enable_console in the configuration)."), status=403
            )
        command = command.strip()
        if not command or len(command) > MAX_COMMAND_LENGTH or not command.isprintable():
            raise ActionError(M("Enter one line of at most 500 printable characters."), status=400)

        async def operation(adapter: GameAdapter, _db: AsyncSession) -> str:
            answer = await adapter.console(command)
            return answer[:MAX_CONSOLE_OUTPUT]

        async with self._sessionmaker() as db:
            return await self._perform(
                actor,
                server_id,
                action="server.console",
                required_role=Role.ADMIN,
                capability=Capability.CONSOLE,
                detail={"command": audited_command(command)},
                operation=operation,
                db=db,
            )


def clean_text(text: str, limit: int) -> str:
    """One line of printable text, at most ``limit`` characters."""
    kept = "".join(ch if ch.isprintable() else " " if ch.isspace() else "" for ch in text)
    return " ".join(kept.split())[:limit]


def clean_players(reported: Sequence[Player]) -> list[Player]:
    """Players as the server reported them, bounded and with printable names; entries
    without an id are dropped (they could not be kicked)."""
    players: list[Player] = []
    seen: set[str] = set()
    for item in reported[:MAX_PLAYERS]:
        if not item.id or item.id in seen or len(item.id) > MAX_PLAYER_TEXT:
            continue
        seen.add(item.id)
        players.append(
            Player(
                id=item.id,
                name=clean_text(item.name, MAX_PLAYER_TEXT) or "(no name)",
                score=item.score,
                ping=item.ping,
                team=clean_text(item.team, MAX_PLAYER_TEXT) if item.team else None,
                bot=item.bot,
                can_kick=item.can_kick,
                can_ban=item.can_ban,
            )
        )
    return players
