"""Server management for administrators.

The form for a server's connection settings is generated from the adapter's connection
schema. Its fields are prefixed with ``c_`` so they cannot collide with LANPilot's own
fields such as ``name``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from lanpilot import audit
from lanpilot.auth.roles import Role
from lanpilot.core import servers
from lanpilot.core.adapters import GameAdapter
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.adapters.schema import FieldSpec, describe_fields
from lanpilot.core.secrets import SecretBox
from lanpilot.db.models import Server
from lanpilot.web.deps import CsrfProtected, DbSession, RequireAdmin, client_ip, require_role
from lanpilot.web.templating import render

FIELD_PREFIX = "c_"

router = APIRouter(prefix="/admin/servers", dependencies=[Depends(require_role(Role.ADMIN))])


def _registry(request: Request) -> AdapterRegistry:
    registry: AdapterRegistry = request.app.state.registry
    return registry


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def _offered_adapters(request: Request) -> list[type[GameAdapter]]:
    """Games a new server can be created for."""
    include_demo: bool = request.app.state.settings.enable_demo_adapter
    return _registry(request).all(include_demo=include_demo)


def _offered_adapter(request: Request, adapter_id: str) -> type[GameAdapter]:
    for adapter in _offered_adapters(request):
        if adapter.id == adapter_id:
            return adapter
    raise HTTPException(status_code=404)


async def _server(db: AsyncSession, server_id: int) -> Server:
    server = await servers.get_server(db, server_id)
    if server is None:
        raise HTTPException(status_code=404)
    return server


def _redirect(request: Request, notice: str) -> Response:
    root = request.app.state.settings.server.root_path
    return RedirectResponse(f"{root}/admin/servers?notice={notice}", status_code=303)


def _input(form: FormData) -> servers.ServerInput:
    name = form.get("name")
    values = {
        key[len(FIELD_PREFIX) :]: value
        for key, value in form.items()
        if key.startswith(FIELD_PREFIX) and isinstance(value, str)
    }
    return servers.ServerInput(
        name=name if isinstance(name, str) else "",
        values=values,
        enabled="enabled" in form,
    )


def _display_values(specs: list[FieldSpec], values: dict[str, Any]) -> dict[str, Any]:
    """Values to put back into the form. Password fields are always rendered empty."""
    shown: dict[str, Any] = {}
    for spec in specs:
        if spec.kind == "password":
            continue
        if spec.kind == "checkbox":
            shown[spec.name] = bool(values.get(spec.name, spec.default))
        else:
            value = values.get(spec.name, spec.default)
            shown[spec.name] = "" if value is None else value
    return shown


def _form_page(
    request: Request,
    adapter: type[GameAdapter],
    *,
    server: Server | None,
    name: str,
    enabled: bool,
    values: dict[str, Any],
    error: servers.ServerError | None = None,
    prefilled: bool = False,
) -> Response:
    specs = describe_fields(adapter.connection_schema)
    context = {
        "adapter": adapter,
        "specs": specs,
        "prefix": FIELD_PREFIX,
        "server": server,
        "name": name,
        "enabled": enabled,
        "values": _display_values(specs, values),
        "error": str(error) if error else None,
        "field_errors": error.fields if error else {},
        "prefilled": prefilled,
    }
    return render(request, "admin/server_form.html", context, status_code=400 if error else 200)


def _resubmitted_values(data: servers.ServerInput, specs: list[FieldSpec]) -> dict[str, Any]:
    return {
        spec.name: (
            spec.name in data.values if spec.kind == "checkbox" else data.values.get(spec.name, "")
        )
        for spec in specs
        if spec.kind != "password"
    }


async def _list_page(
    request: Request,
    db: AsyncSession,
    *,
    probed: Server | None = None,
    probe: servers.ProbeResult | None = None,
) -> Response:
    registry = _registry(request)
    rows = [(server, registry.get(server.adapter_id)) for server in await servers.list_servers(db)]
    context = {
        "rows": rows,
        "can_add": bool(_offered_adapters(request)),
        "probed": probed,
        "probe": probe,
    }
    return render(request, "admin/servers.html", context)


def _audit_detail(server: Server) -> dict[str, object]:
    return {
        "name": server.name,
        "adapter": server.adapter_id,
        "host": server.host,
        "port": server.port,
    }


@router.get("", name="admin_servers")
async def list_servers(request: Request, db: DbSession) -> Response:
    return await _list_page(request, db)


MAX_PREFILL_LENGTH = 253


def _prefill(request: Request, specs: list[FieldSpec]) -> dict[str, Any]:
    """Form values from the query string, as the LAN scan links them: only text, number and
    select fields of the schema; password fields are never filled in from a link."""
    values: dict[str, Any] = {}
    for spec in specs:
        value = request.query_params.get(FIELD_PREFIX + spec.name)
        if value is None or len(value) > MAX_PREFILL_LENGTH or not value.isprintable():
            continue
        if spec.kind in ("text", "number") or (spec.kind == "select" and value in spec.choices):
            values[spec.name] = value
    return values


@router.get("/new", name="admin_server_new")
async def new_server(request: Request, adapter: str | None = None) -> Response:
    if adapter is None:
        return render(request, "admin/server_new.html", {"adapters": _offered_adapters(request)})
    chosen = _offered_adapter(request, adapter)
    name = request.query_params.get("name", "")
    values = _prefill(request, describe_fields(chosen.connection_schema))
    return _form_page(
        request,
        chosen,
        server=None,
        name=name[:100] if name.isprintable() else "",
        enabled=True,
        values=values,
        prefilled=bool(values),
    )


@router.post("/new", dependencies=[CsrfProtected])
async def create_server(request: Request, db: DbSession, auth: RequireAdmin) -> Response:
    form = await request.form()
    adapter_id = form.get("adapter")
    adapter = _offered_adapter(request, adapter_id if isinstance(adapter_id, str) else "")
    data = _input(form)
    try:
        server = await servers.create_server(db, _box(request), adapter, data)
    except servers.ServerError as exc:
        return _form_page(
            request,
            adapter,
            server=None,
            name=data.name,
            enabled=data.enabled,
            values=_resubmitted_values(data, describe_fields(adapter.connection_schema)),
            error=exc,
        )
    audit.record(
        db,
        action="server.create",
        actor=auth.user,
        target_type="server",
        target_id=server.id,
        detail=_audit_detail(server),
        ip=client_ip(request),
    )
    await db.commit()
    await request.app.state.manager.reload(server.id)
    return _redirect(request, "server_created")


def _adapter_of(request: Request, server: Server) -> type[GameAdapter]:
    adapter = servers.adapter_for(server, _registry(request))
    if adapter is None:
        # The plugin that provided the game was removed; the entry can only be deleted.
        raise HTTPException(status_code=404)
    return adapter


@router.get("/{server_id}/edit", name="admin_server_edit")
async def edit_server(request: Request, db: DbSession, server_id: int) -> Response:
    server = await _server(db, server_id)
    return _form_page(
        request,
        _adapter_of(request, server),
        server=server,
        name=server.name,
        enabled=server.enabled,
        values=servers.public_values(server),
    )


@router.post("/{server_id}/edit", dependencies=[CsrfProtected])
async def update_server(
    request: Request, db: DbSession, auth: RequireAdmin, server_id: int
) -> Response:
    server = await _server(db, server_id)
    adapter = _adapter_of(request, server)
    data = _input(await request.form())
    before = _audit_detail(server)
    try:
        await servers.update_server(db, _box(request), adapter, server, data)
    except servers.ServerError as exc:
        return _form_page(
            request,
            adapter,
            server=server,
            name=data.name,
            enabled=data.enabled,
            values=_resubmitted_values(data, describe_fields(adapter.connection_schema)),
            error=exc,
        )
    audit.record(
        db,
        action="server.update",
        actor=auth.user,
        target_type="server",
        target_id=server.id,
        detail={"before": before, "after": _audit_detail(server)},
        ip=client_ip(request),
    )
    await db.commit()
    await request.app.state.manager.reload(server.id)
    return _redirect(request, "server_updated")


@router.post("/{server_id}/delete", dependencies=[CsrfProtected])
async def delete_server(
    request: Request, db: DbSession, auth: RequireAdmin, server_id: int
) -> Response:
    server = await _server(db, server_id)
    detail = _audit_detail(server)
    await servers.delete_server(db, server)
    audit.record(
        db,
        action="server.delete",
        actor=auth.user,
        target_type="server",
        target_id=server_id,
        detail=detail,
        ip=client_ip(request),
    )
    await db.commit()
    await request.app.state.manager.reload(server_id)
    return _redirect(request, "server_deleted")


@router.post("/{server_id}/test", dependencies=[CsrfProtected])
async def test_server(request: Request, db: DbSession, server_id: int) -> Response:
    server = await _server(db, server_id)
    result = await servers.probe(server, _adapter_of(request, server), _box(request))
    return await _list_page(request, db, probed=server, probe=result)
