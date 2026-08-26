"""FastAPI application entrypoint.

Domain routers are mounted here as each epic lands. Stage 1 P0 ships the
health endpoint and CORS only.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="AKIRA Ops Suite API",
    version="0.1.0",
    description="Internal multi-outlet restaurant operations platform.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, Any]:
    """Liveness probe. Must never touch the database."""
    return {"status": "ok", "env": settings.ENV, "version": app.version}
