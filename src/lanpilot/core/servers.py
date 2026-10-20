"""Configured game servers: validation, storage of credentials, connection objects.

Functions change the session but do not commit, so callers can add an audit entry to the
same transaction. They raise ``ServerError`` before changing anything.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot import i18n
from lanpilot.core import secrets
from lanpilot.core.adapters import (
    AdapterError,
    Capability,
    ConnectionSettings,
    GameAdapter,
)
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.adapters.schema import (
    FieldSpec,
    describe_errors,
    describe_fields,
    split_values,
)
from lanpilot.core.redaction import redactor
from lanpilot.core.secrets import SecretBox, SecretError, server_context
from lanpilot.db.models import Server
from lanpilot.i18n import M

NAME_MAX_LENGTH = 100
PROBE_TIMEOUT_SECONDS = 8.0
ADDRESS_CHANGED = M("The address changed, so this has to be entered again.")


class ServerError(Exception):
    """Invalid input. ``message`` and ``fields`` are safe to show; ``fields`` maps a
    connection field name to its problem."""

    def __init__(self, message: str = "", fields: Mapping[str, str] | None = None) -> None:
        super().__init__(message or M("Please correct the marked fields."))
        self.fields = dict(fields or {})


@dataclass(frozen=True)
class ServerInput:
    """What an administrator entered. ``values`` holds the raw connection form values; a
    checkbox that is not ticked is simply absent."""

    name: str
    values: Mapping[str, str]
    enabled: bool = True


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a connection test, phrased for the administrator."""

    reachable: bool
    lines: Sequence[str] = field(default_factory=tuple)
    # None if the game has nothing that would prove remote control works.
    remote_control: bool | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.remote_control is not False


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise ServerError(M("Give the server a name."))
    if len(name) > NAME_MAX_LENGTH:
        raise ServerError(
            M(
                "The name must have at most {maximum} characters.",
                maximum=NAME_MAX_LENGTH,
            )
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ServerError(M("The name must not contain control characters."))
    return name


async def _ensure_unique_name(db: AsyncSession, name: str, *, except_id: int | None) -> None:
    query = select(Server.id).where(Server.name == name)
    if except_id is not None:
        query = query.where(Server.id != except_id)
    if (await db.execute(query)).first() is not None:
        raise ServerError(M("A server named {name} already exists.", name=repr(name)))


def _build_connection(
    adapter: type[GameAdapter],
    specs: Sequence[FieldSpec],
    values: Mapping[str, str],
    kept_secrets: Mapping[str, str],
    *,
    address_changed: bool = False,
) -> ConnectionSettings:
    data: dict[str, Any] = {}
    reentry: dict[str, str] = {}
    for spec in specs:
        raw = values.get(spec.name)
        if spec.kind == "checkbox":
            data[spec.name] = raw is not None
        elif spec.kind == "password":
            if raw:
                redactor.add(raw)
                data[spec.name] = raw
            elif spec.name in kept_secrets:
                data[spec.name] = kept_secrets[spec.name]
            elif address_changed and spec.required:
                reentry[spec.name] = ADDRESS_CHANGED
        elif raw is not None and raw.strip():
            data[spec.name] = raw.strip()
    try:
        connection = adapter.connection_schema.model_validate(data)
    except ValidationError as exc:
        raise ServerError(fields={**describe_errors(exc), **reentry}) from exc
    return connection


def _store(
    server: Server, adapter: type[GameAdapter], connection: ConnectionSettings, box: SecretBox
) -> None:
    public, secret = split_values(connection)
    server.host = str(public.pop("host"))
    server.port = int(public.pop("port"))
    server.settings = json.dumps(public, sort_keys=True)
    context = server_context(adapter.id, server.host, server.port)
    server.credentials = box.encrypt(secret, context=context) if secret else None


def _decrypt(server: Server, box: SecretBox) -> dict[str, str]:
    if server.credentials is None:
        return {}
    context = server_context(server.adapter_id, server.host, server.port)
    values = box.decrypt(server.credentials, context=context)
    for value in values.values():
        redactor.add(value)
    return values


async def list_servers(db: AsyncSession) -> Sequence[Server]:
    return (await db.execute(select(Server).order_by(Server.name))).scalars().all()


async def get_server(db: AsyncSession, server_id: int) -> Server | None:
    return await db.get(Server, server_id)


async def create_server(
    db: AsyncSession,
    box: SecretBox,
    adapter: type[GameAdapter],
    data: ServerInput,
) -> Server:
    name = _clean_name(data.name)
    await _ensure_unique_name(db, name, except_id=None)
    connection = _build_connection(
        adapter, describe_fields(adapter.connection_schema), data.values, {}
    )

    server = Server(
        name=name,
        adapter_id=adapter.id,
        enabled=data.enabled,
    )
    _store(server, adapter, connection, box)
    db.add(server)
    await db.flush()
    return server


async def update_server(
    db: AsyncSession,
    box: SecretBox,
    adapter: type[GameAdapter],
    server: Server,
    data: ServerInput,
) -> None:
    """Apply an edit. Password fields left empty keep their stored value, unless the
    address changed: stored passwords are bound to it and must then be entered again."""
    name = _clean_name(data.name)
    await _ensure_unique_name(db, name, except_id=server.id)

    new_host = data.values.get("host", "").strip().lower()
    new_port = data.values.get("port", "").strip()
    address_changed = new_host != server.host.lower() or new_port != str(server.port)
    kept: dict[str, str] = {}
    if not address_changed:
        try:
            kept = _decrypt(server, box)
        except SecretError:
            kept = {}  # undecryptable: every password has to be entered again

    connection = _build_connection(
        adapter,
        describe_fields(adapter.connection_schema),
        data.values,
        kept,
        address_changed=address_changed,
    )
    server.name = name
    server.enabled = data.enabled
    _store(server, adapter, connection, box)


async def delete_server(db: AsyncSession, server: Server) -> None:
    await db.delete(server)


def public_values(server: Server) -> dict[str, Any]:
    """Connection values that may be shown in a form: everything except secrets."""
    values: dict[str, Any] = json.loads(server.settings)
    values.update(host=server.host, port=server.port)
    return values


def load_connection(
    server: Server, adapter: type[GameAdapter], box: SecretBox
) -> ConnectionSettings:
    """Rebuild the validated connection object, with decrypted credentials."""
    try:
        return adapter.connection_schema.model_validate(
            {**public_values(server), **_decrypt(server, box)}
        )
    except ValidationError as exc:
        raise ServerError(
            M("The stored settings no longer fit this game; edit the server and save it again."),
            describe_errors(exc),
        ) from exc


def adapter_for(server: Server, registry: AdapterRegistry) -> type[GameAdapter] | None:
    return registry.get(server.adapter_id)


async def probe(server: Server, adapter: type[GameAdapter], box: SecretBox) -> ProbeResult:
    """Connect once and report what works: status first, then remote control if the game
    has a map list to ask for (which needs the password)."""
    try:
        instance = adapter(load_connection(server, adapter, box))
    except SecretError as exc:
        return ProbeResult(False, (secrets.describe(exc, reenter=False),))
    except ServerError as exc:
        return ProbeResult(False, (i18n.message_of(exc),))

    lines: list[str] = []
    reachable = False
    remote_control: bool | None = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            status = await instance.query()
            reachable = True
            current = status.map.label if status.map else M("unknown map")
            occupancy = M(
                "{players}/{max_players} players",
                players=status.players,
                max_players=status.max_players,
            )
            lines.append(
                M(
                    "Status: “{name}”, {map}, {occupancy}.",
                    name=status.name,
                    map=current,
                    occupancy=occupancy,
                )
            )
            if instance.supports(Capability.MAP_LIST):
                try:
                    maps = await instance.list_maps()
                    remote_control = True
                    lines.append(
                        M("Remote control works: the server lists {count} maps.", count=len(maps))
                    )
                except AdapterError as exc:
                    remote_control = False
                    lines.append(M("Remote control failed: {reason}", reason=i18n.message_of(exc)))
    except TimeoutError:
        lines.append(
            M("No answer within {seconds} seconds.", seconds=f"{PROBE_TIMEOUT_SECONDS:.0f}")
        )
    except AdapterError as exc:
        lines.append(M("Status query failed: {reason}", reason=i18n.message_of(exc)))
    finally:
        await instance.close()
    return ProbeResult(reachable, tuple(lines), remote_control)
