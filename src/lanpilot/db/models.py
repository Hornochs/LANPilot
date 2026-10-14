"""ORM models. Every table added here needs a migration in ``migrations/versions``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lanpilot.db.base import Base, UTCDateTime, utcnow


class AppSetting(Base):
    """Key/value settings that administrators change at runtime."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # a lanpilot.auth.roles.Role value
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class UserSession(Base):
    """A browser session. Only a hash of the cookie value is stored."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)

    user: Mapped[User] = relationship(lazy="joined")


class AuditEntry(Base):
    """Append-only record of logins and of everything that changes state."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # The name is copied so the entry stays readable after the user is deleted.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_name: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), default=None)
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    detail: Mapped[str] = mapped_column(Text, default="{}")  # JSON object
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    success: Mapped[bool] = mapped_column(default=True)


class Server(Base):
    """A game server LANPilot monitors and controls."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    adapter_id: Mapped[str] = mapped_column(String(32))
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int]
    # Non-secret connection settings besides host and port, as a JSON object.
    settings: Mapped[str] = mapped_column(Text, default="{}")
    # Secret connection settings, encrypted by lanpilot.core.secrets.SecretBox and bound to
    # adapter_id, host and port. None if the adapter needs no secrets.
    credentials: Mapped[str | None] = mapped_column(Text, default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ServerMap(Base):
    """A map a server can switch to. Map changes are only accepted for rows of this table."""

    __tablename__ = "server_maps"
    __table_args__ = (UniqueConstraint("server_id", "key", "game_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200))  # what is sent to the server
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    game_type: Mapped[str] = mapped_column(String(100), default="")  # "" if not applicable
    # "protocol" (reported by the server), "catalog" (bundled stock maps) or "manual".
    source: Mapped[str] = mapped_column(String(16), default="protocol")
    extra: Mapped[str] = mapped_column(Text, default="{}")  # JSON: uid, workshop_id, ...
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def label(self) -> str:
        return self.name or self.key


class MapImage(Base):
    """Preview image of a map, shared by all servers of the same game."""

    __tablename__ = "map_images"
    __table_args__ = (UniqueConstraint("game", "map_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game: Mapped[str] = mapped_column(String(32))  # adapter id
    map_key: Mapped[str] = mapped_column(String(200))  # lower-case
    # File in the image cache directory; the name is random and chosen by LANPilot.
    filename: Mapped[str] = mapped_column(String(64), unique=True)
    provider: Mapped[str] = mapped_column(String(32))  # "upload" or an online source
    source_url: Mapped[str | None] = mapped_column(String(500), default=None)
    width: Mapped[int]
    height: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MapImageMiss(Base):
    """A map for which no online image is wanted right now: the sources had none, or an
    administrator removed the one they had. Lookups skip it until ``checked_at`` is old."""

    __tablename__ = "map_image_misses"
    __table_args__ = (UniqueConstraint("game", "map_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game: Mapped[str] = mapped_column(String(32))  # adapter id
    map_key: Mapped[str] = mapped_column(String(200))  # lower-case
    reason: Mapped[str] = mapped_column(String(16))  # "not_found" or "removed"
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
