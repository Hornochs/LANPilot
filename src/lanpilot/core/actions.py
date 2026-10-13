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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot import audit
from lanpilot.auth.roles import Role
from lanpilot.core import maps
from lanpilot.core.adapters import AdapterError, Capability, GameAdapter, RestartMode
from lanpilot.core.runtime import INTERNAL_ERROR, ServerManager

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_SECONDS = 15.0

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
        self, manager: ServerManager, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        self._manager = manager
        self._sessionmaker = sessionmaker

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

    async def refresh_maps(self, actor: Actor, server_id: int) -> str:
        """Ask the server for its maps again and replace the stored list."""

        async def operation(adapter: GameAdapter, db: AsyncSession) -> str:
            reported = await adapter.list_maps()
            count = await maps.replace_reported(db, server_id, reported)
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
