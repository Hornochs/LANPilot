"""User roles. Each role includes the rights of the ones below it."""

from __future__ import annotations

from enum import StrEnum

from lanpilot.i18n import M


class Role(StrEnum):
    VIEWER = "viewer"  # see servers
    OPERATOR = "operator"  # change maps, restart, kick
    ADMIN = "admin"  # manage users, servers, credentials; raw console

    @property
    def rank(self) -> int:
        return _RANKS[self]

    def allows(self, required: Role) -> bool:
        return self.rank >= required.rank


_RANKS = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


# How roles are shown to users.
LABELS = {Role.VIEWER: M("Viewer"), Role.OPERATOR: M("Operator"), Role.ADMIN: M("Administrator")}
