"""The two pieces of production posture that live in the request path.

**Rate limiting.** One token bucket per caller, in this process's memory. The
caller is the bearer token when there is one (a manager's JWT, a tablet's
device session) and the client address when there is not, so the whole outlet
sharing one NAT address does not share one bucket, while an unauthenticated
scanner hitting the API from one address does. The limit is generous - a
dashboard screen is a dozen requests, the floor tablet a handful a minute -
and exists to turn a runaway client or a credential-stuffing loop into a 429
instead of a saturated database pool. It is not a security boundary; the PIN
lockout (SECURITY.md #7) and Supabase Auth's own limits are.

In-memory is correct for this deployment, which is one instance by
construction (the scheduler forbids a second - see app/jobs/scheduler.py). A
second replica would get its own buckets and the effective limit would double,
which is a nuisance rather than a hole.

**Security headers.** The API serves JSON to one known SPA. `nosniff` and
`no-store` are the two that matter here: the first so a browser never
reinterprets a JSON body, the second so a shared tablet's back-forward cache
does not hand the next person the previous person's review queue. HSTS only in
production, because it is a promise about a hostname and localhost cannot keep
it.
"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import CONTENT_TYPE

#: Probes are exempt: a platform health checker must never be told to go away.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})

#: Buckets idle for longer than this are dropped on the next sweep.
BUCKET_IDLE_SECONDS = 600.0
#: Sweep when the table grows past this many callers.
SWEEP_THRESHOLD = 5000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A token bucket per key. `capacity` tokens, refilled at `capacity` per
    minute, so a caller can burst a screen's worth of requests and then
    settle to the sustained rate."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive; use 0 at the setting to disable")
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60.0
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def take(self, key: str) -> tuple[bool, int, float]:
        """Spend one token. Returns (allowed, remaining, seconds_until_next)."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > SWEEP_THRESHOLD:
                self._sweep(now)
        else:
            elapsed = now - bucket.updated
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, int(bucket.tokens), 0.0
        wait = (1.0 - bucket.tokens) / self.refill_per_second
        return False, 0, wait

    def _sweep(self, now: float) -> None:
        stale = [k for k, b in self._buckets.items() if now - b.updated > BUCKET_IDLE_SECONDS]
        for k in stale:
            del self._buckets[k]


def caller_key(scope: Scope) -> str:
    """Who is asking: the bearer token if present, else the client address.

    The token is hashed and truncated so the key table never holds a
    credential. The address is whatever uvicorn put in the scope - behind a
    proxy that is the forwarded client only when uvicorn was started with
    --proxy-headers, which the Dockerfile does.
    """
    headers = Headers(scope=scope)
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and len(auth) > 7:
        return "t:" + hashlib.sha256(auth[7:].encode()).hexdigest()[:24]
    client = scope.get("client")
    host = client[0] if client else "unknown"
    return "ip:" + str(host)


class RateLimitMiddleware:
    """Pure ASGI, so the 429 is written without building a Request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.app = app
        self.limiter = limiter or (RateLimiter(per_minute) if per_minute > 0 else None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.limiter is None or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        allowed, remaining, wait = self.limiter.take(caller_key(scope))
        limit = str(int(self.limiter.capacity))
        if not allowed:
            retry_after = max(1, int(wait + 0.999))
            response = JSONResponse(
                status_code=429,
                media_type=CONTENT_TYPE,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": limit,
                    "X-RateLimit-Remaining": "0",
                },
                content={
                    "type": "https://akira.ops/errors/rate-limit",
                    "title": "Too Many Requests",
                    "status": 429,
                    "detail": f"Slow down. Try again in {retry_after} seconds.",
                    "instance": scope["path"],
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_budget(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-RateLimit-Limit"] = limit
                headers["X-RateLimit-Remaining"] = str(remaining)
            await send(message)

        await self.app(scope, receive, send_with_budget)


BASE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # Cross-Origin-Resource-Policy: the SPA is on another origin, so
    # `cross-origin`; CORS is what decides which one.
    "Cross-Origin-Resource-Policy": "cross-origin",
}

HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, production: bool) -> None:
        self.app = app
        self.headers = dict(BASE_HEADERS)
        if production:
            self.headers["Strict-Transport-Security"] = HSTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
                # A handler that set its own caching policy keeps it; the
                # default for an API answering with somebody's data is none.
                headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def problems_as_text(problems: list[str]) -> str:
    return "\n".join(f"  - {p}" for p in problems)
