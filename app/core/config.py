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

    # Which transport answers. The prompt is identical whichever it is and the
    # actual model id is recorded on every verdict, so they never get confused
    # for each other.
    #   anthropic — the Claude SDK (D12); needs ANTHROPIC_API_KEY.
    #   gemini    — Gemini's native REST API; needs GEMINI_API_KEY.
    #   openai    — ANY endpoint speaking the OpenAI chat-completions format
    #               (D28): Gemini's compatibility layer by default, or
    #               OpenRouter, or a local Ollama, chosen by base URL alone.
    #               This replaced Groq, whose key had leaked and whose free
    #               tier was too tight for two photos a request.
    AI_REVIEW_PROVIDER: Literal["anthropic", "gemini", "openai"] = "anthropic"

    # --- Stage 2: stock sheet extraction -------------------------------------
    # gemini is the production extractor: measured on the real sheet it
    # row-aligns handwriting correctly and preserves it verbatim, and the
    # free tier covers a single outlet many times over. openai is the same
    # question over any OpenAI-compatible endpoint (D28); everything it
    # extracts is forced into human review unless the endpoint is Gemini's
    # own. stub replays a fixture so the pipeline is testable with no key.
    STOCK_EXTRACT_PROVIDER: Literal["anthropic", "gemini", "openai", "stub"] = "gemini"
    STOCK_EXTRACT_MODEL: str = "claude-opus-5"

    # --- Google AI (Gemini) --------------------------------------------------
    GEMINI_API_KEY: str = ""
    #: gemini-3-flash-preview leads OCR benchmarks and is what the extraction
    #: was measured on. Pinned rather than -latest so a silent model swap
    #: cannot change extraction behaviour under us.
    GEMINI_MODEL: str = "gemini-3-flash-preview"

    # --- Any OpenAI-compatible endpoint (D28) --------------------------------
    # One client, many vendors. Gemini's compatibility layer is the default
    # because the key is already here and the free tier is the widest; point
    # the base URL at OpenRouter (https://openrouter.ai/api/v1) or a local
    # Ollama (http://localhost:11434/v1) and nothing else changes. When the
    # key or model is left blank and the base URL is Gemini's, the Gemini
    # settings above are used, so the common case needs no new secrets.
    OPENAI_COMPAT_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    OPENAI_COMPAT_API_KEY: str = ""
    OPENAI_COMPAT_MODEL: str = ""

    @property
    def openai_compat_is_gemini(self) -> bool:
        return "generativelanguage.googleapis.com" in self.OPENAI_COMPAT_BASE_URL

    @property
    def openai_compat_key(self) -> str:
        if self.OPENAI_COMPAT_API_KEY:
            return self.OPENAI_COMPAT_API_KEY
        return self.GEMINI_API_KEY if self.openai_compat_is_gemini else ""

    @property
    def openai_compat_model(self) -> str:
        if self.OPENAI_COMPAT_MODEL:
            return self.OPENAI_COMPAT_MODEL
        return self.GEMINI_MODEL if self.openai_compat_is_gemini else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
