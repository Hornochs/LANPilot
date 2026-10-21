"""Bundled knowledge about stock maps: their display names, per game.

The catalogue lives in ``lanpilot/catalog/*.toml``: one table per adapter id, mapping a map
key to the name users know it by (``de_dust2 = "Dust II"``). It only adds names; which maps
a server has still comes from the server, except for games whose protocol cannot list
maps: their adapters take the stock maps from here (``keys()``).
"""

from __future__ import annotations

import dataclasses
import functools
import re
import tomllib
from collections.abc import Mapping
from importlib import resources

from lanpilot.core.adapters import MapRef

PACKAGE = "lanpilot.catalog"
KEY_PATTERN = re.compile(r"[A-Za-z0-9_./-]{1,200}")
MAX_NAME_LENGTH = 100


class CatalogError(Exception):
    """A bundled catalogue file is malformed; this is a bug in LANPilot."""


class Catalog:
    def __init__(self, names: Mapping[str, Mapping[str, str]]) -> None:
        # adapter id -> lower-case map key -> display name
        self._names = {
            adapter_id: {key.lower(): name for key, name in entries.items()}
            for adapter_id, entries in names.items()
        }

    def name(self, adapter_id: str, map_key: str) -> str | None:
        return self._names.get(adapter_id, {}).get(map_key.lower())

    def named(self, adapter_id: str, ref: MapRef) -> MapRef:
        """``ref`` with the catalogue's name, unless the server already gave one."""
        if ref.name:
            return ref
        name = self.name(adapter_id, ref.key)
        return dataclasses.replace(ref, name=name) if name else ref

    def keys(self, adapter_id: str) -> list[str]:
        """The map keys of a game, for games whose protocol cannot list maps."""
        return sorted(self._names.get(adapter_id, {}))

    def games(self) -> list[str]:
        return sorted(self._names)


def parse(text: str, source: str) -> dict[str, dict[str, str]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"{source}: {exc}") from exc
    names: dict[str, dict[str, str]] = {}
    for adapter_id, entries in data.items():
        if not isinstance(entries, dict):
            raise CatalogError(f"{source}: {adapter_id} must be a table")
        for key, name in entries.items():
            if not KEY_PATTERN.fullmatch(key):
                raise CatalogError(f"{source}: [{adapter_id}] {key!r} is not a map key")
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > MAX_NAME_LENGTH
                or not name.isprintable()
            ):
                raise CatalogError(f"{source}: [{adapter_id}] {key} needs a short printable name")
        names.setdefault(adapter_id, {}).update(entries)
    return names


@functools.cache
def load() -> Catalog:
    """The bundled catalogue. Read once per process."""
    names: dict[str, dict[str, str]] = {}
    for entry in sorted(resources.files(PACKAGE).iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".toml"):
            for adapter_id, entries in parse(entry.read_text("utf-8"), entry.name).items():
                names.setdefault(adapter_id, {}).update(entries)
    return Catalog(names)
