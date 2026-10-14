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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot import audit
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
from lanpilot.core.runtime import INTERNAL_ERROR, ServerManager

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_SECONDS = 15.0
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
            await record({"error": message}, success=False)
            return ActionError(message, status=status)

        manager = self._manager
        if not actor.role.allows(required_role):
            raise await refused(f"This needs the {required_role.value} role.", 403)
        state = manager.state(server_id)
        if state is None:
            raise ActionError("This server does not exist.", status=404)
        if not state.enabled:
            raise await refused("This server is disabled.", 409)
        if not state.usable:
            raise await refused(state.error or "This server is not set up correctly.", 409)
        if not state.can(capability):
            raise await refused(f"{state.adapter_name} does not support this.", 400)

        try:
            async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
                message = await manager.call(server_id, lambda adapter: operation(adapter, db))
        except TimeoutError:
            seconds = f"{ACTION_TIMEOUT_SECONDS:.0f}"
            raise await refused(
                f"The server did not answer within {seconds} seconds.", 504
            ) from None
        except AdapterError as exc:
            raise await refused(str(exc) or type(exc).__name__, 502) from exc
        except LookupError:
            raise ActionError("This server does not exist.", status=404) from None
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
                raise ActionError("Pick a map from this server's list.", status=400)
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
            raise ActionError("Enter the map's file name as the server knows it.", status=400)
        async with self._sessionmaker() as db:
            if await maps.find_map(db, server_id, key) is not None:
                raise ActionError("This map is already in the list.", status=409)

            async def operation(adapter: GameAdapter, db: AsyncSession) -> str:
                found = await adapter.look_up_map(key)
                try:
                    row = await maps.add_manual(db, server_id, found, adapter.id)
                except ValueError as exc:
                    raise CommandRejectedError(str(exc)) from None
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
            raise ActionError("This server does not exist.", status=404)
        if not state.usable or not state.can(Capability.PLAYERS):
            raise ActionError(f"{state.adapter_name} cannot list players.", status=400)
        try:
            async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
                reported = await self._manager.call(
                    server_id, lambda adapter: adapter.list_players()
                )
        except TimeoutError:
            raise ActionError("The server did not answer in time.", status=504) from None
        except AdapterError as exc:
            raise ActionError(str(exc) or type(exc).__name__, status=502) from exc
        except LookupError:
            raise ActionError("This server does not exist.", status=404) from None
        return clean_players(reported)

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
                raise CommandRejectedError("This player is no longer on the server.")
            detail["player"] = target.name
            if not (target.can_ban if ban else target.can_kick):
                verb = "banned" if ban else "kicked"
                raise CommandRejectedError(f"{target.name} cannot be {verb} on this server.")
            if ban:
                await adapter.ban(target, reason)
                return f"{target.name} was banned."
            await adapter.kick(target, reason)
            return f"{target.name} was kicked."

        async with self._sessionmaker() as db:
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

    async def console(self, actor: Actor, server_id: int, command: str) -> str:
        """Run a raw console command and return what the server answered. Administrators
        only, and only when the console is switched on in the configuration."""
        if not self._console_enabled:
            raise ActionError(
                "The console is switched off (enable_console in the configuration).", status=403
            )
        command = command.strip()
        if not command or len(command) > MAX_COMMAND_LENGTH or not command.isprintable():
            raise ActionError("Enter one line of at most 500 printable characters.", status=400)

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
                detail={"command": command[:200]},
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
