"""Liveness endpoint for service managers, reverse proxies and package tests."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> JSONResponse:
    # Unauthenticated by design, so it reveals nothing beyond "up" or "down".
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("health check: database is not reachable")
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok"})
