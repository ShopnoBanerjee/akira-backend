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
from app.domains.outlets.router import router as outlets_router
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
