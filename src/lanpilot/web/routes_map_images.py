"""A server's maps for administrators: maps added by hand, and preview images (upload,
removal, online lookup); delivery of the images to every signed-in user."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse, RedirectResponse, Response

from lanpilot import audit, i18n
from lanpilot.auth.roles import Role
from lanpilot.core import image_lookup, map_images, maps, servers
from lanpilot.core.actions import ActionError, Actor
from lanpilot.core.adapters import Capability, GameAdapter
from lanpilot.core.image_sources import SOURCES
from lanpilot.db.models import MapImage, Server, ServerMap
from lanpilot.i18n import M
from lanpilot.web.deps import (
    CsrfProtected,
    DbSession,
    RequireAdmin,
    RequireAuth,
    client_ip,
    require_role,
)
from lanpilot.web.templating import render

ONLINE_OFF = M("Looking for images online is switched off (images.online in the configuration).")

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
    images = (await db.execute(select(MapImage).where(MapImage.game == adapter.id))).scalars()
    lookup: image_lookup.ImageLookup = request.app.state.image_lookup
    context = {
        "server": server,
        "game": adapter.id,
        "maps": await maps.list_maps(db, server.id),
        # Where each image came from, by lower-case map key, as a name to show.
        "providers": {
            row.map_key: SOURCES[row.provider].title if row.provider in SOURCES else None
            for row in images
        },
        "misses": await image_lookup.misses(db, adapter.id),
        "online": lookup.enabled,
        "manual_maps": Capability.MANUAL_MAPS in adapter.capabilities,
        "manual_source": maps.SOURCE_MANUAL,
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
        stored = await index.store(db, directory, adapter.id, map_key, data)
    except map_images.ImageError as exc:
        return await _page(
            request, db, server, adapter, error=i18n.message_of(exc), status_code=400
        )

    audit.record(
        db,
        action="map_image.upload",
        actor=auth.user,
        target_type="map",
        target_id=f"{adapter.id}/{map_key}"[:64],
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

    if await index.remove(db, directory, adapter.id, map_key):
        # Otherwise the online lookup would bring a removed picture straight back.
        await image_lookup.remember_miss(db, adapter.id, map_key, image_lookup.REASON_REMOVED)
        audit.record(
            db,
            action="map_image.remove",
            actor=auth.user,
            target_type="map",
            target_id=f"{adapter.id}/{map_key}"[:64],
            ip=client_ip(request),
        )
        await db.commit()
        request.app.state.manager.announce(server_id)
    return _redirect(request, server_id, "image_removed")


@admin_router.post("/{server_id}/maps/fetch-images", dependencies=[CsrfProtected])
async def fetch_missing_images(
    request: Request, db: DbSession, auth: RequireAdmin, server_id: int
) -> Response:
    server, adapter = await _server_and_adapter(request, db, server_id)
    lookup: image_lookup.ImageLookup = request.app.state.image_lookup
    if not lookup.enabled:
        return await _page(request, db, server, adapter, error=ONLINE_OFF, status_code=409)
    refs = [maps.to_ref(row) for row in await maps.list_maps(db, server_id)]
    queued = lookup.want_all(adapter.id, refs, image_lookup.Reach.MISSING)
    audit.record(
        db,
        action="map_image.fetch_missing",
        actor=auth.user,
        target_type="server",
        target_id=str(server_id),
        detail={"queued": queued},
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, server_id, "images_requested")


@admin_router.post("/{server_id}/maps/{map_id}/image/fetch", dependencies=[CsrfProtected])
async def fetch_image(
    request: Request, db: DbSession, auth: RequireAdmin, server_id: int, map_id: int
) -> Response:
    server, adapter = await _server_and_adapter(request, db, server_id)
    row = await _map(db, server_id, map_id)
    lookup: image_lookup.ImageLookup = request.app.state.image_lookup
    if not lookup.enabled:
        return await _page(request, db, server, adapter, error=ONLINE_OFF, status_code=409)
    lookup.want(adapter.id, maps.to_ref(row), image_lookup.Reach.ONE)
    audit.record(
        db,
        action="map_image.fetch",
        actor=auth.user,
        target_type="map",
        target_id=f"{adapter.id}/{row.key}"[:64],
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, server_id, "images_requested")


@admin_router.post("/{server_id}/maps/manual", dependencies=[CsrfProtected])
async def add_map(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    server_id: int,
    key: str = Form(default="", max_length=1000),
) -> Response:
    server, adapter = await _server_and_adapter(request, db, server_id)
    actor = Actor(auth.user.id, auth.user.username, auth.role, client_ip(request))
    try:
        await request.app.state.actions.add_map(actor, server_id, key)
    except ActionError as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404) from None
        return await _page(
            request, db, server, adapter, error=i18n.message_of(exc), status_code=exc.status
        )
    return _redirect(request, server_id, "map_added")


@admin_router.post("/{server_id}/maps/{map_id}/manual/delete", dependencies=[CsrfProtected])
async def remove_map(
    request: Request, db: DbSession, auth: RequireAdmin, server_id: int, map_id: int
) -> Response:
    await _server_and_adapter(request, db, server_id)
    row = await _map(db, server_id, map_id)
    if row.source != maps.SOURCE_MANUAL:
        # Maps the server lists come and go with the server's own list.
        raise HTTPException(status_code=404)
    key = row.key
    await db.delete(row)
    audit.record(
        db,
        action="server.remove_map",
        actor=auth.user,
        target_type="server",
        target_id=str(server_id),
        detail={"map": key[:200]},
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, server_id, "map_removed")
