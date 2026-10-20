"""Audit log for administrators."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from starlette.responses import Response

from lanpilot.auth.roles import Role
from lanpilot.db.models import AuditEntry
from lanpilot.web.deps import DbSession, require_role
from lanpilot.web.templating import render

router = APIRouter(prefix="/admin/audit", dependencies=[Depends(require_role(Role.ADMIN))])

PAGE_SIZE = 200


def _detail(entry: AuditEntry) -> str:
    try:
        detail = json.loads(entry.detail)
    except ValueError:
        return entry.detail
    if not isinstance(detail, dict):
        return entry.detail
    return ", ".join(f"{key}: {value}" for key, value in detail.items())


@router.get("", name="admin_audit")
async def audit_log(request: Request, db: DbSession) -> Response:
    entries = (
        (await db.execute(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(PAGE_SIZE)))
        .scalars()
        .all()
    )
    rows = [(entry, _detail(entry)) for entry in entries]
    return render(request, "admin/audit.html", {"rows": rows, "page_size": PAGE_SIZE})
