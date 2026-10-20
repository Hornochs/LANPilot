"""The on-demand LAN scan for administrators.

The scan runs while the request waits (a few seconds) and the result is rendered at once;
nothing is stored. "Add" links to the normal server form with the found values filled in.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from lanpilot import audit
from lanpilot.auth.roles import Role
from lanpilot.core import servers
from lanpilot.core.scan import Found, ScanBusyError, Scanner, ScanResult
from lanpilot.i18n import M
from lanpilot.web.deps import CsrfProtected, DbSession, RequireAdmin, client_ip, require_role
from lanpilot.web.routes_admin_servers import FIELD_PREFIX
from lanpilot.web.templating import render

router = APIRouter(prefix="/admin/scan", dependencies=[Depends(require_role(Role.ADMIN))])


def _scanner(request: Request) -> Scanner:
    scanner: Scanner = request.app.state.scanner
    return scanner


def add_link(root: str, item: Found) -> str:
    """The server form for a find, with its values filled in (``new_server`` reads them)."""
    server = item.server
    query: dict[str, str] = {"adapter": server.adapter_id, f"{FIELD_PREFIX}host": server.host}
    if server.name:
        query["name"] = server.name
    if server.port is not None:
        query[f"{FIELD_PREFIX}port"] = str(server.port)
    for key, value in server.values.items():
        query[f"{FIELD_PREFIX}{key}"] = value
    return f"{root}/admin/servers/new?{urlencode(query)}"


def _page(
    request: Request,
    *,
    result: ScanResult | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    scanner = _scanner(request)
    root = request.app.state.settings.server.root_path
    context = {
        "enabled": scanner.enabled,
        "networks": scanner.networks,
        "games": [adapter.name for adapter in scanner.adapters()],
        "result": result,
        "rows": [(item, add_link(root, item)) for item in result.found] if result else [],
        "error": error,
    }
    return render(request, "admin/scan.html", context, status_code=status_code)


async def _configured(db: AsyncSession) -> list[tuple[str, str]]:
    return [(server.adapter_id, server.host) for server in await servers.list_servers(db)]


@router.get("", name="admin_scan")
async def scan_page(request: Request) -> Response:
    return _page(request)


@router.post("", dependencies=[CsrfProtected])
async def run_scan(request: Request, db: DbSession, auth: RequireAdmin) -> Response:
    scanner = _scanner(request)
    if not scanner.enabled:
        return _page(request, error=M("No networks are configured for the scan."), status_code=409)
    try:
        result = await scanner.run(await _configured(db))
    except ScanBusyError:
        return _page(
            request, error=M("A scan is already running. Try again in a moment."), status_code=409
        )
    audit.record(
        db,
        action="scan.run",
        actor=auth.user,
        target_type="network",
        target_id=None,
        detail={
            "networks": ", ".join(result.networks),
            "found": len(result.found),
            "failed": ", ".join(result.failed),
        },
        ip=client_ip(request),
    )
    await db.commit()
    return _page(request, result=result)
