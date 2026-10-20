"""Registry of game adapters.

Adapters are found through the entry point group ``lanpilot.adapters``. The adapters that
ship with LANPilot are registered the same way as third-party ones.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points

from lanpilot.core.adapters.base import GameAdapter, validate_adapter_class

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "lanpilot.adapters"
DEMO_ADAPTER_ID = "demo"


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, type[GameAdapter]] = {}

    def register(self, adapter: type[GameAdapter]) -> None:
        if not (isinstance(adapter, type) and issubclass(adapter, GameAdapter)):
            raise TypeError(f"{adapter!r} is not a GameAdapter subclass")
        validate_adapter_class(adapter)
        existing = self._adapters.get(adapter.id)
        if existing is not None and existing is not adapter:
            raise ValueError(
                f"adapter id {adapter.id!r} is already used by "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        self._adapters[adapter.id] = adapter

    def load_entry_points(self, points: Iterable[EntryPoint] | None = None) -> None:
        """Register every installed adapter. A broken plugin is logged and skipped."""
        if points is None:
            points = entry_points(group=ENTRY_POINT_GROUP)
        for point in sorted(points, key=lambda point: point.name):
            try:
                self.register(point.load())
            except Exception:
                # One faulty third-party package must not keep LANPilot from starting.
                logger.exception("could not load game adapter %r (%s)", point.name, point.value)

    def get(self, adapter_id: str) -> type[GameAdapter] | None:
        return self._adapters.get(adapter_id)

    def all(self, *, include_demo: bool = True) -> list[type[GameAdapter]]:
        """Adapters sorted by display name."""
        adapters = [
            adapter
            for adapter in self._adapters.values()
            if include_demo or adapter.id != DEMO_ADAPTER_ID
        ]
        return sorted(adapters, key=lambda adapter: adapter.name.lower())

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


def load_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.load_entry_points()
    return registry
