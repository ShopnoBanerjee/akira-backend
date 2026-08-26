# AKIRA Ops Suite — API

FastAPI backend for AKIRA's internal multi-outlet restaurant operations
platform. Stage 1 delivers the auth and organisation foundation, the SOP
compliance module with photo proof, and a sales-file ingestion skeleton.

The web client is a separate repository:
[akira-frontend](https://github.com/ShopnoBanerjee/akira-frontend).

- **Constitution:** [CLAUDE.md](CLAUDE.md) — read first
- **Specification:** [docs/STAGE1_SPEC.md](docs/STAGE1_SPEC.md)
- **Decisions and deviations:** [docs/DECISIONS.md](docs/DECISIONS.md)

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.x async · asyncpg · Supabase
(Postgres, Auth, Storage) · uv

## Setup

```bash
uv sync
cp .env.example .env    # then fill in the secrets
```

`.env` needs a `SUPABASE_SECRET_KEY` and a `DATABASE_URL`. Both come from the
Supabase dashboard under Project Settings; every variable is documented inline
in `.env.example`.

Supabase signs JWTs asymmetrically (ES256) on this project, so token
verification reads the public key set from `SUPABASE_JWKS_URL`. There is no
shared JWT secret to configure.

The schema is applied to the hosted Supabase project. The direct database
host is **IPv6-only**; from an IPv4-only environment use the Supabase session
pooler instead.

For a local database instead of the hosted project:

```bash
docker compose up -d db
```

Migrations live in `supabase/migrations/` and are append-only. Apply them in
filename order; `supabase/local/` holds a test-only auth shim that must never
be applied to Supabase.

## Run

```bash
uv run uvicorn app.main:app --reload
```

API on http://localhost:8000 · docs at `/docs` · health at `/healthz`.

## Checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy app
```

## The frontend contract

`openapi.json` at the repo root is the contract the web client generates its
types from. Regenerate and commit it after any endpoint change:

```bash
uv run python scripts/export_openapi.py
```

CI in the frontend repo fails if its committed types drift from this file.
