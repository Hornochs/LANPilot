"""User management for administrators."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from lanpilot import audit, i18n
from lanpilot.auth import users
from lanpilot.auth.roles import Role
from lanpilot.db.models import User
from lanpilot.i18n import M
from lanpilot.web.deps import CsrfProtected, DbSession, RequireAdmin, client_ip, require_role
from lanpilot.web.templating import render

# The router-level dependency guards every route below, including ones added later.
router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_role(Role.ADMIN))])


async def _page(
    request: Request, db: AsyncSession, *, error: str | None = None, status_code: int = 200
) -> Response:
    context = {"users": await users.list_users(db), "role_choices": list(Role), "error": error}
    return render(request, "admin/users.html", context, status_code=status_code)


def _redirect(request: Request, notice: str) -> Response:
    root = request.app.state.settings.server.root_path
    return RedirectResponse(f"{root}/admin/users?notice={notice}", status_code=303)


async def _target(db: AsyncSession, user_id: int) -> User:
    user = await users.get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return user


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown role.") from None


@router.get("", name="admin_users")
async def list_users(request: Request, db: DbSession) -> Response:
    return await _page(request, db)


@router.post("", dependencies=[CsrfProtected])
async def create_user(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = Role.VIEWER.value,
) -> Response:
    new_role = _role(role)
    try:
        user = await users.create_user(db, username, password, new_role)
    except users.UserError as exc:
        return await _page(request, db, error=i18n.message_of(exc), status_code=400)
    audit.record(
        db,
        action="user.create",
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "role": user.role},
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, "user_created")


@router.post("/{user_id}/role", dependencies=[CsrfProtected])
async def change_role(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    user_id: int,
    role: Annotated[str, Form()] = "",
) -> Response:
    user = await _target(db, user_id)
    new_role = _role(role)
    old_role = user.role
    try:
        await users.set_role(db, user, new_role)
    except users.UserError as exc:
        return await _page(request, db, error=i18n.message_of(exc), status_code=400)
    audit.record(
        db,
        action="user.role",
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "from": old_role, "to": user.role},
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, "user_updated")


@router.post("/{user_id}/active", dependencies=[CsrfProtected])
async def change_active(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    user_id: int,
    active: Annotated[str, Form()] = "",
) -> Response:
    user = await _target(db, user_id)
    make_active = active == "1"
    try:
        await users.set_active(db, user, make_active)
    except users.UserError as exc:
        return await _page(request, db, error=i18n.message_of(exc), status_code=400)
    audit.record(
        db,
        action="user.enable" if make_active else "user.disable",
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, "user_updated")


@router.post("/{user_id}/password", dependencies=[CsrfProtected])
async def reset_password(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    user_id: int,
    password: Annotated[str, Form()] = "",
) -> Response:
    user = await _target(db, user_id)
    if user.id == auth.user.id:
        # Changing one's own password requires the current one; see /account.
        return await _page(
            request,
            db,
            error=M("Change your own password on the account page."),
            status_code=400,
        )
    try:
        await users.set_password(db, user, password)
    except users.UserError as exc:
        return await _page(request, db, error=i18n.message_of(exc), status_code=400)
    audit.record(
        db,
        action="user.password_reset",
        actor=auth.user,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, "password_reset")


@router.post("/{user_id}/delete", dependencies=[CsrfProtected])
async def delete_user(
    request: Request, db: DbSession, auth: RequireAdmin, user_id: int
) -> Response:
    user = await _target(db, user_id)
    if user.id == auth.user.id:
        return await _page(
            request, db, error=M("You cannot delete your own account."), status_code=400
        )
    detail = {"username": user.username, "role": user.role}
    try:
        await users.delete_user(db, user)
    except users.UserError as exc:
        return await _page(request, db, error=i18n.message_of(exc), status_code=400)
    audit.record(
        db,
        action="user.delete",
        actor=auth.user,
        target_type="user",
        target_id=user_id,
        detail=detail,
        ip=client_ip(request),
    )
    await db.commit()
    return _redirect(request, "user_deleted")
