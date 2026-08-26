"""Application settings.

Every value comes from the environment. See .env.example for the full documented
set. Expanded in P2 (auth) with the JWKS/service-key plumbing.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: Literal["local", "ci", "staging", "production"] = "local"

    # Supabase — auth + storage only. Application data goes through DATABASE_URL.
    SUPABASE_URL: str = ""
    SUPABASE_PUBLISHABLE_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_JWT_JWKS_URL: str = ""

    # Direct Postgres connection. FastAPI owns all application reads and writes.
    DATABASE_URL: str = ""

    # Comma-separated list of allowed browser origins.
    CORS_ORIGINS: str = "http://localhost:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # Salt for customer_phone_hash. Rotating this orphans existing hashes.
    PHONE_HASH_SALT: str = Field(default="dev-only-not-a-secret")


@lru_cache
def get_settings() -> Settings:
    return Settings()
