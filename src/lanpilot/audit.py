"""Audit log: who did what, from where, and whether it worked."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot.db.models import AuditEntry, User

CLI_ACTOR = "(command line)"


def record(
    db: AsyncSession,
    *,
    action: str,
    actor: User | None = None,
    actor_id: int | None = None,
    actor_name: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: Mapping[str, object] | None = None,
    ip: str | None = None,
    success: bool = True,
) -> None:
    """Add an entry to the caller's transaction. Never pass secrets in ``detail``."""
    name = actor.username if actor is not None else (actor_name or "(anonymous)")
    db.add(
        AuditEntry(
            actor_id=actor.id if actor is not None else actor_id,
            actor_name=name[:64],
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id)[:64],
            detail=json.dumps(dict(detail or {}), sort_keys=True),
            ip=ip[:45] if ip else None,
            success=success,
        )
    )
