"""Map preview images: delivery to signed-in users, upload and removal by administrators."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse, RedirectResponse, Response

from lanpilot import audit
from lanpilot.auth.roles import Role
from lanpilot.core import map_images, maps, servers
from lanpilot.core.adapters import GameAdapter
from lanpilot.db.models import Server, ServerMap
from lanpilot.web.deps import (
    CsrfProtected,
    DbSession,
    RequireAdmin,
    RequireAuth,
    client_ip,
    require_role,
)
from lanpilot.web.templating import render

router = APIRouter()
admin_router = APIRouter(prefix="/admin/servers", dependencies=[Depends(require_role(Role.ADMIN))])


@router.get("/map-images/{filename}", name="map_image")
async def map_image(request: Request, auth: RequireAuth, filename: str) -> Response:
    index: map_images.MapImageIndex = request.app.state.map_images
    # Only names LANPilot generated itself are served; the pattern alone rules out any path.
    if not map_images.FILENAME_PATTERN.fullmatch(filename) or not index.knows(filename):
        raise HTTPException(status_code=404)
    path = map_images.images_directory(request.app.state.settings.data_dir) / filename
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type=map_images.OUTPUT_MEDIA_TYPE,
        # The name changes whenever the image does, so browsers may keep it for good.
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


async def _server_and_adapter(
    request: Request, db: AsyncSession, server_id: int
) -> tuple[Server, type[GameAdapter]]:
    server = await servers.get_server(db, server_id)
    adapter = servers.adapter_for(server, request.app.state.registry) if server else None
    if server is None or adapter is None:
        raise HTTPException(status_code=404)
    return server, adapter


async def _page(
    request: Request,
    db: AsyncSession,
    server: Server,
    adapter: type[GameAdapter],
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "server": server,
        "family": adapter.family,
        "maps": await maps.list_maps(db, server.id),
        "error": error,
        "max_megabytes": map_images.MAX_UPLOAD_BYTES // (1024 * 1024),
    }
    return render(request, "admin/server_maps.html", context, status_code=status_code)


async def _map(db: AsyncSession, server_id: int, map_id: int) -> ServerMap:
    row = await maps.get_map(db, server_id, map_id)
    if row is None:
        raise HTTPException(status_code=404)
    return row


def _redirect(request: Request, server_id: int, notice: str) -> Response:
    root = request.app.state.settings.server.root_path
    return RedirectResponse(f"{root}/admin/servers/{server_id}/maps?notice={notice}", 303)


@admin_router.get("/{server_id}/maps", name="admin_server_maps")
async def server_maps(request: Request, db: DbSession, server_id: int) -> Response:
    server, adapter = await _server_and_adapter(request, db, server_id)
    return await _page(request, db, server, adapter)


@admin_router.post("/{server_id}/maps/{map_id}/image", dependencies=[CsrfProtected])
async def upload_image(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    server_id: int,
    map_id: int,
    image: UploadFile | None = None,
) -> Response:
    server, adapter = await _server_and_adapter(request, db, server_id)
    row = await _map(db, server_id, map_id)
    map_key = row.key
    index: map_images.MapImageIndex = request.app.state.map_images
    directory = map_images.images_directory(request.app.state.settings.data_dir)

    # One byte more than allowed is enough to know that the file is too large.
    data = await image.read(map_images.MAX_UPLOAD_BYTES + 1) if image is not None else b""
    try:
        stored = await index.store(db, directory, adapter.family, map_key, data)
    except map_images.ImageError as exc:
        return await _page(request, db, server, adapter, error=str(exc), status_code=400)

    audit.record(
        db,
        action="map_image.upload",
        actor=auth.user,
        target_type="map",
        target_id=f"{adapter.family}/{map_key}"[:64],
        detail={"width": stored.width, "height": stored.height},
        ip=client_ip(request),
    )
    await db.commit()
    request.app.state.manager.announce(server_id)
    return _redirect(request, server_id, "image_saved")


@admin_router.post("/{server_id}/maps/{map_id}/image/delete", dependencies=[CsrfProtected])
async def remove_image(
    request: Request, db: DbSession, auth: RequireAdmin, server_id: int, map_id: int
) -> Response:
    _, adapter = await _server_and_adapter(request, db, server_id)
    row = await _map(db, server_id, map_id)
    map_key = row.key
    index: map_images.MapImageIndex = request.app.state.map_images
    directory = map_images.images_directory(request.app.state.settings.data_dir)

    if await index.remove(db, directory, adapter.family, map_key):
        audit.record(
            db,
            action="map_image.remove",
            actor=auth.user,
            target_type="map",
            target_id=f"{adapter.family}/{map_key}"[:64],
            ip=client_ip(request),
        )
        await db.commit()
        request.app.state.manager.announce(server_id)
    return _redirect(request, server_id, "image_removed")
