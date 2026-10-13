"""Adapter interface and registry. Adapter authors import from here."""

from lanpilot.core.adapters.base import (
    AdapterError,
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
    ServerUnreachableError,
    UnsupportedError,
    validate_adapter_class,
)

__all__ = [
    "AdapterError",
    "AuthenticationError",
    "Capability",
    "CommandRejectedError",
    "ConnectionSettings",
    "DiscoveredServer",
    "GameAdapter",
    "MapRef",
    "Player",
    "ProtocolError",
    "RestartMode",
    "ServerStatus",
    "ServerUnreachableError",
    "UnsupportedError",
    "validate_adapter_class",
]
