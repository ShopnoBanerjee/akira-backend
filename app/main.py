"""FastAPI application entrypoint.

Domain routers are mounted here as each epic lands.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine, warm_pool
from app.core.errors import register_error_handlers
from app.domains.dashboard.router import router as dashboard_router
from app.domains.devices.router import router as devices_router
from app.domains.inventory.counts_router import router as stock_counts_router
from app.domains.inventory.router import router as inventory_router
from app.domains.jobs.router import router as jobs_router
from app.domains.outlets.router import router as outlets_router
from app.domains.sales.router import router as sales_router
from app.domains.settings.router import router as settings_router
from app.domains.sop.reference_router import router as reference_router
from app.domains.sop.review_router import router as review_router
from app.domains.sop.router import router as sop_router
from app.domains.sop.runs_router import floor_router, runs_router
from app.domains.users.router import router as users_router
from app.integrations import storage
from app.jobs import scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Pay the 3.5 s connection setup now, several at once, rather than have the
    # first few requests after a deploy each pay it alone. See db.warm_pool.
    if settings.DATABASE_URL:
        opened = await warm_pool(get_engine())
        logger.info("database pool warmed: %d connections", opened)
    # The scheduler shares this process's event loop. Exactly one instance may
    # run it; see app/jobs/scheduler.py.
    await scheduler.start()
    yield
    await scheduler.shutdown()
    await storage.aclose_client()
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

# The browser link is the other long wire. A 100-row list is 45 KB of JSON and
# crosses a 300 ms path in several TCP windows; gzipped it is one. Below 1 KB
# the header costs more than it saves.
app.add_middleware(GZipMiddleware, minimum_size=1024)

register_error_handlers(app)

app.include_router(users_router)
app.include_router(outlets_router)
app.include_router(devices_router)
app.include_router(inventory_router)
app.include_router(stock_counts_router)
app.include_router(settings_router)
app.include_router(jobs_router)
app.include_router(dashboard_router)
app.include_router(sales_router)
# runs_router first: /sop/runs/... must match before /sop's own routes.
app.include_router(runs_router)
app.include_router(review_router)
app.include_router(reference_router)
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
