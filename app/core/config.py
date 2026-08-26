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
    SUPABASE_SECRET_KEY: str = ""
    SUPABASE_JWKS_URL: str = ""

    # Direct Postgres connection. FastAPI owns all application reads and writes.
    DATABASE_URL: str = ""

    # Log every statement. Local only, and noisy - off by default.
    SQL_ECHO: bool = False

    # Comma-separated list of allowed browser origins.
    CORS_ORIGINS: str = "http://localhost:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # Salt for customer_phone_hash. Rotating this orphans existing hashes.
    PHONE_HASH_SALT: str = Field(default="dev-only-not-a-secret")

    # --- Scheduled jobs ----------------------------------------------------
    # APScheduler runs in-process, so exactly one instance may have this on.
    # A second replica would double-materialise and double-send the digest;
    # that needs a shared lock before it is safe (Stage 2).
    SCHEDULER_ENABLED: bool = True

    # --- Outbound mail -----------------------------------------------------
    # The daily digest. With no host configured the notifier degrades to
    # logging and says so in the job_runs row, rather than silently sending
    # nothing — a digest that quietly stopped is the failure this whole epic
    # exists to make visible.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "AKIRA Ops <ops@akira.local>"
    SMTP_STARTTLS: bool = True

    @property
    def smtp_configured(self) -> bool:
        return bool(self.SMTP_HOST)

    # --- AI photo review (D6) ----------------------------------------------
    # Advisory only. Absent, ai_review.enabled has nothing to call and the
    # review job records that rather than failing.
    ANTHROPIC_API_KEY: str = ""
    # Sonnet by owner's decision (D12): every photo on every run at every
    # outlet is reviewed, so the per-photo cost is the whole cost. The model
    # is recorded on every verdict, so re-running a run against a different
    # one later is additive rather than a rewrite.
    AI_REVIEW_MODEL: str = "claude-sonnet-5"

    # Which vendor answers. Anthropic is the production path; groq exists so
    # the pipeline can be exercised end to end on a key that is easier to come
    # by (D13). The prompt is identical either way and the actual model id is
    # recorded on every verdict, so the two never get confused for each other.
    AI_REVIEW_PROVIDER: Literal["anthropic", "groq"] = "anthropic"
    GROQ_API_KEY: str = ""
    #: The only image-capable model on Groq's roster, confirmed by asking each
    #: candidate to describe a picture rather than by reading a table.
    GROQ_VISION_MODEL: str = "qwen/qwen3.8-27b"


@lru_cache
def get_settings() -> Settings:
    return Settings()
