"""Production posture: the startup guard, the rate limiter, the headers.

None of this touches a database. The guard is a pure function of Settings,
and the middleware is exercised through a tiny app so the assertions are about
the middleware rather than about whichever router happens to answer.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.hardening import (
    BASE_HEADERS,
    HSTS,
    RateLimiter,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    caller_key,
)
from app.main import ProductionConfigError, check_production_config, create_app
from tests.conftest import isolated_settings

PROD_OK = {
    "ENV": "production",
    "DATABASE_URL": "postgresql+asyncpg://postgres:x@db.example.supabase.co:5432/postgres",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SECRET_KEY": "sb_secret_test",
    "SUPABASE_JWKS_URL": "https://example.supabase.co/auth/v1/.well-known/jwks.json",
    "PHONE_HASH_SALT": "a-long-random-value-of-at-least-24-chars",
    "CORS_ORIGINS": "https://ops.akira.example",
    "SMTP_FROM": "AKIRA Ops <ops@akira.example.com>",
}


# --- The guard -----------------------------------------------------------------


class TestTheProductionGuard:
    def test_the_defaults_are_refused_in_production(self) -> None:
        problems = isolated_settings(ENV="production").production_problems()
        joined = "\n".join(problems)
        assert "PHONE_HASH_SALT is the development default" in joined
        assert "DATABASE_URL is empty" in joined
        assert "SUPABASE_SECRET_KEY" in joined
        assert "development origin: http://localhost:5173" in joined

    def test_a_complete_production_configuration_passes(self) -> None:
        assert isolated_settings(**PROD_OK).production_problems() == []

    def test_the_example_salt_is_also_a_default(self) -> None:
        salt = "change-me-before-any-real-ingest"
        cfg = isolated_settings(**{**PROD_OK, "PHONE_HASH_SALT": salt})
        assert cfg.production_problems() == ["PHONE_HASH_SALT is the development default"]

    def test_a_short_salt_is_refused(self) -> None:
        cfg = isolated_settings(**{**PROD_OK, "PHONE_HASH_SALT": "short"})
        assert cfg.production_problems() == ["PHONE_HASH_SALT is shorter than 24 characters"]

    def test_a_jwks_url_from_another_project_is_caught(self) -> None:
        cfg = isolated_settings(
            **{**PROD_OK, "SUPABASE_JWKS_URL": "https://other.supabase.co/auth/v1/jwks.json"}
        )
        assert cfg.production_problems() == ["SUPABASE_JWKS_URL does not belong to SUPABASE_URL"]

    @pytest.mark.parametrize(
        ("origins", "fragment"),
        [
            ("*", "every origin"),
            ("http://ops.akira.example", "non-https"),
            ("https://ops.akira.example,http://127.0.0.1:5173", "development origin"),
            ("", "CORS_ORIGINS is empty"),
        ],
    )
    def test_cors_origins_must_be_real_https_hosts(self, origins: str, fragment: str) -> None:
        cfg = isolated_settings(**{**PROD_OK, "CORS_ORIGINS": origins})
        assert any(fragment in p for p in cfg.production_problems()), cfg.production_problems()

    def test_sql_echo_and_placeholder_sender_are_caught(self) -> None:
        cfg = isolated_settings(**{**PROD_OK, "SQL_ECHO": True, "SMTP_FROM": "x <a@akira.local>"})
        assert set(cfg.production_problems()) == {
            "SQL_ECHO logs every statement, with parameters",
            "SMTP_FROM is a placeholder address",
        }

    def test_the_guard_raises_with_every_problem_listed(self) -> None:
        with pytest.raises(ProductionConfigError) as info:
            check_production_config(isolated_settings(ENV="production"))
        message = str(info.value)
        assert "Refusing to start with ENV=production" in message
        assert "PHONE_HASH_SALT" in message and "DATABASE_URL" in message

    def test_the_guard_is_silent_outside_production(self) -> None:
        check_production_config(isolated_settings(ENV="local"))
        check_production_config(isolated_settings(ENV="staging"))


# --- The rate limiter ------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestTheTokenBucket:
    def test_a_burst_up_to_the_limit_is_allowed_then_refused(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(3, clock=clock)
        assert [limiter.take("a")[0] for _ in range(3)] == [True, True, True]
        allowed, remaining, wait = limiter.take("a")
        assert not allowed and remaining == 0
        assert 0 < wait <= 20.0  # 3/min refills one token every 20 s

    def test_tokens_come_back_with_time(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(60, clock=clock)
        for _ in range(60):
            limiter.take("a")
        assert not limiter.take("a")[0]
        clock.now += 1.0  # one second: one token at 60/min
        assert limiter.take("a")[0]
        assert not limiter.take("a")[0]

    def test_callers_do_not_share_a_bucket(self) -> None:
        limiter = RateLimiter(1, clock=FakeClock())
        assert limiter.take("a")[0]
        assert limiter.take("b")[0]
        assert not limiter.take("a")[0]

    def test_zero_is_not_a_limiter(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(0)


class TestTheCallerKey:
    def _scope(self, headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None):
        return {"type": "http", "headers": headers, "client": client}

    def test_a_bearer_token_identifies_the_caller_and_is_not_stored_raw(self) -> None:
        key = caller_key(self._scope([(b"authorization", b"Bearer secret-token")], ("1.2.3.4", 1)))
        assert key.startswith("t:") and "secret" not in key

    def test_two_tokens_from_one_address_are_two_callers(self) -> None:
        a = caller_key(self._scope([(b"authorization", b"Bearer one")], ("1.2.3.4", 1)))
        b = caller_key(self._scope([(b"authorization", b"Bearer two")], ("1.2.3.4", 1)))
        assert a != b

    def test_no_token_falls_back_to_the_address(self) -> None:
        assert caller_key(self._scope([], ("1.2.3.4", 1))) == "ip:1.2.3.4"
        assert caller_key(self._scope([], None)) == "ip:unknown"


def _tiny_app(*, per_minute: int, production: bool = False) -> FastAPI:
    app = FastAPI()

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/cached")
    async def cached():  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse({"x": 1}, headers={"Cache-Control": "max-age=60"})

    app.add_middleware(RateLimitMiddleware, per_minute=per_minute)
    app.add_middleware(SecurityHeadersMiddleware, production=production)
    return app


class TestTheRateLimitMiddleware:
    def test_the_limit_is_enforced_as_a_problem_document(self) -> None:
        client = TestClient(_tiny_app(per_minute=2))
        assert client.get("/thing").status_code == 200
        second = client.get("/thing")
        assert second.status_code == 200
        assert second.headers["X-RateLimit-Limit"] == "2"
        assert second.headers["X-RateLimit-Remaining"] == "0"
        third = client.get("/thing")
        assert third.status_code == 429
        assert third.headers["content-type"].startswith("application/problem+json")
        assert int(third.headers["Retry-After"]) >= 1
        body = third.json()
        assert body["type"].endswith("/rate-limit") and body["status"] == 429
        assert body["instance"] == "/thing"

    def test_health_probes_are_never_limited(self) -> None:
        client = TestClient(_tiny_app(per_minute=1))
        for _ in range(5):
            assert client.get("/healthz").status_code == 200

    def test_a_different_bearer_token_has_its_own_budget(self) -> None:
        client = TestClient(_tiny_app(per_minute=1))
        assert client.get("/thing", headers={"Authorization": "Bearer a"}).status_code == 200
        assert client.get("/thing", headers={"Authorization": "Bearer a"}).status_code == 429
        assert client.get("/thing", headers={"Authorization": "Bearer b"}).status_code == 200

    def test_zero_disables_the_limiter(self) -> None:
        client = TestClient(_tiny_app(per_minute=0))
        for _ in range(20):
            assert client.get("/thing").status_code == 200
        assert "X-RateLimit-Limit" not in client.get("/thing").headers


class TestTheSecurityHeaders:
    def test_every_response_carries_the_base_set_and_no_store(self) -> None:
        client = TestClient(_tiny_app(per_minute=0))
        response = client.get("/thing")
        for name, value in BASE_HEADERS.items():
            assert response.headers[name] == value
        assert response.headers["Cache-Control"] == "no-store"
        assert "Strict-Transport-Security" not in response.headers

    def test_a_handler_that_sets_its_own_cache_policy_keeps_it(self) -> None:
        client = TestClient(_tiny_app(per_minute=0))
        assert client.get("/cached").headers["Cache-Control"] == "max-age=60"

    def test_the_429_carries_them_too(self) -> None:
        client = TestClient(_tiny_app(per_minute=1))
        client.get("/thing")
        refused = client.get("/thing")
        assert refused.status_code == 429
        assert refused.headers["X-Content-Type-Options"] == "nosniff"

    def test_hsts_only_in_production(self) -> None:
        client = TestClient(_tiny_app(per_minute=0, production=True))
        assert client.get("/thing").headers["Strict-Transport-Security"] == HSTS


# --- The real application, shaped for production -------------------------------


class TestTheProductionApp:
    def test_docs_are_off_in_production_and_on_elsewhere(self) -> None:
        prod = create_app(isolated_settings(**PROD_OK))
        assert prod.docs_url is None and prod.redoc_url is None and prod.openapi_url is None
        local = create_app(isolated_settings(ENV="local"))
        assert local.docs_url == "/docs" and local.openapi_url == "/openapi.json"

    def test_the_real_app_answers_with_the_headers(self) -> None:
        from app.main import app

        response = TestClient(app).get("/healthz")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        # exempt from the limiter, so no budget headers
        assert "X-RateLimit-Limit" not in response.headers
