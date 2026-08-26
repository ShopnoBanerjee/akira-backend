"""FastAPI application entrypoint.

Domain routers are mounted here as each epic lands.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine
from app.core.errors import register_error_handlers
from app.domains.devices.router import router as devices_router
from app.domains.inventory.router import router as inventory_router
from app.domains.jobs.router import router as jobs_router
from app.domains.outlets.router import router as outlets_router
from app.domains.settings.router import router as settings_router
from app.domains.sop.router import router as sop_router
from app.domains.sop.runs_router import floor_router, runs_router
from app.domains.users.router import router as users_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


app = FastAPI(
    title="AKIRA Ops Suite API",
    version="0.1.0",
    description=(
        "Internal multi-outlet restaurant operations platform.\n\n"
        "Authenticate with a Supabase access token: `Authorization: Bearer <token>`."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

app.include_router(users_router)
app.include_router(outlets_router)
app.include_router(devices_router)
app.include_router(inventory_router)
app.include_router(settings_router)
app.include_router(jobs_router)
# runs_router first: /sop/runs/... must match before /sop's own routes.
app.include_router(runs_router)
app.include_router(sop_router)
app.include_router(floor_router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, Any]:
    """Liveness probe. Must never touch the database."""
    return {"status": "ok", "env": settings.ENV, "version": app.version}


@app.get("/readyz", tags=["meta"])
async def readyz() -> dict[str, Any]:
    """Readiness probe. Unlike /healthz this does check the database, because a
    process that cannot reach Postgres is running but not serving."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:
        logger.warning("readiness check failed: %s", exc)
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
