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

from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, get_engine, warm_pool
from app.core.errors import register_error_handlers
from app.core.hardening import RateLimitMiddleware, SecurityHeadersMiddleware, problems_as_text
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


class ProductionConfigError(RuntimeError):
    """Raised at startup when ENV=production and the configuration is not fit
    to serve. Uvicorn exits non-zero and the platform keeps the previous
    release running - which is the whole point of refusing rather than
    warning."""


def check_production_config(cfg: Settings) -> None:
    if not cfg.is_production:
        return
    problems = cfg.production_problems()
    if problems:
        raise ProductionConfigError(
            "Refusing to start with ENV=production:\n" + problems_as_text(problems)
        )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    check_production_config(settings)
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


def create_app(cfg: Settings) -> FastAPI:
    """Build the application for one configuration.

    Module-level `app` below is what uvicorn serves; the factory exists so a
    test can build a production-shaped app without a production .env.
    """
    application = FastAPI(
        title="AKIRA Ops Suite API",
        version="0.1.0",
        description=(
            "Internal multi-outlet restaurant operations platform.\n\n"
            "Authenticate with a Supabase access token: `Authorization: Bearer <token>`."
        ),
        lifespan=lifespan,
        # The interactive docs are a development convenience; the contract
        # itself is openapi.json in the repository. In production the API
        # exposes nothing it does not need to.
        docs_url=None if cfg.is_production else "/docs",
        redoc_url=None if cfg.is_production else "/redoc",
        openapi_url=None if cfg.is_production else "/openapi.json",
    )

    # Middleware is applied inside-out: the first added is the innermost.
    # The limiter sits inside CORS so a 429 still carries the CORS headers
    # the browser needs to show it; the security headers wrap everything
    # the routers produce, including error responses.
    application.add_middleware(RateLimitMiddleware, per_minute=cfg.RATE_LIMIT_PER_MINUTE)
    application.add_middleware(SecurityHeadersMiddleware, production=cfg.is_production)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    )

    # The browser link is the other long wire. A 100-row list is 45 KB of
    # JSON and crosses a 300 ms path in several TCP windows; gzipped it is
    # one. Below 1 KB the header costs more than it saves.
    application.add_middleware(GZipMiddleware, minimum_size=1024)

    register_error_handlers(application)

    application.include_router(users_router)
    application.include_router(outlets_router)
    application.include_router(devices_router)
    application.include_router(inventory_router)
    application.include_router(stock_counts_router)
    application.include_router(settings_router)
    application.include_router(jobs_router)
    application.include_router(dashboard_router)
    application.include_router(sales_router)
    # runs_router first: /sop/runs/... must match before /sop's own routes.
    application.include_router(runs_router)
    application.include_router(review_router)
    application.include_router(reference_router)
    application.include_router(sop_router)
    application.include_router(floor_router)

    @application.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, Any]:
        """Liveness probe. Must never touch the database."""
        return {"status": "ok", "env": cfg.ENV, "version": application.version}

    @application.get("/readyz", tags=["meta"])
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

    return application


app = create_app(settings)
