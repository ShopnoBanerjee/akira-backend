"""One error hierarchy, rendered as RFC 7807 problem+json.

Never leak SQL, stack traces or row counts to a client. The `detail` field is
written for the person reading it in a browser; anything an operator needs goes
to the log instead.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

CONTENT_TYPE = "application/problem+json"


class AppError(Exception):
    """Base for every error this application raises deliberately."""

    status_code: int = 500
    title: str = "Internal Server Error"
    type_uri: str = "about:blank"

    def __init__(
        self,
        detail: str,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra or {}
        self.headers = headers or {}

    def to_problem(self, instance: str) -> dict[str, Any]:
        return {
            "type": self.type_uri,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
            **self.extra,
        }


class AuthError(AppError):
    """No usable credentials. The client should sign in again."""

    status_code = 401
    title = "Unauthenticated"
    type_uri = "https://akira.ops/errors/unauthenticated"

    def __init__(self, detail: str = "Sign in to continue.", **kw: Any) -> None:
        kw.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kw)


class PendingActivationError(AppError):
    """Authenticated, but the profile has not been activated by an admin.

    Distinct from ForbiddenError so the client can route to a page that explains
    the wait rather than showing a bare refusal. This is what stops a
    self-signup from silently gaining access.
    """

    status_code = 403
    title = "Awaiting Activation"
    type_uri = "https://akira.ops/errors/pending-activation"


class MfaRequiredError(AppError):
    """Signed in, but this login must present a second factor first (D33).

    Its own type so the client routes to the enrol/verify screen instead of
    showing a refusal. Nothing else is wrong with the account.
    """

    status_code = 403
    title = "Second Factor Required"
    type_uri = "https://akira.ops/errors/mfa-required"


class ForbiddenError(AppError):
    """Authenticated, identified, and not allowed to do this.

    Returned as 403 rather than disguised as a 404. Hiding authorisation
    failures behind not-found makes real bugs indistinguishable from denials.
    """

    status_code = 403
    title = "Forbidden"
    type_uri = "https://akira.ops/errors/forbidden"


class NotFoundError(AppError):
    status_code = 404
    title = "Not Found"
    type_uri = "https://akira.ops/errors/not-found"


class ConflictError(AppError):
    """The request collides with the current state — a duplicate submission, or
    an edit to a run that has already been approved."""

    status_code = 409
    title = "Conflict"
    type_uri = "https://akira.ops/errors/conflict"


class ValidationError(AppError):
    status_code = 422
    title = "Unprocessable Content"
    type_uri = "https://akira.ops/errors/validation"


class RateLimitError(AppError):
    status_code = 429
    title = "Too Many Requests"
    type_uri = "https://akira.ops/errors/rate-limit"


def _problem(
    status: int,
    title: str,
    detail: str,
    instance: str,
    type_uri: str = "about:blank",
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        **(extra or {}),
    }
    return JSONResponse(status_code=status, content=body, media_type=CONTENT_TYPE, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.exception("unhandled application error", exc_info=exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_problem(str(request.url.path)),
            media_type=CONTENT_TYPE,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's structure is genuinely useful to a client building a form,
        # so keep it — but under a named key rather than as the whole body.
        return _problem(
            422,
            "Unprocessable Content",
            "The request body did not match what this endpoint expects.",
            str(request.url.path),
            "https://akira.ops/errors/validation",
            {"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            exc.status_code,
            str(exc.detail),
            str(exc.detail),
            str(request.url.path),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The only place a bare exception is caught. Log everything, say nothing.
        logger.exception("unhandled exception on %s", request.url.path, exc_info=exc)
        return _problem(
            500,
            "Internal Server Error",
            "Something went wrong on our side. The failure has been logged.",
            str(request.url.path),
        )
